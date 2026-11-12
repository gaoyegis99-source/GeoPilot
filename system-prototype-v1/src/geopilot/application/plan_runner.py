"""Run an exact confirmed plan synchronously through the local step protocol."""

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from os import PathLike
from os.path import samestat
from pathlib import Path, PurePosixPath
from shutil import rmtree
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from geopilot.application.check_evidence import check_named_artifact_rule, collect_check_findings
from geopilot.application.run_checks import RUN_CHECKS, RunCheck, RunCheckContext, RunCheckResult
from geopilot.application.run_inputs import stage_run_inputs
from geopilot.artifacts.dem_area_cache import DemAreaReuse
from geopilot.artifacts.service import (
    ARTIFACT_MANIFEST_PATH, ArtifactRegistrationError, insert_artifacts, list_artifacts, prepare_artifacts,
)
from geopilot.artifacts.step_cache import StepCache
from geopilot.checks.artifact_checks import ArtifactRule
from geopilot.checks.attempt_reports import CHECK_REPORT_PATH, publish_unfinished_attempt
from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.checks.process_checks import _json, _read_text
from geopilot.checks.recheck_service import RecheckError, reserve_check_attempt, verify_recheck_artifacts
from geopilot.checks.report_service import CheckAttempt, CheckReport, publish_report, summarize_check_status
from geopilot.checks.tool_checks import (
    TOOL_CHECKS, ToolCheckContext, ToolFinding, snapshot_tool_files, tool_check_key, tool_check_scopes, tool_file_paths,
)
from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import ExecutionStatus, StepFileRef, StepOutput, StepRequest, StepResult
from geopilot.execution.step_support import PreparedStep, resolve_step_path
from geopilot.observability.telemetry import record_run
from geopilot.persistence.asset_repository import asset_for_ref
from geopilot.persistence.run_repository import get_run, list_run_steps, update_run
from geopilot.persistence.schema import open_database
from geopilot.persistence.workspace import RunWorkspace, create_run_workspace
from geopilot.plans.compiler import PlanCompilationError, _same_json, compile_step_request
from geopilot.plans.models import AnalysisPlan, PlanStatus, PlanStep
from geopilot.plans.service import load_plan
from geopilot.runs.models import CheckStatus, Run
from geopilot.scheduler.cancellation import cancellation_error
from geopilot.scheduler.step_executor import execute_steps
from geopilot.tools.registry import ToolRegistry

_CheckStatus = Literal["NOT_CHECKED", "CHECKING", "PASSED", "WARNING", "FAILED"]
_ASSET_FILES = TypeAdapter(dict[str, StepFileRef])


@dataclass(frozen=True)
class PlanRunResult:
    run_id: str
    workdir: Path
    execution_status: ExecutionStatus
    check_status: _CheckStatus
    steps: dict[str, StepResult]
    artifacts: dict[str, StepOutput]
    error: ErrorDetail | None = None


