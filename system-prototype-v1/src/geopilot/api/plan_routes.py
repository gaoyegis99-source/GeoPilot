"""Expose exact plan versions, confirmation and queued execution."""

import sqlite3
from collections.abc import Iterable
from functools import partial
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from geopilot.application.method_preconditions import METHOD_PRECONDITIONS
from geopilot.application.remote_sensing_plan import remote_sensing_inputs_are_consistent
from geopilot.data.assets import InputChangedError
from geopilot.data.errors import SourceReadError
from geopilot.methods.loader import MethodCatalog
from geopilot.plans.compiler import PlanCompilationError
from geopilot.plans.models import AnalysisPlan
from geopilot.plans.service import PlanConfirmationError, confirm_plan, load_plan
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.runs.service import create_run
from geopilot.tools.registry import ToolRegistry


class PlanVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_version: int = Field(strict=True, gt=0)


class RunAccepted(BaseModel):
    run_id: str


def _problem(status: int, code: str, message: str, **fields) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **fields})


def plan_routes(
    database_path: str | Path, workspace_root: Path, *,
    catalog: MethodCatalog | None, registry: ToolRegistry | None, registered_rules: Iterable[str],
    sensor_registry: SensorConfigRegistry | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/plans")
    rule_names = tuple(registered_rules)
    preconditions = {
        **METHOD_PRECONDITIONS,
        "remote_sensing_inputs_are_consistent": partial(
            remote_sensing_inputs_are_consistent, sensor_registry=sensor_registry,
        ),
    }

    @router.get("/{plan_id}")
    def read_plan(plan_id: str, version: Annotated[int, Query(gt=0)]) -> AnalysisPlan:
        try:
            return load_plan(database_path, plan_id, version)
        except LookupError as error:
            raise _problem(404, "PLAN_NOT_FOUND", str(error)) from error
        except (sqlite3.Error, ValidationError) as error:
            raise _problem(500, "STORAGE_FAILED", "Could not read the plan.") from error

    @router.post("/{plan_id}/confirm")
    def confirm(plan_id: str, request: PlanVersionRequest) -> AnalysisPlan:
        if catalog is None or registry is None:
            raise _problem(503, "METHOD_CATALOG_UNAVAILABLE", "Configure the local method and tool catalogues.")
        try:
            return confirm_plan(
                database_path, plan_id, request.plan_version, catalog=catalog, registry=registry,
                registered_rules=rule_names, precondition_checks=preconditions,
            )
        except LookupError as error:
            raise _problem(404, "PLAN_NOT_FOUND", str(error)) from error
        except PlanConfirmationError as error:
            status = 409 if any(issue.code == "PLAN_CHANGED" for issue in error.issues) else 422
            raise _problem(
                status, "PLAN_CONFIRMATION_FAILED", str(error),
                issues=[issue.model_dump(mode="json") for issue in error.issues],
            ) from error
        except (sqlite3.Error, ValidationError) as error:
            raise _problem(500, "STORAGE_FAILED", "Could not confirm the plan.") from error

    @router.post("/{plan_id}/runs", status_code=202)
    def queue_run(plan_id: str, request: PlanVersionRequest) -> RunAccepted:
        try:
            run_id = create_run(database_path, plan_id, request.plan_version, workspace_root=workspace_root)
        except LookupError as error:
            raise _problem(404, "PLAN_NOT_FOUND", str(error)) from error
        except (PlanCompilationError, InputChangedError, SourceReadError) as error:
            status = 409 if error.code in {"PLAN_NOT_CONFIRMED", "INPUT_CHANGED"} else 422
            raise _problem(status, error.code, str(error), field=error.field) from error
        except (OSError, sqlite3.Error, ValidationError) as error:
            raise _problem(500, "STORAGE_FAILED", "Could not create the local run.") from error
        return RunAccepted(run_id=run_id)

    return router
