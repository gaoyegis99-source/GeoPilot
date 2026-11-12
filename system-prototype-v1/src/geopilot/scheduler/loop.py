"""Claim and dispatch one local run at a time."""

import logging
import math
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from geopilot.artifacts.service import ARTIFACT_MANIFEST_PATH
from geopilot.checks.attempt_reports import CHECK_REPORT_PATH, publish_unfinished_attempt
from geopilot.checks.recheck_service import claim_recheck
from geopilot.checks.report_service import CheckAttempt, get_active_check_attempt
from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import ExecutionStatus, StepResult
from geopilot.execution.step_support import resolve_step_path
from geopilot.persistence.run_repository import get_run, list_run_steps, save_run_step, update_run
from geopilot.persistence.schema import open_database
from geopilot.runs.models import CheckStatus, Run, RunStep


_LOGGER = logging.getLogger(__name__)


def _executing_ids(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(row["id"] for row in connection.execute("""
        SELECT id FROM run
        WHERE execution_status = 'RUNNING'
           OR (execution_status = 'SUCCEEDED' AND check_status = 'CHECKING')
        ORDER BY created_at, id
    """))


def claim_next_run(database_path: str | Path) -> Run | None:
    """Atomically claim the oldest queued run when no work is executing."""
    with open_database(database_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if _executing_ids(connection):
            return None
        row = connection.execute("""
            SELECT id FROM run WHERE execution_status = 'NOT_STARTED'
            ORDER BY created_at, id LIMIT 1
        """).fetchone()
        if row is None:
            return None
        return _claim_run(connection, row["id"])


def _claim_run(connection: sqlite3.Connection, run_id: str) -> Run:
    run = get_run(connection, run_id)
    return update_run(connection, run.model_copy(update={"execution_status": ExecutionStatus.RUNNING}))


def _claim_next_work(database_path: str | Path) -> tuple[Run, CheckAttempt | None] | None:
    with open_database(database_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if _executing_ids(connection):
            return None
        row = connection.execute("""
            SELECT id, work_type FROM (
                SELECT id, created_at, 'run' AS work_type FROM run WHERE execution_status = 'NOT_STARTED'
                UNION ALL
                SELECT id, created_at, 'recheck' AS work_type FROM check_report WHERE status = 'QUEUED'
            )
            ORDER BY julianday(created_at), created_at, id LIMIT 1
        """).fetchone()
        if row is None:
            return None
        if row["work_type"] == "run":
            return _claim_run(connection, row["id"]), None
        attempt = claim_recheck(connection, row["id"])
        return get_run(connection, attempt.run_id), attempt


def _finish_interrupted_step(
    connection: sqlite3.Connection, run: Run, step: RunStep, error: ErrorDetail,
) -> ErrorDetail:
    log_path = step.log_path or (step.request.log_path if step.request is not None else None)
    if log_path is not None:
        try:
            if not resolve_step_path(Path(run.workdir), log_path, field="log_path").is_file():
                log_path = None
        except (OSError, ValueError):
            log_path = None
    error = error.model_copy(update={"step_key": step.step_key, "log_path": log_path})
    now = datetime.now(UTC)
    started = step.started_at or now
    finished = max(started, now)
    result = StepResult(
        run_id=run.id, step_key=step.step_key, status=ExecutionStatus.FAILED,
        exit_code=None, outputs={}, log_path=log_path, warnings=[], error=error,
        started_at=started, finished_at=finished, environment={},
    )
    save_run_step(connection, step.model_copy(update={
        "status": ExecutionStatus.FAILED, "result": result, "log_path": log_path,
        "started_at": started, "finished_at": finished,
    }))
    return error


def _mark_interrupted(connection: sqlite3.Connection, run: Run, message: str) -> None:
    if run.execution_status is ExecutionStatus.RUNNING:
        execution, code, phase = ExecutionStatus.FAILED, "RUN_INTERRUPTED", "execute"
    elif run.execution_status is ExecutionStatus.SUCCEEDED and run.check_status is CheckStatus.CHECKING:
        execution, code, phase = ExecutionStatus.SUCCEEDED, "CHECK_INTERRUPTED", "check"
    else:
        return
    error = ErrorDetail(
        code=code, phase=phase, step_key=run.current_step or "run", field="run",
        message=message, suggestion="Review this run and its logs before resuming work.", log_path=None,
    )
    if execution is ExecutionStatus.FAILED:
        registered = connection.execute("SELECT 1 FROM artifact WHERE run_id = ? LIMIT 1", (run.id,)).fetchone()
        if registered is None:
            try:
                manifest = resolve_step_path(Path(run.workdir), ARTIFACT_MANIFEST_PATH, field="artifacts.manifest")
                manifest.unlink(missing_ok=True)
            except (OSError, ValueError) as cleanup:
                error = error.model_copy(update={"observed": {"manifest_cleanup_error": str(cleanup)}})
        for step in list_run_steps(connection, run.id):
            if step.status is ExecutionStatus.RUNNING:
                step_error = _finish_interrupted_step(connection, run, step, error)
                if step.step_key == run.current_step:
                    error = step_error
    else:
        attempt = get_active_check_attempt(connection, run.id)
        if attempt is not None:
            if run.current_report_id is None:
                try:
                    mirror = resolve_step_path(Path(run.workdir), CHECK_REPORT_PATH, field="checks.report")
                    mirror.unlink(missing_ok=True)
                except (OSError, ValueError) as cleanup:
                    error = error.model_copy(update={"message": f"{message}. Could not remove the unpublished report: {cleanup}"})
            publish_unfinished_attempt(connection, attempt, error)
            return
    update_run(connection, run.model_copy(update={
        "execution_status": execution, "check_status": CheckStatus.NOT_CHECKED, "error": error,
    }))


def _claim_work(database_path: str | Path, *, include_rechecks: bool) -> tuple[Run, CheckAttempt | None] | None:
    if include_rechecks:
        return _claim_next_work(database_path)
    run = claim_next_run(database_path)
    return None if run is None else (run, None)


def _dispatch_work(
    claimed: Run, attempt: CheckAttempt | None, execute: Callable[[Run], None],
    execute_recheck: Callable[[Run, CheckAttempt], None] | None,
) -> str:
    try:
        if attempt is not None and execute_recheck is not None:
            execute_recheck(claimed, attempt)
        else:
            execute(claimed)
    except Exception as error:
        return f"The worker was interrupted: {error}"
    return "The worker returned before finishing the active run"


def _settle_work(database_path: str | Path, run_id: str, message: str) -> None:
    with open_database(database_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        run = get_run(connection, run_id)
        if run is not None:
            _mark_interrupted(connection, run, message)


def run_once(
    database_path: str | Path, execute: Callable[[Run], None], *,
    execute_recheck: Callable[[Run, CheckAttempt], None] | None = None,
) -> str | None:
    """Claim a run, dispatch it outside the transaction, and retain failures."""
    job = _claim_work(database_path, include_rechecks=execute_recheck is not None)
    if job is None:
        return None
    claimed, attempt = job
    message = _dispatch_work(claimed, attempt, execute, execute_recheck)
    _settle_work(database_path, claimed.id, message)
    return claimed.id


def run_loop(
    database_path: str | Path, execute: Callable[[Run], None], stop: Event, *, poll_seconds: float = 0.25,
    execute_recheck: Callable[[Run, CheckAttempt], None] | None = None,
) -> None:
    """Dispatch available work until the owning service requests shutdown."""
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive and finite")
    pending: tuple[str, str] | None = None
    contended = False
    while pending is not None or not stop.is_set():
        try:
            if pending is not None:
                _settle_work(database_path, *pending)
                pending = None
                contended = False
                continue
            job = _claim_work(database_path, include_rechecks=execute_recheck is not None)
        except sqlite3.OperationalError as error:
            primary_code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if primary_code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            if not contended:
                phase = "settle" if pending is not None else "claim"
                _LOGGER.warning("Scheduler could not %s work because SQLite is busy; waiting to retry", phase)
                contended = True
            if stop.wait(poll_seconds):
                # Startup recovery can settle any active row once its workers have stopped.
                return
            continue
        contended = False
        if job is None:
            stop.wait(poll_seconds)
        else:
            claimed, attempt = job
            pending = (claimed.id, _dispatch_work(claimed, attempt, execute, execute_recheck))


def recover_interrupted_runs(
    database_path: str | Path, *, processes_stopped: Callable[[Run], bool],
) -> tuple[str, ...]:
    """Recover startup records after the service verifies its old processes have stopped."""
    with open_database(database_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        ids = _executing_ids(connection)
        for run_id in ids:
            run = get_run(connection, run_id)
            if processes_stopped(run) is not True:
                raise RuntimeError(f"Cannot recover run {run_id!r} until its managed processes have stopped")
            _mark_interrupted(connection, run, "The previous service stopped before this run completed")
    return ids
