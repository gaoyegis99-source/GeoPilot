import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pyogrio
import pytest
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.application.plan_runner import execute_claimed_run
from geopilot.artifacts.service import list_artifacts
from geopilot.execution.process_manager import capture_process
from geopilot.persistence.run_repository import list_run_steps
from geopilot.persistence.schema import open_database
from geopilot.scheduler import cancellation, step_executor
from geopilot.scheduler.loop import claim_next_run, run_once


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "src/geopilot/gis/tools/prepare_measurement_lines.py"


@pytest.mark.parametrize(
    ("script", "timeout", "timed_out"),
    [
        ("from pathlib import Path; Path('worker-result.txt').write_text('complete')", 1.5, False),
        ("import time; time.sleep(30)", 0.25, True),
    ],
    ids=["successful-worker", "worker-deadline"],
)
def test_worker_cancellation_poll_tolerates_exclusive_database_lock(queued_run, script, timeout, timed_out):
    database, _, _, _, run_id, workdir = queued_run
    assert claim_next_run(database).id == run_id
    with open_database(database) as writer, writer:
        writer.execute("BEGIN EXCLUSIVE")
        started = monotonic()
        outcome = capture_process(
            [sys.executable, "-c", script], cwd=workdir, timeout=timeout,
            cancel_requested=lambda: cancellation.observe_cancellation(database, run_id, workdir, "cancel.flag"),
        )
        elapsed = monotonic() - started

    assert elapsed < 3, f"Cancellation polling stalled the worker for {elapsed:.3f}s"
    assert outcome.timed_out is timed_out and not outcome.cancelled
    assert not (workdir / "cancel.flag").exists()
    if not timed_out:
        assert outcome.exit_code == 0
        assert (workdir / "worker-result.txt").read_text(encoding="utf-8") == "complete"
    else:
        assert outcome.exit_code != 0


def test_worker_observes_committed_cancellation_after_database_lock_clears(queued_run):
    database, _, _, _, run_id, workdir = queued_run
    assert claim_next_run(database).id == run_id
    polled = Event()

    def observe():
        requested = cancellation.observe_cancellation(database, run_id, workdir, "cancel.flag")
        polled.set()
        return requested

    with ThreadPoolExecutor(max_workers=1) as pool:
        with open_database(database) as writer, writer:
            writer.execute("BEGIN EXCLUSIVE")
            call = pool.submit(
                capture_process, [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=workdir, timeout=8, cancel_requested=observe,
            )
            assert polled.wait(3)
            assert not (workdir / "cancel.flag").exists()
        run, accepted = cancellation.request_cancellation(database, run_id)
        assert accepted and run.cancel_requested
        outcome = call.result(timeout=5)

    assert outcome.cancelled and not outcome.timed_out and outcome.exit_code != 0
    assert (workdir / "cancel.flag").is_file()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY_SNAPSHOT, sqlite3.SQLITE_LOCKED_SHAREDCACHE])
def test_cancellation_observation_handles_extended_lock_codes(queued_run, monkeypatch, code):
    database, _, _, _, run_id, workdir = queued_run

    def locked(*args):
        error = sqlite3.OperationalError("test lock")
        error.sqlite_errorcode = code
        raise error

    monkeypatch.setattr(cancellation, "get_run", locked)
    assert not cancellation.observe_cancellation(database, run_id, workdir, "cancel.flag")
    assert not (workdir / "cancel.flag").exists()


def test_cancellation_observation_propagates_non_lock_sqlite_error(tmp_path):
    database = tmp_path / "no-schema.sqlite"
    with open_database(database):
        pass
    with pytest.raises(sqlite3.OperationalError, match="no such table") as caught:
        cancellation.observe_cancellation(database, "run", tmp_path, "cancel.flag")
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_ERROR


def test_queued_cancel_is_never_dispatched(queued_run):
    database, _, _, _, run_id, path = queued_run
    with TestClient(create_app(database)) as client:
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 202 and response.json()["cancel_requested"] is True
        assert run_once(database, lambda run: pytest.fail("cancelled run dispatched")) is None
        state = client.get(f"/api/runs/{run_id}").json()
        assert (state["execution_status"], state["check_status"]) == ("CANCELLED", "NOT_CHECKED")
        again = client.post(f"/api/runs/{run_id}/cancel")
        assert again.status_code == 200 and again.json() == state
    assert not list((path / "steps").iterdir())


