"""HTTP entry point for the local GeoPilot workbench."""

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Thread
from time import monotonic_ns
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Response
from pydantic import ValidationError

from geopilot.api.artifact_routes import artifact_routes
from geopilot.api.catalog_routes import catalog_routes
from geopilot.api.check_routes import check_routes
from geopilot.api.model_routes import model_routes
from geopilot.api.plan_routes import plan_routes
from geopilot.api.replanning_routes import replanning_routes
from geopilot.api.run_step_routes import run_step_routes
from geopilot.api.script_routes import script_routes
from geopilot.api.task_routes import task_routes
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.execution.worker_lease import exclusive_local_lock, managed_workers_stopped
from geopilot.llm.base import ModelProvider, validate_model_timeout
from geopilot.methods.loader import MethodCatalog
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.runs.models import Run
from geopilot.runs.query_service import RunStatus, get_run_status
from geopilot.scheduler.cancellation import request_cancellation
from geopilot.scheduler.loop import recover_interrupted_runs, run_loop
from geopilot.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from geopilot.checks.recheck_service import CheckAttempt


_LOGGER = logging.getLogger(__name__)


def create_app(
    database_path: str | Path, *, workspace_root: str | Path | None = None,
    catalog: MethodCatalog | None = None, registry: ToolRegistry | None = None,
    registered_rules: Iterable[str] = RUN_CHECKS,
    execute_run: Callable[[Run], None] | None = None,
    execute_recheck: "Callable[[Run, CheckAttempt], None] | None" = None,
    rule_versions: Mapping[str, int] | None = None,
    processes_stopped: Callable[[Run], bool] | None = None,
    model_provider: ModelProvider | None = None,
    model_timeout: float = 30.0,
    sensor_registry: SensorConfigRegistry | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if execute_run is None:
            yield
            return
        database = Path(database_path).resolve()
        with exclusive_local_lock(database.with_name(f"{database.name}.scheduler.lock")):
            verify = processes_stopped or (lambda run: managed_workers_stopped(Path(run.workdir)))
            recover_interrupted_runs(database_path, processes_stopped=verify)
            stop = Event()
            thread = Thread(
                target=run_loop, args=(database_path, execute_run, stop),
                kwargs={"execute_recheck": execute_recheck}, name="geopilot-scheduler",
            )
            thread.start()
            try:
                yield
            finally:
                stop.set()
                thread.join()

    app = FastAPI(title="GeoPilot", lifespan=lifespan)
    root = Path(workspace_root) if workspace_root is not None else Path(database_path).absolute().parent / "workspace"
    rule_names = tuple(registered_rules)
    versions = {name: (rule_versions or {}).get(name, 1) for name in rule_names}
    app.include_router(plan_routes(
        database_path, root, catalog=catalog, registry=registry, registered_rules=rule_names,
        sensor_registry=sensor_registry,
    ))
    app.include_router(catalog_routes(database_path, catalog=catalog, registry=registry, registered_rules=rule_names))
    app.include_router(task_routes(database_path, catalog=catalog, registry=registry, sensor_registry=sensor_registry))
    app.include_router(run_step_routes(database_path))
    app.include_router(replanning_routes(
        database_path, catalog=catalog, registry=registry, registered_rules=rule_names, sensor_registry=sensor_registry,
    ))
    app.include_router(artifact_routes(database_path))
    app.include_router(check_routes(database_path, versions))
    app.include_router(script_routes(root))
    app.include_router(model_routes(
        database_path, catalog=catalog, registry=registry, registered_rules=rule_names,
        provider=model_provider, timeout=validate_model_timeout(model_timeout), sensor_registry=sensor_registry,
    ))

    @app.get("/api/runs/{run_id}")
    def read_run(run_id: str) -> RunStatus:
        started_ns = monotonic_ns()
        try:
            return get_run_status(database_path, run_id)
        except LookupError as error:
            raise HTTPException(
                status_code=404, detail={"code": "RUN_NOT_FOUND", "message": str(error)},
            ) from error
        except (sqlite3.Error, ValidationError) as error:
            event = {
                "event": "run_status_read_failed",
                "error_kind": "sqlite" if isinstance(error, sqlite3.Error) else "state_validation",
                "elapsed_ms": round((monotonic_ns() - started_ns) / 1_000_000, 3),
            }
            extended_code = getattr(error, "sqlite_errorcode", None)
            if isinstance(extended_code, int):
                event["sqlite_primary_code"] = extended_code & 0xFF
                event["sqlite_extended_code"] = extended_code
            _LOGGER.warning("%s", json.dumps(event))
            raise HTTPException(
                status_code=500, detail={"code": "STORAGE_FAILED", "message": "Could not read the run record."},
            ) from error

    @app.post("/api/runs/{run_id}/cancel", responses={202: {"model": RunStatus}})
    def cancel_run(run_id: str, response: Response) -> RunStatus:
        try:
            run, accepted = request_cancellation(database_path, run_id)
        except LookupError as error:
            raise HTTPException(
                status_code=404, detail={"code": "RUN_NOT_FOUND", "message": str(error)},
            ) from error
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(
                status_code=500, detail={"code": "STORAGE_FAILED", "message": "Could not cancel the run."},
            ) from error
        response.status_code = 202 if accepted else 200
        return RunStatus.from_run(run)

    return app