def _write_json(path: Path, data: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _record_run_state(
    database_path: str | Path, run_id: str, execution: ExecutionStatus,
    check: _CheckStatus, error: ErrorDetail | None = None,
) -> None:
    with open_database(database_path) as connection, connection:
        connection.execute(
            "UPDATE run SET execution_status = ?, check_status = ?, error_json = ? WHERE id = ?",
            (execution.value, check, error.model_dump_json() if error is not None else None, run_id),
        )


def _run_error(
    code: str, phase: str, step_key: str, field: str, message: str, *, observed: JsonValue = None,
) -> ErrorDetail:
    return ErrorDetail(
        code=code, phase=phase, step_key=step_key, field=field, message=message,
        suggestion="Review the confirmed plan, run files and process logs.", log_path=None, observed=observed,
    )


def _check_record_paths(plan: AnalysisPlan) -> None:
    reserved = (PurePosixPath(ARTIFACT_MANIFEST_PATH), PurePosixPath(CHECK_REPORT_PATH))
    for step in plan.steps:
        for role, output in step.outputs.items():
            target = PurePosixPath(output.target)
            if any(path == target or path in target.parents or target in path.parents for path in reserved):
                raise PlanCompilationError(
                    "OUTPUT_TARGET_CONFLICT", f"steps.{step.key}.outputs.{role}.target",
                    f"Output {output.target!r} conflicts with a run record",
                )


def _register_artifacts(
    database_path: str | Path, run_id: str, plan: AnalysisPlan, workdir: Path, results: Mapping[str, StepResult],
    *, asset_files: Mapping[str, StepFileRef], rule_versions: dict[str, int],
) -> tuple[dict[str, StepOutput], ErrorDetail | None, CheckAttempt | None]:
    prepared = prepare_artifacts(run_id, plan, workdir, results)
    document = prepared.manifest
    if tool_check_scopes(plan):
        known = {item["path"]: item["fingerprint"] for item in document["artifacts"].values()}
        with open_database(database_path, read_only=True) as connection:
            for role, reference in plan.inputs.items():
                asset = asset_for_ref(connection, reference)
                if asset is not None:
                    known[asset_files[role].path] = asset.fingerprint
        document["tool_files"] = snapshot_tool_files(workdir, tool_file_paths(plan, asset_files), known_fingerprints=known)
    manifest = resolve_step_path(workdir, ARTIFACT_MANIFEST_PATH, field="artifacts.manifest")
    identity = None
    try:
        with TemporaryDirectory(prefix=".manifest-", dir=manifest.parent, ignore_cleanup_errors=True) as directory:
            staged = Path(directory) / manifest.name
            _write_json(staged, document)
            with open_database(database_path) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                run = get_run(connection, run_id)
                if run.cancel_requested:
                    failure = cancellation_error(run.current_step)
                    update_run(connection, run.model_copy(update={
                        "execution_status": ExecutionStatus.CANCELLED, "error": failure,
                    }))
                    return {}, failure, None
                insert_artifacts(connection, prepared)
                identity = staged.stat()
                manifest.hardlink_to(staged)
                update_run(connection, run.model_copy(update={
                    "execution_status": ExecutionStatus.SUCCEEDED, "check_status": CheckStatus.CHECKING,
                }))
                attempt = reserve_check_attempt(connection, run_id, rule_versions)
    except BaseException as error:
        if identity is not None:
            try:
                if samestat(identity, manifest.lstat()):
                    manifest.unlink()
            except FileNotFoundError:
                pass
            except OSError as cleanup:
                error.add_note(f"Could not remove the unpublished artifact manifest: {cleanup}")
        raise
    return prepared.outputs, None, attempt


def _write_check_report(workdir: Path, report: dict) -> None:
    target = resolve_step_path(workdir, CHECK_REPORT_PATH, field="checks.report")
    identity = None
    try:
        with TemporaryDirectory(prefix=".report-", dir=target.parent, ignore_cleanup_errors=True) as directory:
            staged = Path(directory) / target.name
            _write_json(staged, report)
            identity = staged.stat()
            target.hardlink_to(staged)
    except KeyboardInterrupt as error:
        if identity is not None:
            try:
                # The link may exist even when the publication call was interrupted.
                if samestat(identity, target.lstat()):
                    target.unlink()
            except FileNotFoundError:
                pass
            except OSError as cleanup:
                error.add_note(f"Could not remove the interrupted check report: {cleanup}")
        raise


def _check_cancel_reason(field: str) -> dict[str, str]:
    return {"code": "CHECK_CANCELLED", "field": field, "message": "The check was cancelled by request."}


def _check_cancel_requested(database_path: str | Path, run_id: str) -> bool:
    with open_database(database_path, read_only=True) as connection:
        return get_run(connection, run_id).cancel_requested


def _finish_check_report(
    database_path: str | Path, context: RunCheckContext, report: dict, versions: dict[str, int],
    created_at: datetime, *, write_compatibility_copy: bool,
) -> tuple[_CheckStatus, ErrorDetail | None]:
    run_id, report_id = report["run_id"], report["report_id"]
    compatibility_copy_written = False
    try:
        with open_database(database_path) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            run = get_run(connection, run_id)
            reason = report["interruption"]
            if run.cancel_requested and (reason is None or reason["code"] != "CHECK_CANCELLED"):
                previous = reason
                reason = _check_cancel_reason("checks.report")
                report["status"] = "NOT_CHECKED"
                report["interruption"] = reason if previous is None else {**reason, "previous": previous}
            if write_compatibility_copy:
                try:
                    _write_check_report(context.workdir, report)
                    compatibility_copy_written = True
                except (OSError, TypeError, ValueError, KeyboardInterrupt) as error:
                    previous = reason
                    if isinstance(error, KeyboardInterrupt):
                        detail = " ".join((str(error) or type(error).__name__, *getattr(error, "__notes__", ())))
                        reason = {"code": "CHECK_INTERRUPTED", "field": "checks.report",
                                  "message": f"Report publication was interrupted: {detail}"}
                    else:
                        reason = {"code": "STORAGE_FAILED", "field": "checks.report",
                                  "message": f"Cannot save the check report: {error}"}
                    report["status"] = "NOT_CHECKED"
                    report["interruption"] = reason if previous is None else {**reason, "previous": previous}
            failure = None
            if reason is not None:
                failure = _run_error(
                    reason["code"], "check", reason.get("step_key", context.plan.steps[-1].key),
                    reason["field"], reason["message"],
                    observed=report,
                )
            elif report["status"] == "FAILED":
                method_blocked = any(
                    rule["passed"] is False and rule["severity"] == "blocking" for rule in report["rules"].values()
                )
                tool_failure = None if method_blocked else next((
                    finding for finding in report["phase_findings"]["artifact"]
                    if "rule_version" in finding and finding["passed"] is False
                ), None)
                failure = _run_error(
                    "CHECK_BLOCKED", "check", tool_failure["step_key"] if tool_failure else context.plan.steps[-1].key,
                    tool_failure["field"] if tool_failure else "checks.report",
                    tool_failure["message"] if tool_failure else "At least one blocking check failed.",
                    observed={"report_id": report_id},
                )
            publish_report(connection, CheckReport(
                id=report_id, run_id=run_id, status=CheckStatus(report["status"]), report=report,
                rule_versions=versions, created_at=created_at,
            ), error=failure)
    except (OSError, sqlite3.Error, ValueError) as error:
        if compatibility_copy_written:
            try:
                target = resolve_step_path(context.workdir, CHECK_REPORT_PATH, field="checks.report")
                target.unlink(missing_ok=True)
            except (OSError, ValueError) as cleanup:
                error.add_note(f"Could not remove the unpublished check report: {cleanup}")
        reason = {"code": "STORAGE_FAILED", "field": "checks.report", "message": f"Cannot publish the check report: {error}"}
        report["status"] = "NOT_CHECKED"
        report["interruption"] = reason
        failure = _run_error(
            reason["code"], "check", context.plan.steps[-1].key, reason["field"], reason["message"], observed=report,
        )
        with open_database(database_path) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM check_report WHERE id = ? AND run_id = ? AND status = 'CHECKING' AND report_json IS NULL",
                (report_id, run_id),
            )
            run = get_run(connection, run_id)
            update_run(connection, run.model_copy(update={"check_status": CheckStatus.NOT_CHECKED, "error": failure}))
    return report["status"], failure