def test_active_cancel_waits_for_real_gis_worker_exit_and_stops_dispatch(queued_run, tmp_path):
    database, workspace, registry, implementation, run_id, path = queued_run
    worker = tmp_path / "delayed_measurement.py"
    worker.write_text(
        "import os, runpy, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"try:\n    runpy.run_path({str(WORKER)!r}, run_name='__main__')\n"
        "except SystemExit as error:\n    assert error.code == 0\n"
        "print('Actual GIS output written', flush=True)\n"
        "Path('active.pid').write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    def execute(run):
        return execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry,
            python_implementations={implementation: worker}, timeout=10,
        )

    with TestClient(create_app(database)) as client, ThreadPoolExecutor() as pool:
        call = pool.submit(run_once, database, execute)
        pid_file = path / "active.pid"
        deadline = monotonic() + 5
        while (not pid_file.exists() or not pid_file.stat().st_size) and monotonic() < deadline:
            sleep(0.02)
        pid = int(pid_file.read_text(encoding="utf-8"))
        process_stat = Path(f"/proc/{pid}/stat")
        assert process_stat.exists()
        cancelled_at = monotonic()
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 202
        assert response.json()["cancel_requested"] is True
        assert response.json()["execution_status"] == "RUNNING"
        assert call.result(timeout=8) == run_id
        assert not process_stat.exists()
        print(f"Native GIS cancellation to worker exit: {monotonic() - cancelled_at:.3f}s")
        state = client.get(f"/api/runs/{run_id}").json()
    assert (state["execution_status"], state["check_status"]) == ("CANCELLED", "NOT_CHECKED")
    assert state["current_step"] == "prepare" and state["error"]["code"] == "PROCESS_CANCELLED"
    assert (path / "cancel.flag").is_file()
    with open_database(database) as connection:
        first, second, third = list_run_steps(connection, run_id)
    assert first.status == "CANCELLED" and first.result.exit_code != 0
    assert "Actual GIS output written" in (path / first.log_path).read_text(encoding="utf-8")
    result = first.request.output_targets["roads"]
    assert len(pyogrio.read_dataframe(path / result.path, layer=result.layer)) == 3
    assert second.status == "NOT_STARTED" and not (path / "steps/consume").exists()
    assert third.status == "NOT_STARTED" and not (path / "steps/finalize").exists()
    assert not (path / "outputs/manifest.json").exists()


def test_cancel_between_successful_steps_preserves_the_first_result(queued_run, monkeypatch):
    database, workspace, registry, implementation, run_id, path = queued_run
    with TestClient(create_app(database)) as client:
        run_step = step_executor.run_step

        def run_then_cancel(request, **kwargs):
            assert request.step_key == "prepare"
            result = run_step(request, **kwargs)
            assert result.status == "SUCCEEDED"
            assert client.post(f"/api/runs/{run_id}/cancel").status_code == 202
            return result

        monkeypatch.setattr(step_executor, "run_step", run_then_cancel)
        assert run_once(database, lambda run: execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry,
            python_implementations={implementation: WORKER},
        )) == run_id
        state = client.get(f"/api/runs/{run_id}").json()
    assert state["execution_status"] == "CANCELLED" and state["error"]["code"] == "RUN_CANCELLED"
    with open_database(database) as connection:
        first, second, third = list_run_steps(connection, run_id)
    assert first.status == "SUCCEEDED" and second.status == "NOT_STARTED"
    output = first.result.outputs["roads"]
    assert len(pyogrio.read_dataframe(path / output.path, layer=output.layer)) == 3
    assert not (path / "steps/consume").exists()
    assert third.status == "NOT_STARTED" and not (path / "steps/finalize").exists()
    assert not (path / "outputs/manifest.json").exists()


def test_cancel_after_last_worker_exit_prevents_artifact_publication(queued_run, monkeypatch):
    database, workspace, registry, implementation, run_id, path = queued_run
    with TestClient(create_app(database)) as client:
        run_step = step_executor.run_step

        def run_then_cancel(request, **kwargs):
            if request.step_key != "finalize":
                return run_step(request, **kwargs)
            with open_database(database) as writer, writer:
                writer.execute("BEGIN EXCLUSIVE")
                result = run_step(request, **kwargs)
            assert result.status == "SUCCEEDED" and result.exit_code == 0
            response = client.post(f"/api/runs/{run_id}/cancel")
            assert response.status_code == 202 and response.json()["cancel_requested"] is True
            return result

        monkeypatch.setattr(step_executor, "run_step", run_then_cancel)
        assert run_once(database, lambda run: execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry,
            python_implementations={implementation: WORKER, "test.measurement_middle": WORKER},
        )) == run_id
        state = client.get(f"/api/runs/{run_id}").json()

    assert (state["execution_status"], state["check_status"]) == ("CANCELLED", "NOT_CHECKED")
    assert state["error"]["code"] == "RUN_CANCELLED"
    with open_database(database, read_only=True) as connection:
        assert [step.status for step in list_run_steps(connection, run_id)] == ["SUCCEEDED"] * 3
        assert not list_artifacts(connection, run_id)
    assert not (path / "outputs/manifest.json").exists()
