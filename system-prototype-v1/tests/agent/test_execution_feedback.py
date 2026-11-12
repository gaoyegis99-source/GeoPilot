from datetime import UTC, datetime
import json

from fastapi.testclient import TestClient
import pytest

from geopilot.api.app import create_app
from geopilot.checks.report_service import CheckReport, publish_report
from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import ExecutionStatus, StepResult
from geopilot.llm.execution_feedback import ExecutionFeedback, build_execution_feedback, build_feedback_prompt
from geopilot.persistence.run_repository import insert_run, save_run_step
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.runs.models import Run, RunStep


_NOW = datetime(2026, 11, 12, tzinfo=UTC)
_PRIVATE = "/private/local/users/alpha/protected.gpkg"


@pytest.fixture
def feedback_project(tmp_path):
    db = tmp_path / "project.sqlite"
    workdir = tmp_path / "runs" / "run-one"
    workdir.mkdir(parents=True)
    initialize_database(db)
    with open_database(db) as connection, connection:
        connection.execute("INSERT INTO task VALUES ('task', '{}', NULL, '2026-11-12')")
        connection.execute("INSERT INTO analysis_plan VALUES ('plan', 1, 'task', 'confirmed', '{}', '2026-11-12')")
    return db, workdir


def stored_failure(db, workdir, *, code="PROCESS_TIMEOUT", log="process.log"):
    error = ErrorDetail(
        code=code, phase="execute", step_key="buffer", field="source", message=_PRIVATE,
        suggestion="Review the local log", log_path=log, observed={"raw": "private-field"},
    )
    run = Run(
        id="run-one", plan_id="plan", plan_version=1, workdir=str(workdir), created_at=_NOW,
        execution_status=ExecutionStatus.FAILED, current_step="buffer", error=error,
    )
    result = StepResult(
        run_id=run.id, step_key="buffer", status=ExecutionStatus.FAILED, exit_code=-15,
        outputs={}, log_path=log, warnings=[_PRIVATE], error=error,
        started_at=_NOW, finished_at=_NOW, environment={"private": _PRIVATE},
    )
    step = RunStep(run_id=run.id, step_key="buffer", position=0, status=result.status, result=result)
    with open_database(db) as connection, connection:
        insert_run(connection, run)
        save_run_step(connection, step)
    return run, step


def test_endpoint_reads_persisted_step_and_only_summarizes_log_tail(feedback_project):
    db, workdir = feedback_project
    run, step = stored_failure(db, workdir)
    log = workdir / "process.log"
    log.write_text(f"exit_code: 8\n{_PRIVATE}\n" + ("x" * 10_000) + "\nexit_code: -15\n")
    original = db.read_bytes(), log.read_bytes()

    with TestClient(create_app(db)) as client:
        response = client.get(f"/api/runs/{run.id}/feedback")
    assert response.status_code == 200, response.text
    feedback = ExecutionFeedback.model_validate(response.json())
    (summary,) = feedback.steps
    assert (summary.step_key, summary.status, summary.error_code) == (
        step.step_key, ExecutionStatus.FAILED, "PROCESS_TIMEOUT",
    )
    assert summary.log_available and summary.exit_code == summary.recorded_exit_code == -15
    assert summary.warning_count == 1
    assert [(item.kind, item.step_key, item.error_code) for item in feedback.statements] == [
        ("fact", "buffer", "PROCESS_TIMEOUT"), ("inference", "buffer", "PROCESS_TIMEOUT"),
    ]
    assert _PRIVATE not in response.text and "private-field" not in response.text
    assert (db.read_bytes(), log.read_bytes()) == original


def test_prompt_allowlists_codes_and_omits_names_paths_messages_and_data(feedback_project):
    db, workdir = feedback_project
    run, step = stored_failure(db, workdir, code="PRIVATE_TOKEN_IN_ERROR_CODE")
    log = workdir / "process.log"
    log.write_text(f"{_PRIVATE}\nexit_code: 7\n")

    feedback = build_execution_feedback(run, [step], None, logs={"buffer": log})
    assert feedback.steps[0].recorded_exit_code is None
    assert feedback.statements[0].error_code == "PRIVATE_TOKEN_IN_ERROR_CODE"
    prompt = build_feedback_prompt(feedback)
    encoded = json.dumps(prompt)
    assert prompt["facts"]["run_error_code"] == "OTHER_ERROR"
    assert prompt["facts"]["steps"][0]["error_code"] == "OTHER_ERROR"
    for value in ("run-one", "buffer", "process.log", "PRIVATE_TOKEN_IN_ERROR_CODE", _PRIVATE, "private-field"):
        assert value not in encoded


def test_log_link_is_excluded_by_workspace_resolution(feedback_project, tmp_path):
    db, workdir = feedback_project
    run, _ = stored_failure(db, workdir)
    external = tmp_path / "protected.log"
    external.write_text(f"{_PRIVATE}\nexit_code: -15\n")
    (workdir / "process.log").symlink_to(external)
    with TestClient(create_app(db)) as client:
        response = client.get(f"/api/runs/{run.id}/feedback")
    assert response.status_code == 200
    (step,) = response.json()["steps"]
    assert not step["log_available"] and step["recorded_exit_code"] is None


def test_report_keeps_execution_success_separate_from_failed_checks(feedback_project):
    db, workdir = feedback_project
    run = Run(
        id="run-one", plan_id="plan", plan_version=1, workdir=str(workdir), created_at=_NOW,
        execution_status=ExecutionStatus.SUCCEEDED, check_status="CHECKING",
    )
    report = CheckReport(
        id="report-one", run_id=run.id, status="FAILED", created_at=_NOW, rule_versions={"area_sanity": 1},
        report={
            "rules": {"area_sanity": {
                "passed": False, "severity": "blocking", "message": _PRIVATE, "observed": [1.25, 2.5],
            }},
            "phase_findings": {"input": [{
                "rule": "crs_present", "step_key": "prepare", "passed": True, "severity": "warning",
                "expected": "PRIVATE_FIELD_VALUE",
            }]},
        },
    )
    with open_database(db) as connection, connection:
        insert_run(connection, run)
        publish_report(connection, report)

    with TestClient(create_app(db)) as client:
        feedback = ExecutionFeedback.model_validate(client.get(f"/api/runs/{run.id}/feedback").json())
    assert feedback.execution_status is ExecutionStatus.SUCCEEDED
    assert feedback.check_status == "FAILED" and feedback.check_report_id == report.id
    assert [(item.source, item.rule, item.passed) for item in feedback.findings] == [
        ("method", "area_sanity", False), ("input", "crs_present", True),
    ]
    assert len(feedback.statements) == 1 and feedback.statements[0].check_index == 0
    wire = json.dumps(build_feedback_prompt(feedback))
    for value in ("area_sanity", "crs_present", "report-one", "prepare", "PRIVATE_FIELD_VALUE", "1.25", _PRIVATE):
        assert value not in wire


def test_feedback_rejects_step_from_another_run(feedback_project):
    db, workdir = feedback_project
    run, step = stored_failure(db, workdir)
    with pytest.raises(ValueError, match="does not belong"):
        build_execution_feedback(run.model_copy(update={"id": "different-run"}), [step], None, logs={})


def test_missing_run_returns_existing_not_found_error(feedback_project):
    db, _ = feedback_project
    with TestClient(create_app(db)) as client:
        response = client.get("/api/runs/missing/feedback")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"
