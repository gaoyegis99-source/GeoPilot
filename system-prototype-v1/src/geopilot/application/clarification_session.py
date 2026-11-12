"""Read and answer questions about a saved draft through the plan version service."""

from pathlib import Path

from pydantic import JsonValue, ValidationError

from geopilot.application.draft_options import derived_method_options, resolve_draft_options
from geopilot.application.task_context import ContextCriteria, InputReadiness, TaskContext, build_task_context
from geopilot.domain.data_asset import DataAssetRef
from geopilot.domain.task import TaskGoal
from geopilot.llm.clarification import ClarificationReport, build_clarification_report
from geopilot.methods.loader import MethodCatalog
from geopilot.persistence.schema import open_database
from geopilot.plans.models import AnalysisPlan, PlanStatus
from geopilot.plans.service import load_plan
from geopilot.plans.versioning import PlanAnswerError, PlanDraftRequiredError, revise_plan
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.tools.registry import ToolRegistry


def _criteria(database_path: str | Path, plan: AnalysisPlan) -> ContextCriteria:
    with open_database(database_path, read_only=True) as connection:
        row = connection.execute("SELECT context_json FROM task WHERE id = ?", (plan.task_id,)).fetchone()
    if row is not None and row["context_json"] is not None:
        context = TaskContext.model_validate_json(row["context_json"])
        selected = {role: item.asset for role, item in context.roles.items() if item.asset is not None}
        if (context.task_id, context.method, context.goal, selected) == (plan.task_id, plan.method, plan.goal, plan.inputs):
            return context.criteria or ContextCriteria()
    return ContextCriteria()


def read_clarification(
    database_path: str | Path, plan_id: str, version: int, *, catalog: MethodCatalog, registry: ToolRegistry,
) -> ClarificationReport:
    plan = load_plan(database_path, plan_id, version)
    method = catalog.get(plan.method.id, plan.method.version)
    context = None
    input_fields = {f"inputs.{role}" for role in method.inputs}
    if any(issue.field in input_fields for issue in plan.issues):
        context = build_task_context(
            database_path, task_id=plan.task_id, goal=plan.goal, method=method, selected=plan.inputs,
            criteria=_criteria(database_path, plan), registry=registry, options=plan.critical_options,
        )
    return build_clarification_report(plan, method, context=context)


def answer_clarification(
    database_path: str | Path, plan_id: str, version: int, field: str, value: JsonValue, *,
    catalog: MethodCatalog, registry: ToolRegistry, sensor_registry: SensorConfigRegistry | None = None,
) -> AnalysisPlan:
    plan = load_plan(database_path, plan_id, version)
    if plan.status is not PlanStatus.DRAFT:
        raise PlanDraftRequiredError("Answer clarification on a draft plan version.")
    method = catalog.get_enabled(plan.method.id, plan.method.version)
    report = build_clarification_report(plan, method)
    goal, inputs, options = plan.goal, plan.inputs.copy(), plan.critical_options.copy()
    _, _, name = field.partition(".")
    item = next((item for item in report.items if item.field == field and item.kind in {"goal", "input", "option"}), None)
    if item is None:
        raise PlanAnswerError("Choose an unresolved goal, input or option in this draft.")

    try:
        if field == "goal.summary":
            goal = TaskGoal(summary=value.strip() if isinstance(value, str) else value)
            if goal.summary == plan.goal.summary.strip():
                raise PlanAnswerError("Describe the intended result with a revised goal.")
        elif item.kind == "input":
            inputs[name] = DataAssetRef.model_validate(value)
        elif item.kind == "option" and name not in derived_method_options(method.ref):
            options[name] = value
        else:
            raise PlanAnswerError("This field is resolved by plan validation.")
    except ValidationError as error:
        raise PlanAnswerError("Provide a nonempty goal or an exact asset ID and version.") from error

    context = build_task_context(
        database_path, task_id=plan.task_id, goal=goal, method=method, selected=inputs,
        criteria=_criteria(database_path, plan), registry=registry, options=options,
    )
    context, options = resolve_draft_options(database_path, context, options, sensor_registry=sensor_registry)
    if item.kind == "input" and context.roles[name].status is InputReadiness.BLOCKED:
        raise PlanAnswerError(context.roles[name].message or "This asset version cannot fill the requested role.")
    return revise_plan(
        database_path, plan_id, version, context=context, method=method,
        options=options, registry=registry, answer_field=field,
    )
