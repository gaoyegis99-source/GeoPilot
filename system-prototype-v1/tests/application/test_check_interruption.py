import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from geopilot.application.plan_runner import _check_results, run_confirmed_plan
from geopilot.application.run_checks import RUN_CHECKS, RunCheckContext
from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import StepOutput, StepResult
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.compiler import PlanCompilationError
from geopilot.plans.models import AnalysisPlan
from geopilot.tools.registry import ToolRegistry


@pytest.fixture
def check_run(tmp_path):
    workdir = tmp_path / "run"
    (workdir / "outputs").mkdir(parents=True)
    (workdir / "checks").mkdir()
    artifact = StepOutput(path="outputs/statistics.csv", layer=None, type="table_csv")
    (workdir / artifact.path).write_text("zone_id,count\nwest,1\n", encoding="utf-8")
    plan = AnalysisPlan(
        id="plan", task_id="task", version=1, status="confirmed", goal={"summary": "Inspect actual statistics"},
        inputs={}, critical_options={}, method={"id": "statistics", "version": 1},
        steps=[{
            "key": "statistics", "tool": {"id": "python-statistics", "version": 1},
            "inputs": {}, "parameters": {}, "outputs": {"result": {"type": "table_csv", "target": artifact.path}},
        }],
        expected_artifacts=[{"role": "statistics", "step": "statistics", "output": "result", "type": "table_csv"}],
        check_rules=["output_exists", "interrupted", "later"], issues=[],
    )
    now = datetime.now(UTC)
    step = StepResult(
        run_id="run", step_key="statistics", status="SUCCEEDED", exit_code=0, outputs={"result": artifact},
        log_path="steps/statistics/process.log", warnings=[], error=None, started_at=now, finished_at=now,
        environment={},
    )
    context = RunCheckContext(plan, workdir, {}, {step.step_key: step}, {"statistics": artifact})
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    with open_database(database) as connection, connection:
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES (?, ?, ?)", (
            plan.task_id, plan.goal.model_dump_json(), now.isoformat(),
        ))
        connection.execute(
            "INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (plan.id, plan.version, plan.task_id, plan.status, plan.model_dump_json(), now.isoformat()),
        )
        connection.execute(
            "INSERT INTO run (id, plan_id, plan_version, execution_status, check_status, workdir, created_at) "
            "VALUES (?, ?, ?, 'RUNNING', 'NOT_CHECKED', ?, ?)",
            (step.run_id, plan.id, plan.version, str(workdir), now.isoformat()),
        )
    return database, context


def _persisted_error(database):
    with open_database(database) as connection:
        row = connection.execute("SELECT execution_status, check_status, error_json FROM run WHERE id = 'run'").fetchone()
    assert (row["execution_status"], row["check_status"]) == ("SUCCEEDED", "NOT_CHECKED")
    return ErrorDetail.model_validate_json(row["error_json"])


def _break_check(_context):
    raise RuntimeError("Checker could not finish")


@pytest.mark.parametrize("checker_registered", [True, False])
def test_interruption_saves_completed_evidence_and_pending_rules(check_run, checker_registered):
    database, context = check_run
    later_calls = []
    checks = {"output_exists": RUN_CHECKS["output_exists"], "later": later_calls.append}
    if checker_registered:
        checks["interrupted"] = _break_check
    artifact_path = context.workdir / context.artifacts["statistics"].path
    original = artifact_path.read_bytes()

    status, error = _check_results(database, "run", context, checks)

    assert status == "NOT_CHECKED" and error == _persisted_error(database)
    assert error.code == ("CHECK_INTERRUPTED" if checker_registered else "UNKNOWN_CHECK_RULE")
    assert error.phase == "check" and error.field == "check_rules.1"
    assert "Checker could not finish" in error.message if checker_registered else "interrupted" in error.message
    report = json.loads((context.workdir / "checks/report.json").read_text(encoding="utf-8"))
    assert report == error.observed
    assert report["status"] == "NOT_CHECKED"
    assert report["completed_rules"] == ["output_exists"]
    assert report["pending_rules"] == ["interrupted", "later"]
    assert list(report["rules"]) == ["output_exists"] and report["rules"]["output_exists"]["passed"]
    assert report["interruption"] == {"code": error.code, "field": error.field, "message": error.message}
    assert not later_calls
    assert context.steps["statistics"].status == "SUCCEEDED" and artifact_path.read_bytes() == original