def _step_check_context(context: RunCheckContext, step: PlanStep, run_id: str) -> ToolCheckContext | ToolFinding:
    field = f"steps.{step.key}.request"
    expectation = "Stored request matches the confirmed step and input bindings"
    try:
        expected = compile_step_request(
            context.plan, step.key, run_id=run_id, asset_files=context.asset_files, prior_results=context.steps,
        )
        result = context.steps[step.key]
        if result.log_path != expected.log_path:
            raise ValueError("The process log does not match the confirmed step")
        relative = PurePosixPath(result.log_path).with_name("request.json").as_posix()
        recorded = StepRequest.model_validate(_json(_read_text(context.workdir, relative, field)))
        if not _same_json(recorded.model_dump(mode="json"), expected.model_dump(mode="json")):
            raise ValueError("The stored tool request differs from the confirmed step")
    except (OSError, ValueError) as error:
        return ToolFinding(field, False, None, expectation, str(error))
    return ToolCheckContext(context, step, recorded, result)


def _check_step_rules(
    database_path: str | Path, run_id: str, context: RunCheckContext,
    phase_findings: dict[str, list[dict]], reason: dict | None,
) -> tuple[dict, dict | None]:
    scopes = tool_check_scopes(context.plan)
    if not scopes:
        return {}, reason
    verdicts = {}
    prepared = {}
    for step, rule in scopes:
        if reason is not None:
            break
        key = tool_check_key(step, rule)
        if _check_cancel_requested(database_path, run_id):
            reason = {**_check_cancel_reason(key), "step_key": step.key}
            break
        checker = TOOL_CHECKS.get((rule.name, rule.version))
        if checker is None:
            reason = {"code": "CHECK_RULE_VERSION_MISMATCH", "field": key, "step_key": step.key,
                      "message": f"Tool check {rule.name!r} v{rule.version} is unavailable"}
            break
        try:
            if step.key not in prepared:
                prepared[step.key] = _step_check_context(context, step, run_id)
            scoped = prepared[step.key]
            found = (scoped,) if isinstance(scoped, ToolFinding) else checker(scoped)
            identity = {"step_key": step.key, "tool": step.tool.model_dump(mode="json"),
                        "rule": rule.name, "rule_version": rule.version}
            if not found:
                verdict = {"field": key, "passed": False, "observed": None, "expected": "Applicable step output",
                           "message": "The tool check has no applicable output"}
                findings = [{**identity, **verdict, "phase": "artifact", "severity": "blocking"}]
            else:
                findings = [{**identity, **asdict(item), "phase": "artifact", "severity": "blocking"} for item in found]
            verdicts[key] = {**identity, "passed": all(item["passed"] for item in findings), "severity": "blocking"}
            phase_findings["artifact"].extend(findings)
        except (Exception, KeyboardInterrupt) as error:
            detail = str(error) or type(error).__name__
            reason = {"code": "CHECK_INTERRUPTED", "field": key, "step_key": step.key,
                      "message": f"Tool check {rule.name!r} was interrupted: {detail}"}
            break
    return {
        "tool_rules": verdicts, "completed_tool_rules": list(verdicts),
        "pending_tool_rules": [key for step, rule in scopes if (key := tool_check_key(step, rule)) not in verdicts],
    }, reason


