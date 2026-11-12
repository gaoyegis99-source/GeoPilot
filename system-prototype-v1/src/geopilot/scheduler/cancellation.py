"""Record run cancellation and relay committed intent to local workers."""

from pathlib import Path

from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import ExecutionStatus
from geopilot.execution.step_support import resolve_step_path
from geopilot.persistence.run_repository import get_run, update_run
from geopilot.persistence.schema import open_database
from geopilot.runs.models import CheckStatus, Run


def cancellation_error(step_key: str | None) -> ErrorDetail:
    return ErrorDetail(
        code="RUN_CANCELLED", phase="execute", step_key=step_key or "run", field="run",
        message="The run was cancelled by request.", suggestion="Start a new run to repeat the analysis.",
        log_path=None,
    )


def request_cancellation(database_path: str | Path, run_id: str) -> tuple[Run, bool]:
    """Persist cancellation and report whether the request was accepted."""
    with open_database(database_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        run = get_run(connection, run_id)
        if run is None:
            raise LookupError(f"Run {run_id!r} does not exist.")
        if run.execution_status is ExecutionStatus.NOT_STARTED:
            run = update_run(connection, run.model_copy(update={
                "execution_status": ExecutionStatus.CANCELLED, "cancel_requested": True,
                "error": cancellation_error(None),
            }))
        elif run.execution_status is ExecutionStatus.RUNNING or run.check_status is CheckStatus.CHECKING:
            if not run.cancel_requested:
                run = update_run(connection, run.model_copy(update={"cancel_requested": True}))
        else:
            return run, False
        return run, True


def observe_cancellation(database_path: str | Path, run_id: str, workdir: Path, cancel_ref: str) -> bool:
    """Mirror committed cancellation to the cooperative marker used by scripts."""
    with open_database(database_path, read_only=True) as connection:
        run = get_run(connection, run_id)
    if run is None or not run.cancel_requested:
        return False
    marker = resolve_step_path(workdir, cancel_ref, field="cancel_path")
    try:
        with marker.open("x", encoding="utf-8"):
            pass
    except FileExistsError:
        pass
    return True
