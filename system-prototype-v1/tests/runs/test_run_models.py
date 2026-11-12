import sqlite3

import pytest
from pydantic import ValidationError

from geopilot.domain.step_io import ExecutionStatus, StepRequest, StepResult
from geopilot.persistence.run_repository import (
    get_run,
    get_run_step,
    insert_run,
    list_run_steps,
    save_run_step,
    update_run,
)
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.runs.models import CheckStatus, Run, RunStep


CREATED_AT = "2026-11-11T00:00:00Z"
FINISHED_AT = "2026-11-11T00:01:00Z"
LOG = "steps/buffer/process.log"
RUN_DATA = {"id": "run-1", "plan_id": "plan-1", "plan_version": 2, "workdir": "workspace/run-1", "created_at": CREATED_AT}


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "runs.sqlite"
    initialize_database(path)
    with open_database(path) as connection, connection:
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES ('task-1', '{}', ?)", (CREATED_AT,))
        connection.executemany(
            """INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at)
               VALUES ('plan-1', ?, 'task-1', 'confirmed', '{}', ?)""", [(1, CREATED_AT), (2, CREATED_AT)],
        )
    return path


def _request():
    return StepRequest.model_validate({
        "run_id": "run-1", "step_key": "buffer", "tool": {"id": "qgis-buffer", "version": 1},
        "workdir_ref": "run-1", "inputs": {}, "parameters": {"distance": 500},
        "output_targets": {"buffer": {"path": "intermediate/buffer.gpkg", "layer": "buffer"}},
        "log_path": LOG, "cancel_path": "cancel.flag",
    })


def _result():
    return StepResult.model_validate({
        "run_id": "run-1", "step_key": "buffer", "status": "SUCCEEDED", "exit_code": 0,
        "outputs": {"buffer": {"path": "intermediate/buffer.gpkg", "layer": "buffer", "type": "vector_polygon"}},
        "log_path": LOG, "warnings": [], "error": None, "started_at": CREATED_AT, "finished_at": FINISHED_AT,
        "environment": {"qgis_version": "3.44.15"},
    })


def test_run_statuses_are_independent_and_match_the_design():
    assert [value.value for value in ExecutionStatus] == ["NOT_STARTED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
    assert [value.value for value in CheckStatus] == ["NOT_CHECKED", "CHECKING", "PASSED", "WARNING", "FAILED"]
    initial = Run(**RUN_DATA)
    assert initial.execution_status is ExecutionStatus.NOT_STARTED
    assert initial.check_status is CheckStatus.NOT_CHECKED
    assert not initial.cancel_requested and initial.current_step is initial.current_report_id is initial.error is None

    for check_status in CheckStatus:
        run = Run(**RUN_DATA, execution_status="SUCCEEDED", check_status=check_status)
        assert run.execution_status is ExecutionStatus.SUCCEEDED and run.check_status is check_status
        assert Run.model_validate_json(run.model_dump_json()) == run


@pytest.mark.parametrize(("field", "value"), [
    ("plan_version", True), ("plan_version", "2"), ("plan_version", 1.5), ("plan_version", 0),
    ("cancel_requested", 1), ("execution_status", "PASSED"), ("check_status", "RUNNING"),
    ("created_at", "2026-11-11T00:00:00"),
])
def test_run_rejects_mistyped_fields(field, value):
    with pytest.raises(ValidationError):
        Run.model_validate({**RUN_DATA, field: value})


def test_step_request_result_and_log_belong_to_the_same_record():
    request, result = _request(), _result()
    step = RunStep(run_id="run-1", step_key="buffer", position=0, status="SUCCEEDED", request=request, result=result,
                   log_path=LOG, started_at=CREATED_AT, finished_at=FINISHED_AT)
    assert RunStep.model_validate_json(step.model_dump_json()) == step
    payload = step.model_dump(mode="python")
    for change in (
        {"run_id": "other"}, {"step_key": "other"}, {"status": "FAILED"}, {"log_path": "steps/other/process.log"},
        {"position": -1}, {"position": True}, {"position": "0"}, {"finished_at": "2026-11-10T00:00:00Z"},
    ):
        with pytest.raises(ValidationError):
            RunStep.model_validate({**payload, **change})


def test_repository_persists_typed_snapshots_and_ordered_steps(database):
    run = Run(**RUN_DATA)
    later = RunStep(run_id=run.id, step_key="aggregate", position=1)
    queued = RunStep(run_id=run.id, step_key="buffer", position=0)
    finished = RunStep(run_id=run.id, step_key="buffer", position=0, status="SUCCEEDED", request=_request(), result=_result(),
                       log_path=LOG, started_at=CREATED_AT, finished_at=FINISHED_AT)
    data = run.model_dump(mode="python")
    data.update(execution_status="SUCCEEDED", check_status="FAILED", current_step="buffer", current_report_id="report-1",
                cancel_requested=True, error={"code": "CHECK_FAILED", "phase": "check", "step_key": "buffer",
                                              "field": "rules.0", "message": "Rule failed", "suggestion": "Review the report",
                                              "log_path": LOG, "observed": {"count": 1}})
    checked = Run.model_validate(data)

    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        assert insert_run(connection, run) == run
        assert save_run_step(connection, later) == later
        assert save_run_step(connection, queued) == queued
        assert save_run_step(connection, finished) == finished
        assert update_run(connection, checked) == checked

    with open_database(database) as connection:
        assert get_run(connection, run.id) == checked
        assert get_run(connection, "unknown") is None
        assert get_run_step(connection, run.id, "buffer") == finished
        assert get_run_step(connection, run.id, "unknown") is None
        assert list_run_steps(connection, run.id) == (finished, later)
        assert list_run_steps(connection, "unknown") == ()
        assert connection.execute("SELECT plan_version, cancel_requested FROM run").fetchone()[:] == (2, 1)


def test_repository_preserves_identity_unique_order_and_foreign_keys(database):
    run = Run(**RUN_DATA)
    queued = RunStep(run_id=run.id, step_key="buffer", position=0)
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            insert_run(connection, run.model_copy(update={"plan_version": 3}))
        with pytest.raises(sqlite3.IntegrityError):
            save_run_step(connection, queued)
        insert_run(connection, run)
        save_run_step(connection, queued)
        with pytest.raises(ValueError, match="position"):
            save_run_step(connection, queued.model_copy(update={"position": 1}))
        with pytest.raises(sqlite3.IntegrityError):
            save_run_step(connection, queued.model_copy(update={"step_key": "different"}))
        with pytest.raises(ValueError, match="identity"):
            update_run(connection, run.model_copy(update={"plan_version": 1}))
        assert get_run(connection, run.id) == run
        assert list_run_steps(connection, run.id) == (queued,)


def test_repository_leaves_transaction_to_caller(database):
    run = Run(**RUN_DATA)
    with open_database(database) as writer:
        writer.execute("BEGIN IMMEDIATE")
        insert_run(writer, run)
        save_run_step(writer, RunStep(run_id=run.id, step_key="buffer", position=0))
        with open_database(database) as reader:
            assert get_run(reader, run.id) is None
            assert list_run_steps(reader, run.id) == ()
        writer.rollback()
    with open_database(database) as reader:
        assert get_run(reader, run.id) is None
        assert list_run_steps(reader, run.id) == ()
