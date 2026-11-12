"""Reserve and claim another check of the same registered outputs."""

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter

from geopilot.artifacts.service import ARTIFACT_MANIFEST_PATH, list_artifacts
from geopilot.checks.artifact_checks import check_run_artifacts
from geopilot.checks.report_service import (
    CheckAttempt,
    _attempt_from_row,
    _report_timestamp,
    get_active_check_attempt,
    get_current_report,
    get_report,
)
from geopilot.checks.tool_checks import TOOL_CHECKS, snapshot_tool_files, tool_check_key, tool_check_scopes, tool_file_paths
from geopilot.domain.step_io import ExecutionStatus, StepFileRef
from geopilot.execution.step_support import AdapterError, resolve_step_path
from geopilot.persistence.plan_repository import plan_from_row, row_for_plan
from geopilot.persistence.run_repository import get_run
from geopilot.persistence.schema import open_database
from geopilot.plans.models import PlanStatus
from geopilot.runs.models import CheckStatus, Run


__all__ = [
    "CheckAttempt", "RecheckError", "claim_recheck", "get_active_check_attempt", "get_report",
    "request_recheck", "reserve_check_attempt", "verify_recheck_artifacts",
]
_ASSET_FILES = TypeAdapter(dict[str, StepFileRef])


class RecheckError(ValueError):
    def __init__(self, code: str, status_code: int, message: str, field: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.field = field


def _unavailable(message: str, field: str) -> RecheckError:
    return RecheckError("RECHECK_NOT_AVAILABLE", 409, message, field)


def _require_run(connection: sqlite3.Connection, run_id: str) -> Run:
    run = get_run(connection, run_id)
    if run is None:
        raise RecheckError("RUN_NOT_FOUND", 404, f"Run {run_id!r} does not exist", "run_id")
    if run.execution_status is not ExecutionStatus.SUCCEEDED:
        raise _unavailable("Recheck requires a successful execution", "execution_status")
    return run


def reserve_check_attempt(
    connection: sqlite3.Connection, run_id: str, rule_versions: Mapping[str, int], *,
    status: Literal["CHECKING", "QUEUED"] = "CHECKING",
) -> CheckAttempt:
    """Freeze a report ID and rules in the caller's write transaction."""
    if not connection.in_transaction:
        raise ValueError("An active write transaction is required to reserve a check attempt")
    if status not in {"CHECKING", "QUEUED"}:
        raise ValueError("A check attempt must be reserved as CHECKING or QUEUED")
    run = _require_run(connection, run_id)
    expected = CheckStatus.CHECKING if status == "CHECKING" else CheckStatus.NOT_CHECKED
    if run.check_status is not expected:
        raise _unavailable(f"Reserving a {status} attempt requires check status {expected.value}", "check_status")
    if get_active_check_attempt(connection, run_id) is not None:
        raise sqlite3.IntegrityError("The run already has an active check attempt")
    attempt = CheckAttempt(id=uuid4().hex, run_id=run_id, rule_versions=dict(rule_versions), created_at=datetime.now(UTC))
    inserted = connection.execute(
        """
        INSERT INTO check_report (id, run_id, status, report_json, rule_versions_json, created_at)
        VALUES (?, ?, ?, NULL, ?, ?)
        """,
        (attempt.id, attempt.run_id, status, json.dumps(attempt.rule_versions, allow_nan=False),
         _report_timestamp(attempt.created_at)),
    )
    if inserted.rowcount != 1:
        raise sqlite3.IntegrityError("The check attempt was not reserved")
    return attempt


def claim_recheck(connection: sqlite3.Connection, report_id: str) -> CheckAttempt:
    """Move a queued recheck and its run to CHECKING together."""
    if not connection.in_transaction:
        raise ValueError("An active write transaction is required to claim a recheck")
    row = connection.execute(
        """
        SELECT id, run_id, rule_versions_json, created_at
        FROM check_report WHERE id = ? AND status = 'QUEUED'
        """,
        (report_id,),
    ).fetchone()
    if row is None:
        raise _unavailable("The recheck attempt is not queued", "report_id")
    attempt = _attempt_from_row(row)
    run = _require_run(connection, attempt.run_id)
    if run.check_status is not CheckStatus.NOT_CHECKED:
        raise _unavailable("A queued recheck requires an interrupted run", "check_status")

    connection.execute("SAVEPOINT geopilot_claim_recheck")
    try:
        claimed = connection.execute(
            "UPDATE check_report SET status = 'CHECKING' WHERE id = ? AND status = 'QUEUED'", (report_id,),
        )
        updated = connection.execute(
            """
            UPDATE run SET check_status = 'CHECKING', cancel_requested = 0, error_json = NULL
            WHERE id = ? AND execution_status = 'SUCCEEDED' AND check_status = 'NOT_CHECKED'
            """,
            (run.id,),
        )
        if claimed.rowcount != 1 or updated.rowcount != 1:
            raise sqlite3.IntegrityError("The queued recheck and run could not be claimed together")
    except BaseException as error:
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK TO SAVEPOINT geopilot_claim_recheck")
                connection.execute("RELEASE SAVEPOINT geopilot_claim_recheck")
            except sqlite3.Error as cleanup:
                error.add_note(f"Could not roll back the recheck claim savepoint: {cleanup}")
        raise
    connection.execute("RELEASE SAVEPOINT geopilot_claim_recheck")
    return attempt


def _request_state(
    connection: sqlite3.Connection, run_id: str, versions: Mapping[str, int],
) -> tuple[dict[str, int], CheckAttempt | None]:
    run = _require_run(connection, run_id)
    active = get_active_check_attempt(connection, run_id)
    if run.check_status is not CheckStatus.NOT_CHECKED and not (
        run.check_status is CheckStatus.CHECKING and active is not None
    ):
        raise _unavailable("Only an interrupted check can be requested again", "check_status")
    previous = get_current_report(connection, run_id)
    if previous is None:
        raise _unavailable("The original report and rule versions are unavailable", "current_report_id")
    row = row_for_plan(connection, run.plan_id, run.plan_version)
    if row is None:
        raise _unavailable("The confirmed plan version is unavailable", "plan_version")
    plan = plan_from_row(row)
    original = previous.rule_versions
    if plan.status is not PlanStatus.CONFIRMED or not plan.has_checks or set(plan.check_rules) != original.keys():
        raise _unavailable("The original report does not identify every confirmed rule version", "rule_versions")
    if active is not None and active.rule_versions != original:
        raise _unavailable("The active attempt does not use the original rule versions", "rule_versions")
    for name in plan.check_rules:
        available = versions.get(name)
        if type(available) is not int or available != original[name]:
            raise RecheckError(
                "CHECK_RULE_VERSION_MISMATCH", 409, f"Original rule {name!r} v{original[name]} is unavailable",
                f"rule_versions.{name}",
            )
    for step, rule in tool_check_scopes(plan):
        if (rule.name, rule.version) not in TOOL_CHECKS:
            raise RecheckError(
                "CHECK_RULE_VERSION_MISMATCH", 409, f"Original tool check {rule.name!r} v{rule.version} is unavailable",
                tool_check_key(step, rule),
            )
    return original, active


def _verify_tool_files(database_path: str | Path, run_id: str) -> None:
    with open_database(database_path, read_only=True) as connection, connection:
        connection.execute("BEGIN")
        run = _require_run(connection, run_id)
        row = row_for_plan(connection, run.plan_id, run.plan_version)
        plan = plan_from_row(row)
        if not tool_check_scopes(plan):
            return
        known = {artifact.uri: artifact.fingerprint for artifact in list_artifacts(connection, run_id)}

    try:
        workdir = Path(run.workdir)
        request = resolve_step_path(workdir, "request.json", field="tool_files")
        snapshot = json.loads(request.read_text(encoding="utf-8"))
        if snapshot["run_id"] != run_id or snapshot["plan"] != plan.model_dump(mode="json"):
            raise ValueError("The run request no longer matches the confirmed plan")
        paths = tool_file_paths(plan, _ASSET_FILES.validate_python(snapshot["asset_files"]))
        manifest = resolve_step_path(workdir, ARTIFACT_MANIFEST_PATH, field="tool_files")
        original = json.loads(manifest.read_text(encoding="utf-8"))["tool_files"]
        if not isinstance(original, dict) or original.keys() != paths:
            raise ValueError("The manifest no longer identifies the confirmed tool files")
    except (AdapterError, KeyError, OSError, TypeError, ValueError) as error:
        raise RecheckError("ARTIFACT_UNREADABLE", 409, str(error), "tool_files") from error

    actual = snapshot_tool_files(workdir, paths, known_fingerprints=known)
    for relative, observed in actual.items():
        field = f"tool_files.{relative}.fingerprint"
        if observed is None or not isinstance(original[relative], str):
            raise RecheckError("ARTIFACT_UNREADABLE", 409, f"Cannot reopen the original tool file {relative!r}", field)
        if observed != original[relative]:
            raise RecheckError("ARTIFACT_CHANGED", 409, f"Tool file {relative!r} differs from its recorded fingerprint", field)


def verify_recheck_artifacts(database_path: str | Path, run_id: str) -> None:
    """Verify registered artifacts and all files used by the frozen tool checks."""
    try:
        findings = check_run_artifacts(database_path, run_id, rules={})
    except LookupError as error:
        raise RecheckError("ARTIFACT_UNREADABLE", 409, str(error), "artifacts") from error
    for finding in findings:
        if finding.passed is not True:
            code = "ARTIFACT_CHANGED" if finding.field.endswith(".fingerprint") else "ARTIFACT_UNREADABLE"
            raise RecheckError(code, 409, finding.message, finding.field)
    _verify_tool_files(database_path, run_id)


def request_recheck(
    database_path: str | Path, run_id: str, *, rule_versions: Mapping[str, int],
) -> str:
    """Verify unchanged artifacts and queue the original check rules without rerunning GIS."""
    available = dict(rule_versions)
    with open_database(database_path, read_only=True) as connection, connection:
        connection.execute("BEGIN")
        _request_state(connection, run_id, available)
    verify_recheck_artifacts(database_path, run_id)

    with open_database(database_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        original, active = _request_state(connection, run_id, available)
        if active is None:
            active = reserve_check_attempt(connection, run_id, original, status="QUEUED")
        return active.id
