"""Summarize completed findings and publish an immutable check report."""

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from geopilot.domain.errors import ErrorDetail, _Text
from geopilot.domain.step_io import ExecutionStatus, _Timestamp
from geopilot.persistence.run_repository import get_run
from geopilot.runs.models import CheckStatus


_RuleVersion = Annotated[int, Field(strict=True, gt=0)]


class CheckAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always", allow_inf_nan=False)

    id: _Text
    run_id: _Text
    rule_versions: dict[_Text, _RuleVersion]
    created_at: _Timestamp


class CheckReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always", allow_inf_nan=False)

    id: _Text
    run_id: _Text
    status: CheckStatus
    report: dict[_Text, JsonValue]
    rule_versions: dict[_Text, _RuleVersion]
    created_at: _Timestamp

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.status is CheckStatus.CHECKING:
            raise ValueError("A published report requires a final check status")
        if self.report.get("run_id", self.run_id) != self.run_id:
            raise ValueError("The report payload must refer to the same run")
        if self.report.get("status", self.status.value) != self.status.value:
            raise ValueError("The report payload must use the same status")
        return self


def summarize_check_status(
    findings: Iterable[Mapping[str, JsonValue]], *, interrupted: bool = False,
) -> CheckStatus:
    """Use each finding's Boolean verdict and blocking or warning severity."""
    status = CheckStatus.PASSED
    for finding in findings:
        if not isinstance(finding, Mapping):
            raise ValueError("A finding must be a JSON object")
        passed = finding.get("passed")
        severity = finding.get("severity")
        if type(passed) is not bool or severity not in ("blocking", "warning"):
            raise ValueError("A finding requires a Boolean passed value and blocking or warning severity")
        if not passed:
            if severity == "blocking":
                status = CheckStatus.FAILED
            elif status is CheckStatus.PASSED:
                status = CheckStatus.WARNING
    return CheckStatus.NOT_CHECKED if interrupted else status


def _report_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _attempt_from_row(row: sqlite3.Row) -> CheckAttempt:
    return CheckAttempt(id=row["id"], run_id=row["run_id"], created_at=row["created_at"],
                        rule_versions=json.loads(row["rule_versions_json"]))


def get_active_check_attempt(connection: sqlite3.Connection, run_id: str) -> CheckAttempt | None:
    """Read the unfinished check attempt for a run, if any."""
    rows = connection.execute(
        """
        SELECT id, run_id, rule_versions_json, created_at FROM check_report
        WHERE run_id = ? AND status IN ('QUEUED', 'CHECKING') LIMIT 2
        """,
        (run_id,),
    ).fetchall()
    if len(rows) > 1:
        raise sqlite3.IntegrityError(f"Run {run_id!r} has multiple active check attempts")
    return _attempt_from_row(rows[0]) if rows else None


def publish_report(
    connection: sqlite3.Connection, report: CheckReport, *, error: ErrorDetail | None = None,
) -> CheckReport:
    """Finalize an exact attempt and update its run in the caller's transaction."""
    if not connection.in_transaction:
        raise ValueError("An active write transaction is required to publish a check report")
    report = CheckReport.model_validate(report)
    run = get_run(connection, report.run_id)
    if run is None:
        raise LookupError(f"Run {report.run_id!r} does not exist")
    if run.execution_status is not ExecutionStatus.SUCCEEDED or run.check_status is not CheckStatus.CHECKING:
        raise ValueError("The run must be SUCCEEDED and CHECKING to publish a report")
    payload = json.dumps(report.report, ensure_ascii=False, allow_nan=False)
    rule_versions = json.dumps(report.rule_versions, ensure_ascii=False, allow_nan=False)
    error_json = ErrorDetail.model_validate(error).model_dump_json() if error is not None else None
    created_at = _report_timestamp(report.created_at)
    reserved = connection.execute(
        "SELECT id, run_id, status, rule_versions_json, created_at FROM check_report WHERE id = ?", (report.id,),
    ).fetchone()
    if reserved is None:
        if get_active_check_attempt(connection, report.run_id) is not None:
            raise sqlite3.IntegrityError("The report ID must match the active check attempt")
    else:
        attempt = _attempt_from_row(reserved)
        if reserved["status"] != "CHECKING" or (
            attempt.run_id, attempt.rule_versions, attempt.created_at,
        ) != (report.run_id, report.rule_versions, report.created_at):
            raise sqlite3.IntegrityError("The report must match an unfinished CHECKING reservation")

    connection.execute("SAVEPOINT geopilot_check_report")
    try:
        if reserved is None:
            saved = connection.execute(
                """
                INSERT INTO check_report (id, run_id, status, report_json, rule_versions_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (report.id, report.run_id, report.status.value, payload, rule_versions, created_at),
            )
        else:
            saved = connection.execute(
                "UPDATE check_report SET status = ?, report_json = ? WHERE id = ? AND status = 'CHECKING'",
                (report.status.value, payload, report.id),
            )
        if saved.rowcount != 1:
            raise sqlite3.IntegrityError("The check report was not finalized")
        updated = connection.execute(
            """
            UPDATE run SET check_status = ?, current_report_id = ?, error_json = ?
            WHERE id = ? AND execution_status = 'SUCCEEDED' AND check_status = 'CHECKING'
            """,
            (report.status.value, report.id, error_json, report.run_id),
        )
        if updated.rowcount != 1:
            raise sqlite3.IntegrityError("The current check report was not updated")
    except BaseException as failure:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK TO SAVEPOINT geopilot_check_report")
                connection.execute("RELEASE SAVEPOINT geopilot_check_report")
            except sqlite3.Error as cleanup:
                failure.add_note(f"Could not roll back the check report savepoint: {cleanup}")
        raise
    connection.execute("RELEASE SAVEPOINT geopilot_check_report")
    return report


def get_current_report(connection: sqlite3.Connection, run_id: str) -> CheckReport | None:
    """Read the final report currently referenced by the run."""
    row = connection.execute(
        """
        SELECT report.id, report.run_id, report.status, report.report_json, report.rule_versions_json, report.created_at
        FROM run JOIN check_report AS report ON report.id = run.current_report_id AND report.run_id = run.id
        WHERE run.id = ? AND report.report_json IS NOT NULL
        """,
        (run_id,),
    ).fetchone()
    return _report_from_row(row) if row is not None else None


def get_report(connection: sqlite3.Connection, run_id: str, report_id: str) -> CheckReport | None:
    """Read an exact published report of this run."""
    row = connection.execute(
        """
        SELECT id, run_id, status, report_json, rule_versions_json, created_at
        FROM check_report WHERE run_id = ? AND id = ? AND report_json IS NOT NULL
        """,
        (run_id, report_id),
    ).fetchone()
    return _report_from_row(row) if row is not None else None


def _report_from_row(row: sqlite3.Row) -> CheckReport:
    return CheckReport(
        id=row["id"], run_id=row["run_id"], status=row["status"], created_at=row["created_at"],
        report=json.loads(row["report_json"]), rule_versions=json.loads(row["rule_versions_json"]),
    )


def list_report_ids(connection: sqlite3.Connection, run_id: str) -> tuple[str, ...]:
    """Read published report IDs for the run, newest first."""
    rows = connection.execute(
        """
        SELECT id FROM check_report WHERE run_id = ? AND report_json IS NOT NULL
        ORDER BY created_at DESC, id DESC
        """,
        (run_id,),
    )
    return tuple(row["id"] for row in rows)
