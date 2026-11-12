"""Prepare and publish a run for the local scheduler."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

from geopilot.application.run_inputs import stage_run_inputs
from geopilot.persistence.run_repository import insert_run
from geopilot.persistence.schema import open_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.plans.compiler import PlanCompilationError
from geopilot.plans.models import PlanStatus
from geopilot.plans.service import load_plan
from geopilot.runs.models import Run


def _active_run_id(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("""
        SELECT id FROM run
        WHERE execution_status IN ('NOT_STARTED', 'RUNNING')
           OR (execution_status = 'SUCCEEDED' AND check_status = 'CHECKING')
        ORDER BY created_at, id LIMIT 1
    """).fetchone()
    return row["id"] if row is not None else None


def create_run(
    database_path: str | Path, plan_id: str, version: int, *, workspace_root: str | Path,
) -> str:
    """Return an existing active run or publish a newly prepared, queued run."""
    plan = load_plan(database_path, plan_id, version)
    if plan.status is not PlanStatus.CONFIRMED:
        raise PlanCompilationError("PLAN_NOT_CONFIRMED", "status", "Confirm this version before running")
    if not plan.has_checks:
        raise PlanCompilationError("MISSING_CHECK_RULES", "check_rules", "The confirmed plan requires check rules")

    with open_database(database_path) as connection:
        active = _active_run_id(connection)
    if active is not None:
        return active

    run_id = uuid4().hex
    workspace = create_run_workspace(workspace_root, plan.task_id, run_id)
    try:
        asset_files = stage_run_inputs(database_path, plan.inputs, workspace.path)
        snapshot = {
            "run_id": run_id,
            "plan": plan.model_dump(mode="json"),
            "asset_files": {role: reference.model_dump(mode="json") for role, reference in asset_files.items()},
        }
        with workspace.request_path.open("x", encoding="utf-8") as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")

        # Keep the write lock out of file copying, then arbitrate concurrent submissions.
        with open_database(database_path) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            active = _active_run_id(connection)
            if active is None:
                insert_run(connection, Run(
                    id=run_id, plan_id=plan.id, plan_version=plan.version,
                    workdir=str(workspace.path), created_at=datetime.now(UTC),
                ))
    except BaseException as error:
        try:
            rmtree(workspace.path)
        except OSError as cleanup:
            error.add_note(f"Could not remove unpublished run directory {workspace.path}: {cleanup}")
        raise

    if active is not None:
        rmtree(workspace.path)
        return active
    return run_id
