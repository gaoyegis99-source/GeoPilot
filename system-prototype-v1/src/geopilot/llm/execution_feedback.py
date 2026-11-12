"""Explain committed execution evidence and build a bounded, redacted prompt."""

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from stat import S_ISREG
from typing import Literal

from pydantic import BaseModel, ConfigDict

from geopilot.checks.report_service import CheckReport
from geopilot.domain.step_io import ExecutionStatus
from geopilot.runs.models import CheckStatus, Run, RunStep


_LOG_TAIL_LIMIT = 8192
_EXIT_LINE = re.compile(rb"^exit_code: (-?\d{1,10})\r?$", re.MULTILINE)
_EXPLANATIONS = {
    "PROCESS_TIMEOUT": "The worker exceeded its execution time limit.",
    "PROCESS_CANCELLED": "The active worker was stopped after a cancellation request.",
    "RUN_CANCELLED": "The stored run was cancelled by request.",
    "RUN_INTERRUPTED": "The execution was interrupted before it could complete.",
    "CHECK_INTERRUPTED": "The result checks were interrupted.",
    "CHECK_CANCELLED": "The result checks were cancelled.",
    "PROCESS_FAILED": "The worker process reported a failure.",
    "STORAGE_FAILED": "The run reported a storage error.",
}
_INFERENCES = {
    "PROCESS_TIMEOUT": "Input size or resource pressure may have contributed to the timeout.",
    "STORAGE_FAILED": "A lock or unavailable storage may have contributed to the error.",
}


class _FeedbackModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeedbackStep(_FeedbackModel):
    step_key: str
    position: int
    status: ExecutionStatus
    error_code: str | None
    exit_code: int | None
    warning_count: int
    log_available: bool
    recorded_exit_code: int | None


class FeedbackCheck(_FeedbackModel):
    index: int
    source: Literal["method", "tool", "input", "process", "artifact"]
    rule: str
    step_key: str | None
    passed: bool | None
    severity: Literal["blocking", "warning"]


class FeedbackStatement(_FeedbackModel):
    kind: Literal["fact", "inference"]
    text: str
    step_key: str | None = None
    error_code: str | None = None
    check_index: int | None = None


class ExecutionFeedback(_FeedbackModel):
    schema_version: Literal[1] = 1
    run_id: str
    execution_status: ExecutionStatus
    check_status: CheckStatus
    check_report_id: str | None
    run_error_code: str | None
    steps: tuple[FeedbackStep, ...]
    findings: tuple[FeedbackCheck, ...]
    statements: tuple[FeedbackStatement, ...]


def _summarize_log(path: Path | None) -> tuple[bool, int | None]:
    if path is None:
        return False, None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            if not S_ISREG(meta.st_mode):
                return False, None
            stream.seek(max(0, meta.st_size - _LOG_TAIL_LIMIT))
            matches = _EXIT_LINE.findall(stream.read(_LOG_TAIL_LIMIT))
    except OSError:
        return False, None
    return True, int(matches[-1]) if matches else None


def _summarize_steps(steps: Iterable[RunStep], logs: Mapping[str, Path | None]) -> tuple[FeedbackStep, ...]:
    evidence = []
    for step in steps:
        result = step.result
        log_available, recorded = _summarize_log(logs.get(step.step_key))
        exit_code = result.exit_code if result is not None else None
        evidence.append(FeedbackStep(
            step_key=step.step_key, position=step.position, status=step.status,
            exit_code=exit_code, warning_count=len(result.warnings) if result is not None else 0,
            error_code=result.error.code if result is not None and result.error else None,
            log_available=log_available, recorded_exit_code=recorded if recorded == exit_code else None,
        ))
    return tuple(evidence)