def _check_results(
    database_path: str | Path, run_id: str, context: RunCheckContext, checks: Mapping[str, RunCheck],
    *, registry: ToolRegistry | None = None, qgis_bindings: Mapping | None = None,
    python_implementations: Mapping | None = None, artifact_rules: Mapping[str, ArtifactRule] | None = None,
    rule_versions: Mapping[str, int] | None = None,
    attempt: CheckAttempt | None = None, write_compatibility_copy: bool = True,
) -> tuple[_CheckStatus, ErrorDetail | None]:
    planned = context.plan.check_rules
    versions = attempt.rule_versions if attempt is not None else _rule_versions(planned, rule_versions)
    report_id = attempt.id if attempt is not None else uuid4().hex
    created_at = attempt.created_at if attempt is not None else datetime.now(UTC)
    completed: list[str] = []
    verdicts: dict[str, dict] = {}
    reason = None
    phase_findings = {"input": [], "process": [], "artifact": []}
    if registry is None:
        _record_run_state(database_path, run_id, ExecutionStatus.SUCCEEDED, "CHECKING")
    elif _check_cancel_requested(database_path, run_id):
        reason = _check_cancel_reason("phase_findings")
    else:
        try:
            phase_findings = collect_check_findings(
                database_path, run_id, context, registry=registry, qgis_bindings=qgis_bindings,
                python_implementations=python_implementations,
            )
        except (Exception, KeyboardInterrupt) as error:
            detail = str(error) or type(error).__name__
            reason = {"code": "CHECK_INTERRUPTED", "field": "phase_findings", "message": detail}

    artifact_rules = artifact_rules or {}
    for index, name in enumerate(planned):
        if reason is not None:
            break
        field = f"check_rules.{index}"
        if _check_cancel_requested(database_path, run_id):
            reason = _check_cancel_reason(field)
            break
        try:
            if name in artifact_rules:
                verdict, findings = check_named_artifact_rule(database_path, run_id, name, artifact_rules[name])
                verdicts[name] = verdict
                phase_findings["artifact"].extend(findings)
            elif name in checks:
                outcome = checks[name](context)
                if (not isinstance(outcome, RunCheckResult) or type(outcome.passed) is not bool
                        or not isinstance(outcome.message, str) or outcome.severity not in {"blocking", "warning"}):
                    raise TypeError("Checker must return a RunCheckResult with a Boolean verdict, message and severity")
                verdicts[name] = json.loads(json.dumps(asdict(outcome), ensure_ascii=False, allow_nan=False))
            else:
                reason = {"code": "UNKNOWN_CHECK_RULE", "field": field, "message": f"Check {name!r} is not registered"}
                break
        except (Exception, KeyboardInterrupt) as error:
            detail = str(error) or type(error).__name__
            reason = {"code": "CHECK_INTERRUPTED", "field": field, "message": f"Check {name!r} was interrupted: {detail}"}
            break
        if verdicts[name]["passed"] is not None:
            completed.append(name)

    tool_report, reason = _check_step_rules(database_path, run_id, context, phase_findings, reason)
    all_findings = [*verdicts.values(), *(item for group in phase_findings.values() for item in group)]
    check_status = summarize_check_status(
        (item for item in all_findings if item["passed"] is not None), interrupted=reason is not None,
    ).value
    report = {
        "report_id": report_id, "run_id": run_id, "status": check_status, "rules": verdicts,
        "completed_rules": completed, "pending_rules": [name for name in planned if name not in completed],
        "interruption": reason, "phase_findings": phase_findings,
        **tool_report,
    }
    return _finish_check_report(
        database_path, context, report, versions, created_at, write_compatibility_copy=write_compatibility_copy,
    )


