from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
import sqlite3
from threading import Event
from time import monotonic, sleep

from fastapi.testclient import TestClient
import pytest

from geopilot.api import app as http
from geopilot.api.app import create_app
from geopilot.persistence.run_repository import get_run, insert_run, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.runs.models import Run
from geopilot.scheduler import loop


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "project.sqlite"
    initialize_database(path)
    with open_database(path) as connection, connection:
        connection.execute("INSERT INTO task VALUES ('task', '{}', NULL, '2026-11-11')")
        connection.execute("INSERT INTO analysis_plan VALUES ('plan', 1, 'task', 'confirmed', '{}', '2026-11-11')")
    return path


def enqueue(connection, database, *ids):
    for run_id in ids:
        insert_run(connection, Run(
            id=run_id, plan_id="plan", plan_version=1, workdir=str(database.parent / run_id),
            created_at=datetime(2026, 11, 11, tzinfo=UTC),
        ))


def finish(database, run):
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        current = get_run(connection, run.id)
        update_run(connection, current.model_copy(update={"execution_status": "SUCCEEDED", "check_status": "PASSED"}))


def status(client, run_id):
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_for(predicate, seconds=8):
    deadline = monotonic() + seconds
    while not predicate():
        assert monotonic() < deadline, "Scheduler did not reach the expected state"
        sleep(0.025)


def test_scheduler_resumes_after_real_claim_busy_timeout(database, monkeypatch):
    real_claim = loop.claim_next_run
    contended = Event()
    dispatched = []

    def observe_claim(path):
        try:
            return real_claim(path)
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                contended.set()
            raise

    def execute(run):
        dispatched.append(run.id)
        finish(database, run)

    monkeypatch.setattr(loop, "claim_next_run", observe_claim)
    with TestClient(create_app(database, execute_run=execute)) as client:
        with open_database(database) as holder, holder:
            holder.execute("BEGIN IMMEDIATE")
            enqueue(holder, database, "first", "second")
            assert contended.wait(8), "Expected the real SQLite lock timeout while claiming"
            assert dispatched == []
        wait_for(lambda: status(client, "second")["execution_status"] == "SUCCEEDED")
        assert status(client, "first")["execution_status"] == "SUCCEEDED"
    assert dispatched == ["first", "second"]


def test_scheduler_settles_a_claim_after_timeout_without_reexecution(database):
    release = Event()
    dispatched = []

    def execute(run):
        dispatched.append(run.id)
        if run.id == "first":
            assert release.wait(8)
            raise RuntimeError("The worker stopped before publishing a result")
        finish(database, run)

    with open_database(database) as connection, connection:
        enqueue(connection, database, "first", "second")

    with TestClient(create_app(database, execute_run=execute)) as client:
        try:
            wait_for(lambda: status(client, "first")["execution_status"] == "RUNNING")
            with open_database(database) as holder, holder:
                holder.execute("BEGIN IMMEDIATE")
                assert holder.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
                release.set()
                # Hold across a complete attempt to settle the claimed run.
                sleep(5.35)
                assert status(client, "first")["execution_status"] == "RUNNING"
                assert status(client, "second")["execution_status"] == "NOT_STARTED"
                assert dispatched == ["first"]
            wait_for(lambda: status(client, "second")["execution_status"] == "SUCCEEDED")
            interrupted = status(client, "first")
            assert interrupted["execution_status"] == "FAILED"
            assert interrupted["error"]["code"] == "RUN_INTERRUPTED"
            assert "before publishing a result" in interrupted["error"]["message"]
        finally:
            release.set()
    assert dispatched == ["first", "second"]


def test_cancellation_burst_and_restart_drain_the_queue_once(database, monkeypatch):
    ids = [f"run-{i:03}" for i in range(128)]
    cancelled = ids[1:65]
    remaining = ids[65:]
    dispatched = []
    active = Event()
    service_stop = []

    def observe_service_stop(path, execute, stop, **kwargs):
        service_stop.append(stop)
        loop.run_loop(path, execute, stop, **kwargs)

    def execute(run):
        dispatched.append(run.id)
        if run.id == ids[0]:
            active.set()
            assert service_stop[0].wait(10)
        finish(database, run)

    with open_database(database) as connection, connection:
        enqueue(connection, database, *ids)

    monkeypatch.setattr(http, "run_loop", observe_service_stop)
    with TestClient(create_app(database, execute_run=execute)) as client:
        assert active.wait(8)
        started = monotonic()

        def cancel(run_id):
            response = client.post(f"/api/runs/{run_id}/cancel")
            assert response.status_code in {200, 202}, response.text
            assert response.json()["execution_status"] == "CANCELLED"

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(cancel, cancelled * 3))
        print(f"192 cancellation requests across 64 queued runs in {monotonic() - started:.3f}s")
        assert dispatched == ids[:1]
        assert all(status(client, run_id)["execution_status"] == "CANCELLED" for run_id in cancelled)

    with open_database(database, read_only=True) as connection:
        assert all(get_run(connection, run_id).execution_status == "NOT_STARTED" for run_id in remaining)
    with TestClient(create_app(database, execute_run=execute)) as client:
        wait_for(lambda: status(client, ids[-1])["execution_status"] == "SUCCEEDED")
        assert all(status(client, run_id)["execution_status"] == "CANCELLED" for run_id in cancelled)
        assert all(status(client, run_id)["execution_status"] == "SUCCEEDED" for run_id in ids[:1] + remaining)
    assert dispatched == ids[:1] + remaining


def test_shutdown_during_settlement_contention_leaves_recoverable_run(database, monkeypatch):
    stopped, worker_ready, worker_release, busy = Event(), Event(), Event(), Event()
    dispatched = []

    @contextmanager
    def short_busy_wait(*args, **kwargs):
        try:
            with open_database(*args, **kwargs) as connection:
                connection.execute("PRAGMA busy_timeout = 50")
                yield connection
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                busy.set()
            raise

    def execute(run):
        dispatched.append(run.id)
        worker_ready.set()
        assert worker_release.wait(5)

    with open_database(database) as connection, connection:
        enqueue(connection, database, "first", "second")
    monkeypatch.setattr(loop, "open_database", short_busy_wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        service = pool.submit(loop.run_loop, database, execute, stopped, poll_seconds=0.01)
        try:
            assert worker_ready.wait(5)
            with open_database(database) as holder, holder:
                holder.execute("BEGIN IMMEDIATE")
                worker_release.set()
                assert busy.wait(5)
                stopped.set()
                assert service.result(timeout=2) is None
        finally:
            stopped.set()
            worker_release.set()
    assert dispatched == ["first"]
    with open_database(database, read_only=True) as connection:
        assert get_run(connection, "first").execution_status == "RUNNING"
        assert get_run(connection, "second").execution_status == "NOT_STARTED"

    with TestClient(create_app(database, execute_run=lambda run: finish(database, run))) as client:
        wait_for(lambda: status(client, "second")["execution_status"] == "SUCCEEDED")
        interrupted = status(client, "first")
        assert interrupted["execution_status"] == "FAILED"
        assert interrupted["error"]["code"] == "RUN_INTERRUPTED"


def test_fatal_storage_error_is_not_retried(tmp_path):
    with pytest.raises(sqlite3.OperationalError) as failure:
        loop.run_loop(tmp_path / "uninitialized.sqlite", lambda run: pytest.fail("Unexpected dispatch"), Event())
    assert failure.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