@pytest.mark.parametrize("checker_interrupted", [False, True])
def test_unwritable_report_persists_full_check_evidence_in_database(check_run, checker_interrupted):
    database, context = check_run
    names = ("output_exists", "interrupted", "later") if checker_interrupted else ("output_exists",)
    context = replace(context, plan=context.plan.model_copy(update={"check_rules": names}))
    report_path = context.workdir / "checks/report.json"
    report_path.write_text("existing file", encoding="utf-8")

    status, error = _check_results(database, "run", context, {
        "output_exists": RUN_CHECKS["output_exists"], "interrupted": _break_check,
    })

    assert status == "NOT_CHECKED" and error == _persisted_error(database)
    assert (error.code, error.phase, error.field) == ("STORAGE_FAILED", "check", "checks.report")
    report = error.observed
    assert report["status"] == "NOT_CHECKED" and report["run_id"] == "run"
    assert report["completed_rules"] == ["output_exists"]
    assert report["pending_rules"] == (["interrupted", "later"] if checker_interrupted else [])
    assert report["rules"]["output_exists"]["observed"]["opened"]["statistics"]["path"] == "outputs/statistics.csv"
    assert report["interruption"]["code"] == "STORAGE_FAILED"
    if checker_interrupted:
        assert report["interruption"]["previous"]["code"] == "CHECK_INTERRUPTED"
    assert report_path.read_text(encoding="utf-8") == "existing file"
    assert list(report_path.parent.iterdir()) == [report_path]


def test_keyboard_interrupt_during_check_records_completed_and_pending_rules(check_run):
    database, context = check_run
    later_calls = []

    def interrupt(_context):
        raise KeyboardInterrupt

    try:
        status, error = _check_results(database, "run", context, {
            "output_exists": RUN_CHECKS["output_exists"], "interrupted": interrupt, "later": later_calls.append,
        })
    except KeyboardInterrupt:
        pytest.fail("The checker interrupt escaped before recording the final state")

    assert status == "NOT_CHECKED" and error == _persisted_error(database)
    assert (error.code, error.phase, error.field) == ("CHECK_INTERRUPTED", "check", "check_rules.1")
    assert "KeyboardInterrupt" in error.message
    report = json.loads((context.workdir / "checks/report.json").read_text(encoding="utf-8"))
    assert report == error.observed
    assert report["completed_rules"] == ["output_exists"] and report["pending_rules"] == ["interrupted", "later"]
    assert report["rules"]["output_exists"]["passed"] and not later_calls


@pytest.mark.parametrize("link_created", [False, True])
def test_keyboard_interrupt_during_publication_retains_database_evidence(check_run, monkeypatch, link_created):
    database, context = check_run
    context = replace(context, plan=context.plan.model_copy(update={"check_rules": ("output_exists",)}))
    report_path = context.workdir / "checks/report.json"
    if not link_created:
        report_path.write_text("previous report", encoding="utf-8")
    link = Path.hardlink_to

    def interrupt_link(path, staged):
        assert path == report_path
        if link_created:
            link(path, staged)
            assert path.is_file()
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "hardlink_to", interrupt_link)
    try:
        status, error = _check_results(database, "run", context, RUN_CHECKS)
    except KeyboardInterrupt:
        pytest.fail("The report publication interrupt escaped before recording the final state")

    assert status == "NOT_CHECKED" and error == _persisted_error(database)
    assert (error.code, error.phase, error.field) == ("CHECK_INTERRUPTED", "check", "checks.report")
    assert "KeyboardInterrupt" in error.message
    report = error.observed
    assert report["status"] == "NOT_CHECKED" and report["completed_rules"] == ["output_exists"]
    assert report["pending_rules"] == [] and report["rules"]["output_exists"]["passed"]
    assert report["interruption"] == {"code": error.code, "field": error.field, "message": error.message}
    if link_created:
        assert not list(report_path.parent.iterdir())
    else:
        assert report_path.read_text(encoding="utf-8") == "previous report"
        assert list(report_path.parent.iterdir()) == [report_path]


def test_legacy_confirmed_plan_without_check_rules_is_rejected_before_run_creation(check_run, tmp_path):
    database, context = check_run
    plan = context.plan.model_copy(update={"check_rules": ()})
    with open_database(database) as connection, connection:
        connection.execute("UPDATE analysis_plan SET plan_json = ? WHERE id = ? AND version = ?", (
            plan.model_dump_json(), plan.id, plan.version,
        ))

    with pytest.raises(PlanCompilationError) as raised:
        run_confirmed_plan(
            database, plan.id, plan.version, workspace_root=tmp_path / "workspace",
            run_id="new-run", registry=ToolRegistry(),
        )

    assert (raised.value.code, raised.value.field) == ("MISSING_CHECK_RULES", "check_rules")
    assert not (tmp_path / "workspace").exists()
    with open_database(database) as connection:
        assert connection.execute("SELECT id FROM run WHERE id = 'new-run'").fetchone() is None