def _rule_versions(names: tuple[str, ...], configured: Mapping[str, int] | None) -> dict[str, int]:
    return {name: (configured or {}).get(name, 1) for name in names}


def run_confirmed_plan(
    database_path: str | Path,
    plan_id: str,
    version: int,
    *,
    workspace_root: str | Path,
    run_id: str,
    registry: ToolRegistry,
    qgis_bindings: Mapping | None = None,
    python_implementations: Mapping[str, str | Path] | None = None,
    qgis_process: str | PathLike[str] | None = None,
    check_rules: Mapping[str, RunCheck] | None = None,
    artifact_rules: Mapping[str, ArtifactRule] | None = None,
    rule_versions: Mapping[str, int] | None = None,
    input_preflight: Callable[[PreparedStep], None] | None = None,
    timeout: float = 60,
) -> PlanRunResult:
    """Load a confirmed snapshot, run its declared steps and check actual artifacts."""
    plan = load_plan(database_path, plan_id, version)
    if plan.status is not PlanStatus.CONFIRMED:
        raise PlanCompilationError("PLAN_NOT_CONFIRMED", "status", "Confirm this version before running")
    if not plan.has_checks:
        raise PlanCompilationError("MISSING_CHECK_RULES", "check_rules", "The confirmed plan requires check rules")
    _check_record_paths(plan)
    workspace = create_run_workspace(workspace_root, plan.task_id, run_id)
    workdir = workspace.path
    try:
        asset_files = stage_run_inputs(database_path, plan.inputs, workdir)
        _write_json(workspace.request_path, {
            "run_id": run_id,
            "plan": plan.model_dump(mode="json"),
            "asset_files": {role: ref.model_dump(mode="json") for role, ref in asset_files.items()},
        })
        with open_database(database_path) as connection, connection:
            connection.execute(
                """
                INSERT INTO run (id, plan_id, plan_version, execution_status, check_status, workdir, created_at)
                VALUES (?, ?, ?, 'RUNNING', 'NOT_CHECKED', ?, ?)
                """,
                (run_id, plan.id, plan.version, str(workdir), datetime.now(UTC).isoformat()),
            )
    except BaseException as error:
        try:
            rmtree(workdir)
        except OSError as cleanup:
            error.add_note(f"Could not remove incomplete run directory {workdir}: {cleanup}")
        raise

    with record_run(workdir, run_id) as telemetry:
        results = execute_steps(
            database_path, workspace, plan, asset_files, registry=registry, qgis_bindings=qgis_bindings,
            python_implementations=python_implementations, qgis_process=qgis_process, timeout=timeout,
            input_preflight=input_preflight,
        )
        if telemetry is not None:
            telemetry.steps_finished()
        result = _finish_run(
            database_path, run_id, workdir, plan, asset_files, results, check_rules, registry=registry,
            qgis_bindings=qgis_bindings, python_implementations=python_implementations, artifact_rules=artifact_rules,
            rule_versions=rule_versions,
        )
        if telemetry is not None:
            telemetry.complete(result.execution_status.value, result.check_status)
        return result


