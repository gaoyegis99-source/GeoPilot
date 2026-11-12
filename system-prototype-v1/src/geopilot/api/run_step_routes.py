"""Expose persisted run origin, step status and local process logs."""

import sqlite3
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import ExecutionStatus
from geopilot.execution.step_support import AdapterError, resolve_step_path
from geopilot.persistence.run_repository import get_run, get_run_step, list_run_steps
from geopilot.persistence.schema import open_database
from geopilot.runs.models import RunStep


class StepStatus(BaseModel):
    step_key: str
    position: int
    status: ExecutionStatus
    log_available: bool
    started_at: datetime | None
    finished_at: datetime | None
    error: ErrorDetail | None


class RunSteps(BaseModel):
    run_id: str
    steps: list[StepStatus]


class RunOrigin(BaseModel):
    run_id: str
    task_id: str
    plan_id: str
    plan_version: Annotated[int, Field(strict=True, gt=0)]


def _log_file(workdir: Path, step: RunStep) -> Path | None:
    reference = step.log_path or (step.request.log_path if step.request is not None else None)
    if reference is None:
        return None
    try:
        path = resolve_step_path(workdir, reference, field="log_path")
        return path if path.is_file() else None
    except (OSError, AdapterError):
        return None


def run_step_routes(database_path: str | Path) -> APIRouter:
    router = APIRouter(prefix="/api/runs")

    @router.get("/{run_id}/origin")
    def read_origin(run_id: str) -> RunOrigin:
        try:
            with open_database(database_path, read_only=True) as connection:
                row = connection.execute(
                    """
                    SELECT r.id AS run_id, p.task_id, r.plan_id, r.plan_version
                    FROM run AS r
                    LEFT JOIN analysis_plan AS p ON p.id = r.plan_id AND p.version = r.plan_version
                    WHERE r.id = ?
                    """,
                    (run_id,),
                ).fetchone()
            if row is not None:
                return RunOrigin.model_validate(dict(row))
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(status_code=500, detail={
                "code": "STORAGE_FAILED", "message": "Could not read the run origin.",
            }) from error
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": "The run does not exist."})

    @router.get("/{run_id}/steps")
    def read_steps(run_id: str) -> RunSteps:
        try:
            with open_database(database_path, read_only=True) as connection, connection:
                connection.execute("BEGIN")
                run = get_run(connection, run_id)
                if run is None:
                    raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": "The run does not exist."})
                steps = list_run_steps(connection, run_id)
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(status_code=500, detail={
                "code": "STORAGE_FAILED", "message": "Could not read the run steps.",
            }) from error
        return RunSteps(run_id=run_id, steps=[
            StepStatus(
                step_key=step.step_key, position=step.position, status=step.status,
                log_available=_log_file(Path(run.workdir), step) is not None,
                started_at=step.started_at, finished_at=step.finished_at,
                error=step.result.error if step.result is not None else None,
            ) for step in steps
        ])

    @router.get("/{run_id}/steps/{step_key}/log", response_class=PlainTextResponse)
    def read_log(run_id: str, step_key: str, tail: Annotated[int | None, Query(ge=1, le=1000)] = None) -> Response:
        try:
            with open_database(database_path, read_only=True) as connection, connection:
                connection.execute("BEGIN")
                run = get_run(connection, run_id)
                step = get_run_step(connection, run_id, step_key)
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(status_code=500, detail={
                "code": "STORAGE_FAILED", "message": "Could not locate the step log.",
            }) from error
        if run is None or step is None or (path := _log_file(Path(run.workdir), step)) is None:
            raise HTTPException(status_code=404, detail={"code": "LOG_NOT_FOUND", "message": "The step log is unavailable."})
        if tail is None:
            return FileResponse(path, media_type="text/plain")
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                return PlainTextResponse("".join(deque(stream, maxlen=tail)))
        except OSError as error:
            raise HTTPException(status_code=422, detail={
                "code": "LOG_UNREADABLE", "message": "Could not open the step log.",
            }) from error

    return router
