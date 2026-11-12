"""Prepare and restore task, context and versioned plan records."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Self
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from geopilot.application.draft_options import resolve_draft_options
from geopilot.application.task_context import ContextCriteria, TaskContext, build_task_context
from geopilot.domain.data_asset import DataAssetRef
from geopilot.domain.step_io import ExecutionStatus
from geopilot.domain.task import TaskGoal
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition, MethodRef
from geopilot.persistence.schema import open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.models import AnalysisPlan, PlanStatus
from geopilot.plans.service import save_draft
from geopilot.plans.versioning import PlanVersionConflictError, revise_plan
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.runs.models import CheckStatus
from geopilot.tools.registry import ToolRegistry


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: TaskGoal


class TaskCreated(BaseModel):
    task_id: str


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: MethodRef
    inputs: dict[str, DataAssetRef] = Field(default_factory=dict)
    goal: TaskGoal | None = None
    critical_options: dict[str, JsonValue] = Field(default_factory=dict)
    criteria: ContextCriteria | None = None


class ContextPreviewRequest(ContextRequest):
    goal: TaskGoal


class DraftRequest(ContextRequest):
    base_plan_id: str | None = None
    base_plan_version: Annotated[int, Field(strict=True, gt=0)] | None = None

    @model_validator(mode="after")
    def validate_baseline(self) -> Self:
        if (self.base_plan_id is None) != (self.base_plan_version is None):
            raise ValueError("A revision requires both the baseline plan ID and version.")
        if self.base_plan_id is not None and not self.base_plan_id.strip():
            raise ValueError("The baseline plan ID cannot be empty.")
        return self


class PlanLink(BaseModel):
    id: str
    version: int
    status: PlanStatus


class RunLink(BaseModel):
    run_id: str
    plan_id: str
    plan_version: int
    execution_status: ExecutionStatus
    check_status: CheckStatus


class TaskRecord(BaseModel):
    task_id: str
    goal: TaskGoal
    context: TaskContext | None
    plans: list[PlanLink]
    runs: list[RunLink]


def _task_row(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT goal_json, context_json FROM task WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": f"Task {task_id!r} does not exist."})
    return row


def _storage_error(message: str) -> HTTPException:
    return HTTPException(status_code=500, detail={"code": "STORAGE_FAILED", "message": message})


def task_routes(
    database_path: str | Path, *, catalog: MethodCatalog | None, registry: ToolRegistry | None,
    sensor_registry: SensorConfigRegistry | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/tasks")

    def prepare_context(task_id: str, request: ContextRequest, *, preview: bool = False) -> tuple[TaskContext, MethodDefinition]:
        if catalog is None or registry is None:
            raise HTTPException(status_code=503, detail={
                "code": "METHOD_CATALOG_UNAVAILABLE", "message": "The local method catalogue is unavailable.",
            })
        if preview:
            goal = request.goal
        else:
            with open_database(database_path, read_only=True) as connection:
                stored = _task_row(connection, task_id)
            goal = request.goal or TaskGoal.model_validate_json(stored["goal_json"])
        try:
            method = catalog.get_enabled(request.method.id, request.method.version)
        except KeyError as error:
            raise HTTPException(status_code=404, detail={
                "code": "METHOD_NOT_FOUND", "field": "method", "message": "The exact method version is unavailable for selection.",
            }) from error
        try:
            context = build_task_context(
                database_path, task_id=task_id, goal=goal, method=method, selected=request.inputs, registry=registry,
                criteria=(request.criteria or ContextCriteria()) if preview else request.criteria,
                options=request.critical_options, verify_selected=not preview,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail={
                "code": "INVALID_TASK_INPUTS", "field": "inputs", "message": str(error),
            }) from error
        return context, method

    @router.post("/context-preview")
    def preview_context(request: ContextPreviewRequest) -> TaskContext:
        try:
            context, _ = prepare_context("preview", request, preview=True)
        except (sqlite3.Error, ValidationError) as error:
            raise _storage_error("Could not preview the task context.") from error
        return context

    @router.post("", status_code=201)
    def create_task(request: TaskRequest) -> TaskCreated:
        task_id = uuid4().hex
        try:
            with open_database(database_path) as connection, connection:
                connection.execute(
                    "INSERT INTO task (id, goal_json, created_at) VALUES (?, ?, ?)",
                    (task_id, request.goal.model_dump_json(), datetime.now(UTC).isoformat()),
                )
        except sqlite3.Error as error:
            raise _storage_error("Could not save the task.") from error
        return TaskCreated(task_id=task_id)

    @router.get("/{task_id}")
    def read_task(task_id: str) -> TaskRecord:
        try:
            with open_database(database_path, read_only=True) as connection, connection:
                connection.execute("BEGIN")
                row = _task_row(connection, task_id)
                plans = connection.execute(
                    "SELECT id, version, status FROM analysis_plan WHERE task_id = ? ORDER BY created_at DESC, version DESC, id",
                    (task_id,),
                ).fetchall()
                runs = connection.execute(
                    """
                    SELECT r.id AS run_id, r.plan_id, r.plan_version, r.execution_status, r.check_status
                    FROM run AS r JOIN analysis_plan AS p ON p.id = r.plan_id AND p.version = r.plan_version
                    WHERE p.task_id = ? ORDER BY r.created_at DESC, r.id
                    """, (task_id,),
                ).fetchall()
            return TaskRecord(
                task_id=task_id, goal=TaskGoal.model_validate_json(row["goal_json"]),
                context=None if row["context_json"] is None else TaskContext.model_validate_json(row["context_json"]),
                plans=[PlanLink(**entry) for entry in plans], runs=[RunLink(**entry) for entry in runs],
            )
        except (sqlite3.Error, ValidationError) as error:
            raise _storage_error("Could not read the task.") from error

    @router.post("/{task_id}/context")
    def save_context(task_id: str, request: ContextRequest) -> TaskContext:
        try:
            context, _ = prepare_context(task_id, request)
            with open_database(database_path) as connection, connection:
                connection.execute(
                    "UPDATE task SET goal_json = ?, context_json = ? WHERE id = ?",
                    (context.goal.model_dump_json(), context.model_dump_json(), task_id),
                )
        except (sqlite3.Error, ValidationError) as error:
            raise _storage_error("Could not prepare the task context.") from error
        return context

    @router.post("/{task_id}/plans")
    def draft_plan(task_id: str, request: DraftRequest) -> AnalysisPlan:
        try:
            context, method = prepare_context(task_id, request)
            context, options = resolve_draft_options(
                database_path, context, request.critical_options, sensor_registry=sensor_registry,
            )
            if request.base_plan_id is not None:
                return revise_plan(
                    database_path, request.base_plan_id, request.base_plan_version, context=context,
                    method=method, options=options, registry=registry,
                )
            plan = build_draft(context, method, plan_id=uuid4().hex, options=options, registry=registry)
            return save_draft(database_path, plan, context=context)
        except (sqlite3.Error, ValidationError) as error:
            raise _storage_error("Could not save the plan draft.") from error
        except PlanVersionConflictError as error:
            raise HTTPException(status_code=409, detail={
                "code": error.code, "field": error.field, "message": str(error),
            }) from error
        except LookupError as error:
            raise HTTPException(status_code=404, detail={"code": "PLAN_NOT_FOUND", "message": str(error)}) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"code": "INVALID_PLAN_DRAFT", "message": str(error)}) from error

    return router
