"""Compare exact registered GIS methods and tool variants without persisting a plan."""

from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, JsonValue

from geopilot.application.draft_options import derived_method_options, resolve_draft_options
from geopilot.application.task_context import ContextCriteria, build_task_context, current_task_criteria
from geopilot.domain.tool_contract import MeasurementPolicy, ParameterSpec
from geopilot.llm.clarification import require_goal_clarification
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition
from geopilot.methods.validator import validate_method
from geopilot.plans.compiler import PlanCompilationError, compile_plan_steps
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.models import GOAL_CLARIFICATION_ISSUE, AnalysisPlan, PlanIssue
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.tools.registry import ToolRegistry


class PlanComparison(BaseModel):
    plan: AnalysisPlan
    criteria: ContextCriteria
    input_types: dict[str, str]
    option_specs: dict[str, JsonValue]
    preconditions: tuple[str, ...]
    measurement: dict[str, MeasurementPolicy]


class PlanAlternatives(BaseModel):
    baseline: PlanComparison
    alternatives: tuple[PlanComparison, ...]


def _artifact_types(method: MethodDefinition) -> dict[str, str]:
    return {artifact.role: artifact.type for artifact in method.expected_artifacts if not isinstance(artifact, str)}


def _comparison(
    plan: AnalysisPlan, method: MethodDefinition, criteria: ContextCriteria, registry: ToolRegistry,
) -> PlanComparison:
    measurement = {}
    for step in plan.steps:
        if step.tool is None:
            continue
        try:
            registration = registry.get(step.tool.id, step.tool.version)
        except KeyError:
            continue
        measurement[step.key] = registration.contract.measurement_policy
    return PlanComparison(
        plan=plan, criteria=criteria, input_types=method.inputs, preconditions=method.preconditions, measurement=measurement,
        option_specs={name: spec if isinstance(spec, str) else spec.model_dump(mode="json", exclude_none=True)
                      for name, spec in method.critical_options.items()},
    )


def _compatible_option(source, target) -> bool:
    if isinstance(source, ParameterSpec) and isinstance(target, ParameterSpec):
        return source.type == target.type and source.unit == target.unit
    return source == target


def compare_plan_alternatives(
    database_path: str | Path, plan: AnalysisPlan, *, catalog: MethodCatalog, registry: ToolRegistry,
    registered_rules: Iterable[str], sensor_registry: SensorConfigRegistry | None = None,
) -> PlanAlternatives:
    """Recheck candidate bindings and local assets against the saved task goal."""
    source = catalog.get(plan.method.id, plan.method.version)
    saved = current_task_criteria(
        database_path, task_id=plan.task_id, method=plan.method, goal=plan.goal, selected=plan.inputs,
    )
    expected = _artifact_types(source)
    fully_typed = bool(expected) and len(expected) == len(source.expected_artifacts)
    rules = tuple(registered_rules)
    alternatives = []
    for method in catalog.list_enabled():
        if method.id != source.id and (
            not fully_typed or _artifact_types(method) != expected or len(method.expected_artifacts) != len(expected)
        ):
            continue
        compatible_roles = {role for role, kind in method.inputs.items() if source.inputs.get(role) == kind}
        selected = {role: ref for role, ref in plan.inputs.items() if role in compatible_roles}
        artifact_roles = {item if isinstance(item, str) else item.role for item in method.expected_artifacts}
        criteria = ContextCriteria(
            scope=saved.scope, object_types={role: kind for role, kind in saved.object_types.items() if role in compatible_roles},
            expected_artifact=saved.expected_artifact if saved.expected_artifact in artifact_roles else None,
        )
        derived = set(derived_method_options(source.ref)) | set(derived_method_options(method.ref))
        inherited = {
            name: value for name, value in plan.critical_options.items()
            if name in source.critical_options and name in method.critical_options and name not in derived
            and (
                source.critical_options[name] == method.critical_options[name]
                or (method.id == source.id and _compatible_option(source.critical_options[name], method.critical_options[name]))
            )
        }
        variants = method.step_variants
        if variants is not None:
            current_mode = variants.selected(plan.critical_options) if method.ref == plan.method else None
            selections = [{**inherited, variants.option: mode} for mode in variants.choices if mode != current_mode]
        elif method.ref == plan.method:
            continue
        else:
            selector = source.step_variants.option if source.step_variants is not None else None
            spec = method.critical_options.get(selector) if selector is not None else None
            if selector is not None and isinstance(spec, ParameterSpec) and spec.type == "enum":
                selections = [{**inherited, selector: mode} for mode in spec.values or ()]
            else:
                selections = [inherited]
        for options in selections:
            options = {name: value for name, value in options.items() if name not in method.inactive_variant_options(options)}
            if validate_method(method, registry=registry, registered_rules=rules, options=options):
                continue
            context = build_task_context(
                database_path, task_id=plan.task_id, goal=plan.goal, method=method, selected=selected,
                criteria=criteria, registry=registry, options=options,
            )
            context, resolved = resolve_draft_options(database_path, context, options, sensor_registry=sensor_registry)
            candidate = build_draft(
                context, method, plan_id=plan.id, version=plan.version, options=resolved, registry=registry,
            )
            if any(issue.code == GOAL_CLARIFICATION_ISSUE.code for issue in plan.issues):
                candidate = require_goal_clarification(candidate)
            if not candidate.issues:
                try:
                    compile_plan_steps(candidate, method, registry=registry)
                except PlanCompilationError as error:
                    candidate = candidate.model_copy(update={
                        "issues": (PlanIssue(code=error.code, field=error.field, message=error.message),),
                    })
            alternatives.append(_comparison(candidate, method, criteria, registry))
    return PlanAlternatives(baseline=_comparison(plan, source, saved, registry), alternatives=tuple(alternatives))
