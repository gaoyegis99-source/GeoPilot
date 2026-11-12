"""Compare and select a draft revision for an exact failed local run."""

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from geopilot.application.failure_replanning import (
    FailureReplanning, ReplanningError, apply_failure_revision, propose_failure_revision,
)
from geopilot.methods.loader import MethodCatalog
from geopilot.plans.models import AnalysisPlan
from geopilot.plans.versioning import FailedRunConflictError, PlanVersionConflictError
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.tools.registry import ToolRegistry


class ProposalSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")


@contextmanager
def _replanning_errors() -> Iterator[None]:
    try:
        yield
    except KeyError as error:
        raise HTTPException(status_code=404, detail={
            "code": "METHOD_NOT_FOUND", "message": "The exact method version is unavailable.",
        }) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": str(error)}) from error
    except PlanVersionConflictError as error:
        raise HTTPException(status_code=409, detail={
            "code": error.code, "message": str(error), "field": error.field, "latest_version": error.latest_version,
        }) from error
    except FailedRunConflictError as error:
        raise HTTPException(status_code=409, detail={"code": "FAILED_RUN_CHANGED", "message": str(error)}) from error
    except ReplanningError as error:
        raise HTTPException(status_code=409, detail={"code": error.code, "message": str(error)}) from error
    except (sqlite3.Error, ValueError) as error:
        raise HTTPException(status_code=500, detail={
            "code": "STORAGE_FAILED", "message": "Could not prepare a revision of the failed run.",
        }) from error


def replanning_routes(
    database: str | Path, *, catalog: MethodCatalog | None, registry: ToolRegistry | None,
    registered_rules: Iterable[str], sensor_registry: SensorConfigRegistry | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/runs")
    rules = tuple(registered_rules)

    def require_catalog() -> tuple[MethodCatalog, ToolRegistry]:
        if catalog is None or registry is None:
            raise HTTPException(status_code=503, detail={
                "code": "METHOD_CATALOG_UNAVAILABLE", "message": "Configure the local method and tool catalogues.",
            })
        return catalog, registry

    @router.get("/{run_id}/replanning")
    def proposals(run_id: str) -> FailureReplanning:
        methods, tools = require_catalog()
        with _replanning_errors():
            return propose_failure_revision(
                database, run_id, catalog=methods, registry=tools, registered_rules=rules, sensor_registry=sensor_registry,
            )

    @router.post("/{run_id}/replanning")
    def select(run_id: str, request: ProposalSelection) -> AnalysisPlan:
        methods, tools = require_catalog()
        with _replanning_errors():
            return apply_failure_revision(
                database, run_id, request.proposal_id, catalog=methods, registry=tools,
                registered_rules=rules, sensor_registry=sensor_registry,
            )

    return router
