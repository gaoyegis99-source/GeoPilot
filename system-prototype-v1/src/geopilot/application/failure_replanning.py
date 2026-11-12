"""Prepare and apply a local draft proposal tied to a specific failed run."""

import json
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel

from geopilot.application.draft_options import resolve_draft_options
from geopilot.application.task_context import build_task_context
from geopilot.domain.step_io import ExecutionStatus
from geopilot.llm.alternative_plan import PlanComparison, compare_plan_alternatives, preview_plan_revision
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.validator import validate_method
from geopilot.persistence.plan_repository import plan_from_row, row_for_plan
from geopilot.persistence.run_repository import get_run
from geopilot.persistence.schema import open_database
from geopilot.plans.models import AnalysisPlan, PlanStatus
from geopilot.plans.versioning import revise_plan
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.runs.models import CheckStatus, Run
from geopilot.tools.registry import ToolRegistry


class ReplanningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FailureProposal(PlanComparison):
    proposal_id: str


class FailureReplanning(BaseModel):
    run_id: str
    execution_status: ExecutionStatus
    check_status: CheckStatus
    baseline: PlanComparison
    alternatives: tuple[FailureProposal, ...]


def _failed_origin(database: str | Path, run_id: str) -> tuple[Run, AnalysisPlan]:
    with open_database(database, read_only=True) as connection, connection:
        connection.execute("BEGIN")
        run = get_run(connection, run_id)
        if run is None:
            raise LookupError(f"Run {run_id!r} does not exist.")
        if not run.can_replan:
            raise ReplanningError("RUN_NOT_FAILED", "The selected run has no current execution or check failure.")
        row = row_for_plan(connection, run.plan_id, run.plan_version)
        if row is None:
            raise ValueError("The failed run has no exact plan snapshot")
        plan = plan_from_row(row)
    if plan.status is not PlanStatus.CONFIRMED:
        raise ValueError("A failed run requires a confirmed source plan")
    return run, plan


def _proposal(run: Run, comparison: PlanComparison) -> FailureProposal:
    payload = comparison.model_dump(mode="json")
    state = run.model_dump(mode="json", include={"id", "execution_status", "check_status", "current_report_id"})
    canonical = json.dumps([state, payload], sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return FailureProposal(proposal_id=sha256(canonical.encode("utf-8")).hexdigest(), **payload)


def _report(
    database: str | Path, run: Run, plan: AnalysisPlan, *, catalog: MethodCatalog, registry: ToolRegistry,
    registered_rules: Iterable[str], sensor_registry: SensorConfigRegistry | None,
) -> FailureReplanning:
    rules = tuple(registered_rules)
    comparison = compare_plan_alternatives(
        database, plan, catalog=catalog, registry=registry, registered_rules=rules, sensor_registry=sensor_registry,
    )
    alternatives = list(comparison.alternatives)
    try:
        method = catalog.get_enabled(plan.method.id, plan.method.version)
    except KeyError:
        method = None
    if method is not None and not validate_method(
        method, registry=registry, registered_rules=rules, options=plan.critical_options,
    ):
        refreshed = preview_plan_revision(
            database, plan, method, selected=plan.inputs, options=plan.critical_options,
            criteria=comparison.baseline.criteria, registry=registry, sensor_registry=sensor_registry,
        )
        if any(issue.field.startswith(("inputs.", "critical_options.")) for issue in refreshed.plan.issues):
            alternatives.insert(0, refreshed)
    return FailureReplanning(
        run_id=run.id, execution_status=run.execution_status, check_status=run.check_status,
        baseline=comparison.baseline, alternatives=tuple(_proposal(run, item) for item in alternatives),
    )


def propose_failure_revision(
    database: str | Path, run_id: str, *, catalog: MethodCatalog, registry: ToolRegistry,
    registered_rules: Iterable[str], sensor_registry: SensorConfigRegistry | None = None,
) -> FailureReplanning:
    run, plan = _failed_origin(database, run_id)
    return _report(
        database, run, plan, catalog=catalog, registry=registry,
        registered_rules=registered_rules, sensor_registry=sensor_registry,
    )


def apply_failure_revision(
    database: str | Path, run_id: str, proposal_id: str, *, catalog: MethodCatalog, registry: ToolRegistry,
    registered_rules: Iterable[str], sensor_registry: SensorConfigRegistry | None = None,
) -> AnalysisPlan:
    run, plan = _failed_origin(database, run_id)
    report = _report(
        database, run, plan, catalog=catalog, registry=registry,
        registered_rules=registered_rules, sensor_registry=sensor_registry,
    )
    chosen = next((item for item in report.alternatives if item.proposal_id == proposal_id), None)
    if chosen is None:
        raise ReplanningError("REPLAN_PROPOSAL_CHANGED", "The proposal changed. Compare the available revisions again.")
    candidate = chosen.plan
    method = catalog.get_enabled(candidate.method.id, candidate.method.version)
    context = build_task_context(
        database, task_id=plan.task_id, goal=plan.goal, method=method, selected=candidate.inputs,
        criteria=chosen.criteria, registry=registry, options=candidate.critical_options,
    )
    context, options = resolve_draft_options(database, context, candidate.critical_options, sensor_registry=sensor_registry)
    return revise_plan(
        database, plan.id, plan.version, context=context, method=method,
        options=options, registry=registry, failed_run=run,
    )