def _findings(report: CheckReport | None) -> tuple[FeedbackCheck, ...]:
    if report is None:
        return ()
    items = []

    def add(source, rule, finding):
        if not isinstance(rule, str) or not rule or not isinstance(finding, dict):
            return
        passed, severity = finding.get("passed"), finding.get("severity")
        if (passed is not None and type(passed) is not bool) or severity not in ("blocking", "warning"):
            return
        step = finding.get("step_key")
        items.append(FeedbackCheck(
            index=len(items), source=source, rule=rule, step_key=step if isinstance(step, str) else None,
            passed=passed, severity=severity,
        ))

    for name, source in (("rules", "method"), ("tool_rules", "tool")):
        section = report.report.get(name)
        if isinstance(section, dict):
            for rule, finding in section.items():
                add(source, rule, finding)
    phases = report.report.get("phase_findings")
    if isinstance(phases, dict):
        for phase in ("input", "process", "artifact"):
            group = phases.get(phase)
            if isinstance(group, list):
                for finding in group:
                    if isinstance(finding, dict):
                        add(phase, finding.get("rule"), finding)
    return tuple(items)


def build_execution_feedback(
    run: Run, steps: Iterable[RunStep], report: CheckReport | None, *, logs: Mapping[str, Path | None],
) -> ExecutionFeedback:
    """Keep one run's persisted facts separate from potential explanations."""
    steps = tuple(steps)
    if any(step.run_id != run.id for step in steps) or (
        report is not None and (report.run_id, report.id) != (run.id, run.current_report_id)
    ):
        raise ValueError("Feedback evidence does not belong to the selected run and report")
    summaries = _summarize_steps(steps, logs)
    checks = _findings(report)
    errors = [(step.error_code, step.step_key) for step in summaries if step.error_code is not None]
    if run.error is not None and (run.error.code, run.error.step_key) not in errors:
        errors.append((run.error.code, run.error.step_key))
    statements = []
    for code, step in errors:
        statements.append(FeedbackStatement(
            kind="fact", error_code=code, step_key=step,
            text=_EXPLANATIONS.get(code, "The stored run or step reports this error."),
        ))
        if code in _INFERENCES:
            statements.append(FeedbackStatement(kind="inference", error_code=code, step_key=step, text=_INFERENCES[code]))
    for check in checks:
        if check.passed is False:
            statements.append(FeedbackStatement(
                kind="fact", check_index=check.index, step_key=check.step_key,
                text="The saved check did not pass.",
            ))
    if not statements and run.execution_status in (ExecutionStatus.FAILED, ExecutionStatus.CANCELLED):
        statements.append(FeedbackStatement(
            kind="fact", text=f"The stored run status is {run.execution_status.value}.",
        ))
    return ExecutionFeedback(
        run_id=run.id, execution_status=run.execution_status, check_status=run.check_status,
        check_report_id=report.id if report is not None else None,
        run_error_code=run.error.code if run.error is not None else None,
        steps=summaries, findings=checks, statements=tuple(statements),
    )


def build_feedback_prompt(feedback: ExecutionFeedback) -> dict:
    """Create prompt data without raw logs, identifiers, paths or free-form findings."""
    def safe_code(code):
        return code if code in _EXPLANATIONS else "OTHER_ERROR" if code is not None else None

    return {
        "schema_version": 1,
        "instruction": (
            "Explain the recorded execution and checks. Cite step positions and check indexes. "
            "Label potential causes as inferences. Use only these facts and never propose modifying outputs."
        ),
        "facts": {
            "execution_status": feedback.execution_status.value, "check_status": feedback.check_status.value,
            "run_error_code": safe_code(feedback.run_error_code),
            "steps": [{
                "position": step.position, "status": step.status.value, "exit_code": step.exit_code,
                "error_code": safe_code(step.error_code), "log_available": step.log_available,
                "warning_count": step.warning_count,
            } for step in feedback.steps[:32]],
            "findings": [{
                "index": check.index, "source": check.source, "severity": check.severity, "passed": check.passed,
            } for check in feedback.findings[:32]],
        },
    }
