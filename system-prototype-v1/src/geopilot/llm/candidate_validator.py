"""Revalidate a model candidate and prepare a local, unconfirmed analysis draft."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from geopilot.application.draft_options import resolve_draft_options
from geopilot.application.task_context import build_task_context
from geopilot.domain.data_asset import DataAssetRef
from geopilot.domain.task import TaskGoal
from geopilot.llm.base import ModelRequest
from geopilot.llm.prompt_builder import describe_method_candidate
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition, MethodRef, OptionBinding
from geopilot.methods.validator import validate_method
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.models import AnalysisPlan, PlanIssue
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.tools.parameter_normalizer import ParameterNormalizationError, normalize_parameters
from geopilot.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class CandidateValidation:
    plan: AnalysisPlan | None
    issues: tuple[PlanIssue, ...]


def _rejected(code: str, field: str, message: str) -> CandidateValidation:
    return CandidateValidation(None, (PlanIssue(code=code, field=field, message=message),))


def _tool_parameter_issues(
    plan: AnalysisPlan, definition: MethodDefinition, registry: ToolRegistry,
) -> tuple[PlanIssue, ...]:
    issues = []
    for actual, declared in zip(plan.steps, definition.steps, strict=True):
        if actual.tool is None:
            continue
        contract = registry.get(actual.tool.id, actual.tool.version).contract
        try:
            normalize_parameters(contract, actual.parameters)
        except ParameterNormalizationError as error:
            name = error.field.removeprefix("parameters.")
            binding = declared.parameters.get(name)
            field = f"critical_options.{binding.option}" if isinstance(binding, OptionBinding) else f"steps.{actual.key}.{error.field}"
            issues.append(PlanIssue(code=error.code, field=field, message=error.message))
    return tuple(issues)


def validate_model_candidate(
    database_path: str | Path,
    *,
    task_id: str,
    plan_id: str,
    method: MethodRef,
    goal: TaskGoal,
    inputs: Mapping[str, DataAssetRef],
    options: Mapping[str, JsonValue],
    request: ModelRequest,
    catalog: MethodCatalog,
    registry: ToolRegistry,
    registered_rules: Iterable[str],
    version: int = 1,
    sensor_registry: SensorConfigRegistry | None = None,
) -> CandidateValidation:
    """Use only the advertised candidate and current local contracts and assets."""
    candidates = request.payload.get("candidate_methods")
    advertised = [
        entry for entry in candidates
        if isinstance(entry, dict) and entry.get("id") == method.id
        and type(entry.get("version")) is int and entry["version"] == method.version
    ] if isinstance(candidates, list) else []
    if len(advertised) != 1:
        return _rejected("MODEL_CANDIDATE_UNKNOWN", "method", "Select a method from the exact advertised versions.")
    try:
        definition = catalog.get(method.id, method.version)
    except KeyError:
        return _rejected("MODEL_CANDIDATE_UNAVAILABLE", "method", "The candidate version is unavailable.")
    if validate_method(definition, registry=registry, registered_rules=registered_rules):
        return _rejected("MODEL_CANDIDATE_UNAVAILABLE", "method", "The candidate tools or rules are unavailable.")
    if describe_method_candidate(definition) != advertised[0]:
        return _rejected("MODEL_CANDIDATE_CHANGED", "method", "The candidate changed. Request a new suggestion.")
    if inputs.keys() - definition.inputs.keys():
        return _rejected("UNKNOWN_ASSET_ROLE", "inputs", "Choose assets for the declared input roles.")

    context = build_task_context(
        database_path, task_id=task_id, goal=goal, method=definition, selected=inputs, registry=registry,
    )
    context, options = resolve_draft_options(database_path, context, options, sensor_registry=sensor_registry)
    draft = build_draft(context, definition, plan_id=plan_id, version=version, options=options)
    if draft.issues:
        return CandidateValidation(draft, draft.issues)

    issues = _tool_parameter_issues(draft, definition, registry)
    return CandidateValidation(draft.model_copy(update={"issues": issues}) if issues else draft, issues)