def execute_claimed_run(
    database_path: str | Path,
    run: Run,
    *,
    workspace_root: str | Path,
    registry: ToolRegistry,
    qgis_bindings: Mapping | None = None,
    python_implementations: Mapping[str, str | Path] | None = None,
    qgis_process: str | PathLike[str] | None = None,
    check_rules: Mapping[str, RunCheck] | None = None,
    artifact_rules: Mapping[str, ArtifactRule] | None = None,
    rule_versions: Mapping[str, int] | None = None,
    input_preflight: Callable[[PreparedStep], None] = preflight_step_inputs,
    timeout: float = 60,
    step_cache: StepCache | None = None,
) -> PlanRunResult:
    """Execute the staged snapshot published by run creation."""
    plan = load_plan(database_path, run.plan_id, run.plan_version)
    if not plan.has_checks:
        raise PlanCompilationError("MISSING_CHECK_RULES", "check_rules", "The confirmed plan requires check rules")
    workspace = RunWorkspace(Path(workspace_root).resolve(), plan.task_id, run.id)
    workdir = workspace.path
    if workdir != Path(run.workdir) or workdir.resolve(strict=True) != workdir:
        raise ValueError("Claimed run workspace differs from its expected path")
    _check_record_paths(plan)
    asset_files = _read_run_snapshot(workdir, plan, run.id)
    reuse = DemAreaReuse.for_plan(step_cache, database_path, plan, python_implementations or {}) if step_cache else None
    hooks = {} if reuse is None else {"restore": reuse.restore, "capture": reuse.capture}
    with record_run(workdir, run.id, created_at=run.created_at) as telemetry:
        results = execute_steps(
            database_path, workspace, plan, asset_files, registry=registry, qgis_bindings=qgis_bindings,
            python_implementations=python_implementations, qgis_process=qgis_process, timeout=timeout,
            input_preflight=input_preflight, **hooks,
        )
        if telemetry is not None:
            telemetry.steps_finished()
        result = _finish_run(
            database_path, run.id, workdir, plan, asset_files, results, check_rules, registry=registry,
            qgis_bindings=qgis_bindings, python_implementations=python_implementations, artifact_rules=artifact_rules,
            rule_versions=rule_versions,
        )
        if telemetry is not None:
            telemetry.complete(result.execution_status.value, result.check_status)
        if reuse is not None and result.execution_status is ExecutionStatus.SUCCEEDED and result.check_status == "PASSED":
            reuse.publish()
        return result


def _read_run_snapshot(workdir: Path, plan: AnalysisPlan, run_id: str) -> dict[str, StepFileRef]:
    snapshot_path = resolve_step_path(workdir, "request.json", field="workdir_ref")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot_plan = AnalysisPlan.model_validate(snapshot["plan"])
    if snapshot["run_id"] != run_id or snapshot_plan.model_dump_json() != plan.model_dump_json():
        raise ValueError("Run request differs from its confirmed plan")
    asset_files = _ASSET_FILES.validate_python(snapshot["asset_files"])
    if asset_files.keys() != plan.inputs.keys():
        raise ValueError("Run request does not contain the confirmed input roles")
    return asset_files


