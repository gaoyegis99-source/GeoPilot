"""Read the committed public run status from local metadata."""

from pathlib import Path

from pydantic import BaseModel

from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import ExecutionStatus
from geopilot.persistence.run_repository import get_run
from geopilot.persistence.schema import open_database
from geopilot.runs.models import CheckStatus, Run


class RunStatus(BaseModel):
    run_id: str
    execution_status: ExecutionStatus
    check_status: CheckStatus
    current_step: str | None
    cancel_requested: bool
    error: ErrorDetail | None
    current_report_id: str | None

    @classmethod
    def from_run(cls, run: Run) -> "RunStatus":
        return cls(
            run_id=run.id, execution_status=run.execution_status, check_status=run.check_status,
            current_step=run.current_step, cancel_requested=run.cancel_requested, error=run.error,
            current_report_id=run.current_report_id,
        )


def get_run_status(database_path: str | Path, run_id: str) -> RunStatus:
    with open_database(database_path, read_only=True) as connection:
        run = get_run(connection, run_id)
    if run is None:
        raise LookupError(f"Run {run_id!r} does not exist.")
    return RunStatus.from_run(run)
