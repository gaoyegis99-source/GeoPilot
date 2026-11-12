"""Describe only locally available, versioned method and tool candidates."""

import re
from collections.abc import Iterable

from pydantic import JsonValue

from geopilot.application.draft_options import derived_method_options
from geopilot.domain.tool_contract import ParameterSpec
from geopilot.llm.base import ModelRequest
from geopilot.llm.context_builder import OutboundContext
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition, OptionBinding, ReferenceOption
from geopilot.methods.validator import validate_method
from geopilot.tools.registry import ToolRegistry


_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}")
_PARAMETER_FIELDS = {"type", "values", "unit", "minimum", "maximum", "minimum_exclusive", "maximum_exclusive"}


def _safe_candidate(method: MethodDefinition) -> bool:
    labels = [method.id, *method.inputs, *method.inputs.values(), *method.critical_options]
    for step in method.steps:
        if step.tool is None:
            return False
        labels.extend((step.key, step.tool.id))
    for spec in method.critical_options.values():
        if isinstance(spec, ParameterSpec):
            labels.extend(spec.values or ())
            if spec.unit is not None:
                labels.append(spec.unit)
        elif isinstance(spec, ReferenceOption) and spec.required_for is not None:
            labels.append(spec.required_for)
    return all(_TOKEN.fullmatch(label) for label in labels)


def _options(method: MethodDefinition) -> list[dict[str, JsonValue]]:
    bound = {
        binding.option for step in method.steps for binding in step.parameters.values()
        if isinstance(binding, OptionBinding)
    }
    derived = derived_method_options(method.ref)
    result = []
    for name, spec in method.critical_options.items():
        if name in derived:
            continue
        if isinstance(spec, ParameterSpec):
            constraints = spec.model_dump(mode="json", include=_PARAMETER_FIELDS, exclude_none=True)
            required = spec.default is None and (spec.required or name in bound)
        elif isinstance(spec, ReferenceOption):
            constraints = spec.model_dump(mode="json", exclude_none=True)
            required = name in bound or spec.required_for is None
        else:
            constraints = {"type": spec}
            required = True
        result.append({"name": name, "required": required, **constraints})
    return result


def describe_method_candidate(method: MethodDefinition) -> dict[str, JsonValue] | None:
    """Project a method definition to the candidate fields safe to serialize."""
    if not _safe_candidate(method):
        return None
    return {
        **method.ref.model_dump(mode="json"),
        "inputs": [{"role": role, "type": kind} for role, kind in method.inputs.items()],
        "options": _options(method),
        "steps": [
            {"key": step.key, "tool": step.tool.model_dump(mode="json")}
            for step in method.steps if step.tool is not None
        ],
    }


def build_model_request(
    context: OutboundContext,
    catalog: MethodCatalog,
    *,
    registry: ToolRegistry,
    registered_rules: Iterable[str],
) -> ModelRequest:
    """Build a versioned request for subsequent outbound data-boundary validation."""
    rules = tuple(registered_rules)
    methods = []
    tools: set[tuple[str, int]] = set()
    for method in catalog.list():
        candidate = describe_method_candidate(method)
        if candidate is None or validate_method(method, registry=registry, registered_rules=rules):
            continue
        tools.update((step.tool.id, step.tool.version) for step in method.steps if step.tool is not None)
        methods.append(candidate)
    return ModelRequest(payload={
        "context": context.model_dump(mode="json", exclude_none=True),
        "candidate_methods": methods,
        "candidate_tools": [{"id": identifier, "version": version} for identifier, version in sorted(tools)],
    })
