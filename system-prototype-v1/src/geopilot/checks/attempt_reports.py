"""Finalize a reserved check attempt when its work cannot finish."""

from sqlite3 import Connection

from geopilot.checks.report_service import CheckAttempt, CheckReport, publish_report
from geopilot.checks.tool_checks import tool_check_key, tool_check_scopes
from geopilot.domain.errors import ErrorDetail
from geopilot.persistence.plan_repository import plan_from_row, row_for_plan
from geopilot.persistence.run_repository import get_run
from geopilot.runs.models import CheckStatus

CHECK_REPORT_PATH = "checks/report.json"


def publish_unfinished_attempt(connection: Connection, attempt: CheckAttempt, error: ErrorDetail) -> None:
    report = {
        "report_id": attempt.id, "run_id": attempt.run_id, "status": "NOT_CHECKED",
        "rules": {}, "completed_rules": [], "pending_rules": list(attempt.rule_versions),
        "interruption": {"code": error.code, "field": error.field, "message": error.message},
        "phase_findings": {"input": [], "process": [], "artifact": []},
    }
    run = get_run(connection, attempt.run_id)
    if run is not None and (row := row_for_plan(connection, run.plan_id, run.plan_version)) is not None:
        pending = [tool_check_key(step, rule) for step, rule in tool_check_scopes(plan_from_row(row))]
        if pending:
            report.update(tool_rules={}, completed_tool_rules=[], pending_tool_rules=pending)
    publish_report(connection, CheckReport(
        id=attempt.id, run_id=attempt.run_id, status=CheckStatus.NOT_CHECKED,
        report=report, rule_versions=attempt.rule_versions, created_at=attempt.created_at,
    ), error=error.model_copy(update={"observed": report}))
