"""Create a fresh run using an exact confirmed version of a previous plan."""

from pathlib import Path

from geopilot.persistence.run_repository import get_run
from geopilot.persistence.schema import open_database
from geopilot.runs.service import create_run


def retry_run(
    database_path: str | Path, previous_run_id: str, *, workspace_root: str | Path,
    plan_version: int | None = None,
) -> str:
    with open_database(database_path, read_only=True) as connection:
        previous = get_run(connection, previous_run_id)
    if previous is None:
        raise LookupError(f"Run {previous_run_id!r} does not exist.")
    return create_run(
        database_path, previous.plan_id, previous.plan_version if plan_version is None else plan_version,
        workspace_root=workspace_root,
    )
