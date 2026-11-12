from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event

import pytest

from geopilot.domain.step_io import ExecutionStatus
from geopilot.persistence.run_repository import get_run, insert_run, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.runs.models import CheckStatus, Run
from geopilot.scheduler.loop import claim_next_run, recover_interrupted_runs, run_loop, run_once


@pytest.fixture
def database(tmp_path):
    db = tmp_path / "project.sqlite"
    initialize_database(db)
    with open_database(db) as connection, connection:
        connection.execute("INSERT INTO task VALUES ('task', '{}', NULL, '2026-11-11')")
        connection.execute(
            "INSERT INTO analysis_plan VALUES ('plan', 1, 'task', 'confirmed', '{}', '2026-11-11')"
        )
        for run_id in ("first", "second"):
            insert_run(connection, Run(
                id=run_id, plan_id="plan", plan_version=1, workdir=str(tmp_path / run_id),
                created_at=datetime(2026, 11, 11, tzinfo=UTC),
            ))
    return db


def _read(database, run_id):
    with open_database(database) as connection:
        return get_run(connection, run_id)


def _finish(database, run, execution=ExecutionStatus.SUCCEEDED, check=CheckStatus.PASSED):
    with open_database(database) as connection, connection:
        update_run(connection, run.model_copy(update={"execution_status": execution, "check_status": check}))


def test_atomic_claim_does_not_dispatch_twice(database):
    barrier = Barrier(2, timeout=5)

    def claim():
        barrier.wait()
        return claim_next_run(database)

    with ThreadPoolExecutor(max_workers=2) as executor:
        calls = [executor.submit(claim) for _ in range(2)]
        claims = [call.result(timeout=10) for call in calls]
    run, = [claim for claim in claims if claim is not None]
    assert run.id == "first" and _read(database, run.id).execution_status is ExecutionStatus.RUNNING
    assert _read(database, "second").execution_status is ExecutionStatus.NOT_STARTED
    _finish(database, run, check=CheckStatus.CHECKING)
    assert claim_next_run(database) is None
    _finish(database, _read(database, run.id))
    assert claim_next_run(database).id == "second"


def test_worker_failure_releases_queue_and_keeps_error(database):
    def execute(run):
        assert _read(database, run.id).execution_status is ExecutionStatus.RUNNING
        raise RuntimeError("worker lost its input")

    assert run_once(database, execute) == "first"
    row = _read(database, "first")
    assert row.execution_status is ExecutionStatus.FAILED and row.check_status is CheckStatus.NOT_CHECKED
    assert row.error.code == "RUN_INTERRUPTED" and "worker lost its input" in row.error.message
    assert run_once(database, lambda run: _finish(database, run)) == "second"
    assert _read(database, "second").execution_status is ExecutionStatus.SUCCEEDED
    assert run_once(database, lambda run: pytest.fail("empty queue dispatched")) is None


def test_recovery_requires_verified_process_stop_and_preserves_computation(database):
    run = claim_next_run(database)
    assert run.id == "first"
    with pytest.raises(RuntimeError, match="first"):
        recover_interrupted_runs(database, processes_stopped=lambda _: False)
    assert _read(database, "first").execution_status is ExecutionStatus.RUNNING
    assert recover_interrupted_runs(database, processes_stopped=lambda _: True) == ("first",)
    assert _read(database, "first").error.code == "RUN_INTERRUPTED"
    second = claim_next_run(database)
    _finish(database, second, check=CheckStatus.CHECKING)
    assert recover_interrupted_runs(database, processes_stopped=lambda _: True) == ("second",)
    row = _read(database, "second")
    assert row.execution_status is ExecutionStatus.SUCCEEDED and row.check_status is CheckStatus.NOT_CHECKED
    assert row.error.code == "CHECK_INTERRUPTED"


def test_loop_observes_stop_and_keeps_remaining_queue(database):
    stop = Event()
    seen = []

    def execute(run):
        seen.append(run.id)
        _finish(database, run)
        stop.set()

    run_loop(database, execute, stop, poll_seconds=0.01)
    assert seen == ["first"]
    assert _read(database, "second").execution_status is ExecutionStatus.NOT_STARTED