def execute_claimed_recheck(
    database_path: str | Path, run: Run, attempt: CheckAttempt, *, registry: ToolRegistry,
    qgis_bindings: Mapping | None = None, python_implementations: Mapping[str, str | Path] | None = None,
    check_rules: Mapping[str, RunCheck] | None = None, artifact_rules: Mapping[str, ArtifactRule] | None = None,
    rule_versions: Mapping[str, int] | None = None,
) -> None:
    """Recheck a completed run using its original versioned rules and files."""
    if (run.id != attempt.run_id or run.execution_status is not ExecutionStatus.SUCCEEDED
            or run.check_status is not CheckStatus.CHECKING):
        raise ValueError("Recheck does not match the claimed run")
    checks = RUN_CHECKS if check_rules is None else check_rules
    try:
        plan = load_plan(database_path, run.plan_id, run.plan_version)
        current = _rule_versions(plan.check_rules, rule_versions)
        if current != attempt.rule_versions or current.keys() - (checks.keys() | (artifact_rules or {}).keys()):
            raise RecheckError(
                code="CHECK_RULE_VERSION_MISMATCH", status_code=409,
                message="The original rule versions are unavailable.", field="check_rules",
            )
        for step, rule in tool_check_scopes(plan):
            if (rule.name, rule.version) not in TOOL_CHECKS:
                raise RecheckError(
                    code="CHECK_RULE_VERSION_MISMATCH", status_code=409,
                    message=f"Tool check {rule.name!r} v{rule.version} is unavailable.", field=tool_check_key(step, rule),
                )
        verify_recheck_artifacts(database_path, run.id)
        workdir = Path(run.workdir)
        asset_files = _read_run_snapshot(workdir, plan, run.id)
        with open_database(database_path, read_only=True) as connection, connection:
            connection.execute("BEGIN")
            steps = {step.step_key: step.result for step in list_run_steps(connection, run.id)}
            artifacts = {record.role: record.output for record in list_artifacts(connection, run.id)}
        if set(steps) != {step.key for step in plan.steps} or any(
            result is None or result.status is not ExecutionStatus.SUCCEEDED for result in steps.values()
        ):
            raise ValueError("Stored steps do not establish a completed run")
    except (OSError, ValueError, LookupError) as error:
        failure = _run_error(
            getattr(error, "code", "CHECK_INTERRUPTED"), "check", run.current_step or "run",
            getattr(error, "field", "checks.report"), str(error),
        )
        with open_database(database_path) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            publish_unfinished_attempt(connection, attempt, failure)
        return
    _check_results(
        database_path, run.id, RunCheckContext(plan, workdir, asset_files, steps, artifacts), checks,
        registry=registry, qgis_bindings=qgis_bindings, python_implementations=python_implementations,
        artifact_rules=artifact_rules, attempt=attempt, write_compatibility_copy=False,
    )


def _finish_run(
    database_path: str | Path, run_id: str, workdir: Path, plan: AnalysisPlan,
    asset_files: dict[str, StepFileRef], results: dict[str, StepResult], check_rules: Mapping[str, RunCheck] | None,
    *, registry: ToolRegistry, qgis_bindings: Mapping | None, python_implementations: Mapping | None,
    artifact_rules: Mapping[str, ArtifactRule] | None, rule_versions: Mapping[str, int] | None,
) -> PlanRunResult:
    last = results[next(reversed(results))] if results else None
    if last is not None and last.status is not ExecutionStatus.SUCCEEDED:
        _record_run_state(database_path, run_id, last.status, "NOT_CHECKED", last.error)
        return PlanRunResult(run_id, workdir, last.status, "NOT_CHECKED", results, {}, last.error)
    if len(results) != len(plan.steps):
        failure = cancellation_error(last.step_key if last is not None else None)
        _record_run_state(database_path, run_id, ExecutionStatus.CANCELLED, "NOT_CHECKED", failure)
        return PlanRunResult(run_id, workdir, ExecutionStatus.CANCELLED, "NOT_CHECKED", results, {}, failure)

    try:
        artifacts, failure, attempt = _register_artifacts(
            database_path, run_id, plan, workdir, results, asset_files=asset_files,
            rule_versions=_rule_versions(plan.check_rules, rule_versions),
        )
    except (OSError, sqlite3.Error, ValueError) as error:
        if isinstance(error, ArtifactRegistrationError):
            code, field, step_key = error.code, error.field, error.step_key or plan.steps[-1].key
        else:
            code, field, step_key = "STORAGE_FAILED", "expected_artifacts", plan.steps[-1].key
        failure = _run_error(code, "execute", step_key, field, str(error))
        _record_run_state(database_path, run_id, ExecutionStatus.FAILED, "NOT_CHECKED", failure)
        return PlanRunResult(run_id, workdir, ExecutionStatus.FAILED, "NOT_CHECKED", results, {}, failure)
    if failure is not None:
        return PlanRunResult(run_id, workdir, ExecutionStatus.CANCELLED, "NOT_CHECKED", results, {}, failure)

    context = RunCheckContext(plan, workdir, asset_files, results, artifacts)
    check_status, failure = _check_results(
        database_path, run_id, context, RUN_CHECKS if check_rules is None else check_rules,
        registry=registry, qgis_bindings=qgis_bindings, python_implementations=python_implementations,
        artifact_rules=artifact_rules, attempt=attempt,
    )
    return PlanRunResult(run_id, workdir, ExecutionStatus.SUCCEEDED, check_status, results, artifacts, failure)
