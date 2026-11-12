"""Execute confirmed steps in order and persist their actual handoffs."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path

from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import ExecutionStatus, StepFileRef, StepResult
from geopilot.execution.runner import run_step
from geopilot.execution.step_support import AdapterError, PreparedStep, resolve_step_path
from geopilot.methods.models import StepOutputBinding
from geopilot.persistence.run_repository import get_run, list_run_steps, save_run_step, update_run
from geopilot.persistence.schema import open_database
from geopilot.persistence.workspace import RunWorkspace
from geopilot.plans.compiler import PlanCompilationError, compile_step_request
from geopilot.plans.models import AnalysisPlan, PlanStatus
from geopilot.runs.models import RunStep
from geopilot.scheduler.cancellation import observe_cancellation
from geopilot.scheduler.output_resolver import resolve_named_output
from geopilot.tools.registry import ToolRegistry


def execute_steps(
    database_path: str | Path,
    workspace: RunWorkspace,
    plan: AnalysisPlan,
    asset_files: Mapping[str, StepFileRef],
    *,
    registry: ToolRegistry,
    qgis_bindings: Mapping | None = None,
    python_implementations: Mapping[str, str | Path] | None = None,
    qgis_process: str | PathLike[str] | None = None,
    timeout: float = 60,
    input_preflight: Callable[[PreparedStep], None] | None = None,
) -> dict[str, StepResult]:
    """Persist each confirmed step and stop the chain on the first non-success."""
    workdir = workspace.path
    run_id = workdir.name
    if plan.status is not PlanStatus.CONFIRMED:
        raise PlanCompilationError("PLAN_NOT_CONFIRMED", "status", "Confirm this version before running")
    records = tuple(
        RunStep(run_id=run_id, step_key=step.key, position=index)
        for index, step in enumerate(plan.steps)
    )
    with open_database(database_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        run = get_run(connection, run_id)
        if run is None or run.execution_status is not ExecutionStatus.RUNNING:
            raise RuntimeError(f"Run {run_id!r} has not been claimed")
        if (run.plan_id, run.plan_version, Path(run.workdir)) != (plan.id, plan.version, workdir):
            raise ValueError("Claimed run does not match the confirmed plan and workspace")
        if list_run_steps(connection, run_id):
            raise RuntimeError(f"Run {run_id!r} already has step records")
        for record in records:
            save_run_step(connection, record)

    results = {}
    for record, step in zip(records, plan.steps, strict=True):
        started_at = datetime.now(UTC)
        request = None
        try:
            request = compile_step_request(
                plan, record.step_key, run_id=run_id, asset_files=asset_files, prior_results=results,
            )
            def cancel_requested() -> bool:
                return observe_cancellation(database_path, run_id, workdir, request.cancel_path)

            if cancel_requested():
                break
            inputs = request.inputs.copy()
            for port, binding in step.inputs.items():
                if isinstance(binding, StepOutputBinding):
                    output = results[binding.step].outputs[binding.output]
                    inputs[port] = resolve_named_output(workdir, output, field=f"inputs.{port}")
            request = request.model_copy(update={"inputs": inputs})
            with open_database(database_path) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                run = get_run(connection, run_id)
                if run.cancel_requested:
                    break
                workspace.create_step(record.step_key)
                for role, target in request.output_targets.items():
                    path = resolve_step_path(workdir, target.path, field=f"output_targets.{role}.path")
                    path.parent.mkdir(parents=True, exist_ok=True)
                update_run(connection, run.model_copy(update={"current_step": record.step_key}))
                save_run_step(connection, record.model_copy(update={
                    "status": ExecutionStatus.RUNNING, "request": request, "started_at": started_at,
                }))
            result = run_step(
                request, registry=registry, workdir=workdir, qgis_bindings=qgis_bindings,
                python_implementations=python_implementations, qgis_process=qgis_process,
                timeout=timeout, input_preflight=input_preflight, cancel_requested=cancel_requested,
            )
        except (PlanCompilationError, AdapterError, OSError) as error:
            failure = ErrorDetail(
                code=getattr(error, "code", "STORAGE_FAILED"),
                phase=getattr(error, "phase", "execute" if isinstance(error, OSError) else "preflight"),
                step_key=record.step_key, field=getattr(error, "field", "workdir_ref"),
                message=str(error), suggestion="Review the confirmed request and run files.", log_path=None,
            )
            result = StepResult(
                run_id=run_id, step_key=record.step_key, status=ExecutionStatus.FAILED, exit_code=None,
                outputs={}, log_path=None, warnings=[], error=failure, started_at=started_at,
                finished_at=datetime.now(UTC), environment={},
            )
        with open_database(database_path) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            run = get_run(connection, run_id)
            if run.current_step != record.step_key:
                update_run(connection, run.model_copy(update={"current_step": record.step_key}))
            save_run_step(connection, record.model_copy(update={
                "status": result.status, "request": request, "result": result, "log_path": result.log_path,
                "started_at": result.started_at, "finished_at": result.finished_at,
            }))
        results[record.step_key] = result
        if result.status is not ExecutionStatus.SUCCEEDED:
            break
    return results
