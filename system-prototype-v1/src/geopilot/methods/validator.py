"""Validate the static bindings of versioned analysis method definitions."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from os import PathLike
from pathlib import Path

from pydantic import JsonValue, ValidationError

from geopilot.checks.tool_checks import tool_check_version
from geopilot.domain.tool_contract import ParameterSpec, ToolContract
from geopilot.methods.models import AssetRoleBinding, MethodDefinition, MethodStep, OptionBinding
from geopilot.plans.dependency_validator import validate_dependencies
from geopilot.tools.parameter_normalizer import ParameterNormalizationError, normalize_parameters
from geopilot.tools.registry import ToolRegistry


@dataclass(frozen=True)
class MethodIssue:
    code: str
    field: str
    message: str
    path: Path | None = None


_Report = Callable[[str, str, str], None]


def _input_types_overlap(declared: str, expected: str) -> bool:
    if declared == expected:
        return True
    if declared.startswith("vector_") and expected.startswith("vector_"):
        declared_kinds = declared.removeprefix("vector_").split("_or_")
        expected_kinds = expected.removeprefix("vector_").split("_or_")
        return not set(declared_kinds).isdisjoint(expected_kinds)
    return any(
        (declared == family and expected.startswith(f"{family}_"))
        or (expected == family and declared.startswith(f"{family}_"))
        for family in ("vector", "raster", "table")
    )


def _check_ports(
    step: MethodStep, contract: ToolContract, assets: Mapping[str, str],
    steps: Mapping[str, MethodStep | None], field: str, report: _Report,
) -> None:
    for category, singular, provided, expected in (
        ("inputs", "INPUT", step.inputs, contract.inputs),
        ("outputs", "OUTPUT", step.outputs, contract.outputs),
    ):
        for port in expected:
            if port not in provided:
                report(f"MISSING_{singular}_PORT", f"{field}.{category}.{port}", f"Required port {port!r} is unbound")
        for port in provided:
            if port not in expected:
                report(f"UNKNOWN_{singular}_PORT", f"{field}.{category}.{port}", f"Port {port!r} is absent from the tool contract")

    for port, binding in step.inputs.items():
        expected_type = contract.inputs.get(port)
        if expected_type is None:
            continue
        if isinstance(binding, AssetRoleBinding):
            declared_type = assets.get(binding.asset)
        else:
            producer = steps.get(binding.step)
            declared_type = producer.outputs.get(binding.output) if producer is not None else None
        if declared_type is not None and not _input_types_overlap(declared_type, expected_type):
            report(
                "INPUT_TYPE_MISMATCH", f"{field}.inputs.{port}",
                f"Declared source type {declared_type!r} is incompatible with tool input type {expected_type!r}",
            )

    for port, declared_type in step.outputs.items():
        expected_type = contract.outputs.get(port)
        if expected_type is not None and declared_type != expected_type:
            report(
                "OUTPUT_TYPE_MISMATCH", f"{field}.outputs.{port}",
                f"Declared output type {declared_type!r} differs from tool output type {expected_type!r}",
            )


def _check_parameters(
    step: MethodStep, contract: ToolContract | None, method: MethodDefinition, field: str, report: _Report,
    *, selected: Mapping[str, JsonValue],
) -> None:
    if contract is not None:
        for name, spec in contract.parameters.items():
            if spec.required and spec.default is None and name not in step.parameters:
                report("MISSING_PARAMETER_BINDING", f"{field}.{name}", f"Required parameter {name!r} is unbound")

    for name, binding in step.parameters.items():
        binding_field = f"{field}.{name}"
        if contract is not None and name not in contract.parameters:
            report("UNKNOWN_PARAMETER", binding_field, f"Parameter {name!r} is absent from the tool contract")
            continue
        if isinstance(binding, OptionBinding):
            value_field = f"{binding_field}.option"
            option = method.critical_options.get(binding.option)
            if option is None:
                report("UNKNOWN_OPTION", value_field, f"Option {binding.option!r} is not declared by the method")
                continue
            if binding.option in selected:
                value = selected[binding.option]
            elif isinstance(option, ParameterSpec) and option.default is not None:
                value = option.default
            else:
                continue
        else:
            value_field = f"{binding_field}.literal"
            value = binding.literal

        if contract is not None:
            # A single known value can be checked before other options are selected.
            partial_contract = contract.model_copy(update={"parameters": {name: contract.parameters[name]}})
            try:
                normalize_parameters(partial_contract, {name: value})
            except ParameterNormalizationError as error:
                report("INVALID_PARAMETER_BINDING", value_field, error.message)


def _check_artifacts(method: MethodDefinition, steps: Mapping[str, MethodStep | None], report: _Report) -> None:
    roles: set[str] = set()
    for index, artifact in enumerate(method.expected_artifacts):
        field = f"expected_artifacts.{index}"
        role = artifact if isinstance(artifact, str) else artifact.role
        if role in roles:
            role_field = field if isinstance(artifact, str) else f"{field}.role"
            report("DUPLICATE_ARTIFACT_ROLE", role_field, f"Artifact role {role!r} is already declared")
        roles.add(role)

        if isinstance(artifact, str):
            report("MISSING_ARTIFACT_BINDING", field, f"Artifact role {role!r} requires a step, output and type")
            continue
        if artifact.step not in steps:
            report("UNKNOWN_ARTIFACT_STEP", f"{field}.step", f"Unknown producer step {artifact.step!r}")
            continue
        step = steps[artifact.step]
        if step is None:
            continue
        output_type = step.outputs.get(artifact.output)
        if output_type is None:
            report("UNKNOWN_ARTIFACT_OUTPUT", f"{field}.output", f"Step {step.key!r} has no output {artifact.output!r}")
        elif artifact.type != output_type:
            report(
                "ARTIFACT_TYPE_MISMATCH", f"{field}.type",
                f"Artifact type {artifact.type!r} differs from the step output type {output_type!r}",
            )


def _check_tool_rules(contract: ToolContract, field: str, report: _Report) -> None:
    seen = set()
    for index, name in enumerate(contract.check_rules):
        location = f"{field}.tool.check_rules.{index}"
        if name in seen:
            report("DUPLICATE_TOOL_CHECK_RULE", location, f"Tool check rule {name!r} is repeated")
        elif tool_check_version(name) is None:
            report("UNKNOWN_TOOL_CHECK_RULE", location, f"Tool check rule {name!r} is not registered")
        seen.add(name)


def _check_steps(
    definition: MethodDefinition, *, registry: ToolRegistry, selected: Mapping[str, JsonValue], report: _Report,
) -> None:
    ordered = definition.steps_for(selected)
    if not ordered:
        report("MISSING_STEPS", "steps", "An executable method requires at least one step")

    steps: dict[str, MethodStep | None] = {}
    for step in ordered:
        # A repeated key cannot identify a producer for type comparison.
        steps[step.key] = None if step.key in steps else step

    seen_steps: set[str] = set()
    for index, step in enumerate(ordered):
        field = f"steps.{index}"
        if step.key in seen_steps:
            report("DUPLICATE_STEP_KEY", f"{field}.key", f"Step key {step.key!r} is already declared")
        seen_steps.add(step.key)

        contract = None
        if step.tool is None:
            report("MISSING_TOOL_BINDING", f"{field}.tool", f"Step {step.key!r} requires an exact tool reference")
        else:
            tool = step.tool
            try:
                registration = registry.get(tool.id, tool.version)
            except KeyError:
                report("TOOL_NOT_REGISTERED", f"{field}.tool", f"Tool {tool.id!r} version {tool.version} is not registered")
            else:
                contract = registration.contract
                if not registration.available:
                    report(
                        "TOOL_UNAVAILABLE", f"{field}.tool",
                        f"Tool {tool.id!r} version {tool.version} is unavailable: {registration.reason}",
                    )
                _check_ports(step, contract, definition.inputs, steps, field, report)
                _check_tool_rules(contract, field, report)
        _check_parameters(step, contract, definition, f"{field}.parameters", report, selected=selected)

    if definition.step_variants is not None:
        for issue in validate_dependencies(definition.inputs, ordered):
            if issue.code != "DUPLICATE_STEP_KEY":
                report(issue.code, issue.field, issue.message)
    _check_artifacts(definition, steps, report)


def validate_method(
    method: MethodDefinition | Mapping[str, object], *, registry: ToolRegistry,
    registered_rules: Iterable[str], source: str | PathLike[str] | None = None,
    options: Mapping[str, JsonValue] | None = None,
) -> tuple[MethodIssue, ...]:
    """Return all located static binding issues in declaration order."""
    path = Path(source) if source is not None else None
    try:
        definition = MethodDefinition.model_validate(method)
    except ValidationError as error:
        return tuple(
            MethodIssue("INVALID_METHOD_CONFIG", ".".join(map(str, detail["loc"])) or "$", detail["msg"], path)
            for detail in error.errors(include_url=False, include_context=False, include_input=False)
        )

    issues: list[MethodIssue] = []

    def report(code: str, field: str, message: str) -> None:
        issues.append(MethodIssue(code, field, message, path))

    variants = definition.step_variants
    if variants is None:
        _check_steps(definition, registry=registry, selected={}, report=report)
    elif options is None:
        for mode in variants.choices:
            def report_variant(code: str, field: str, message: str) -> None:
                report(code, f"step_variants.choices.{mode}.{field}", message)

            _check_steps(definition, registry=registry, selected={variants.option: mode}, report=report_variant)
    else:
        mode = variants.selected(options)
        if mode is None:
            code = "INVALID_OPTION" if variants.option in options else "OPTION_REQUIRED"
            report(code, f"critical_options.{variants.option}", "Select a declared option to bind the method's tools")
        else:
            _check_steps(definition, registry=registry, selected={variants.option: mode}, report=report)
    known_rules = frozenset(registered_rules)
    for index, rule in enumerate(definition.check_rules):
        if rule not in known_rules:
            report("UNKNOWN_CHECK_RULE", f"check_rules.{index}", f"Check rule {rule!r} is not registered")
    return tuple(issues)
