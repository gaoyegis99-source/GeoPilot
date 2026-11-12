"""Expand a method template into an editable plan for selected asset versions."""

import re
from collections.abc import Mapping
from hashlib import sha256

from pydantic import JsonValue, ValidationError

from geopilot.application.task_context import InputReadiness, TaskContext
from geopilot.checks.tool_checks import tool_check_references
from geopilot.domain.data_asset import DataAssetRef
from geopilot.domain.tool_contract import ParameterSpec, _JSON_VALUE
from geopilot.methods.models import (
    ExpectedArtifactBinding,
    LiteralBinding,
    MethodDefinition,
    OptionBinding,
    ReferenceOption,
)
from geopilot.plans.dependency_validator import validate_dependencies
from geopilot.plans.models import AnalysisPlan, PlanIssue, PlanOutput, PlanStatus, PlanStep
from geopilot.tools.parameter_normalizer import ParameterNormalizationError, _convert_quantity, normalize_parameters
from geopilot.tools.registry import ToolRegistry


_SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,47}")
_OUTPUT_EXTENSIONS = {
    "vector": "gpkg",
    "raster": "tif",
    "table": "csv",
    "table_csv": "csv",
    "json": "json",
}


def _selected_inputs(
    context: TaskContext, method: MethodDefinition, issues: list[PlanIssue]
) -> dict[str, DataAssetRef]:
    inputs = {}
    for role in method.inputs:
        assessment = context.roles.get(role)
        field = f"inputs.{role}"
        if assessment is None or assessment.asset is None:
            issues.append(PlanIssue(code="INPUT_REQUIRED", field=field, message=f"Select an asset for {role}"))
            continue
        inputs[role] = assessment.asset
        if assessment.status is not InputReadiness.READY:
            issues.append(PlanIssue(
                code=assessment.code or "INPUT_NOT_READY", field=assessment.field or field,
                message=assessment.message or f"The selected asset is {assessment.status.value.lower()}",
            ))
    return inputs


def _option_values(
    method: MethodDefinition, options: Mapping[str, JsonValue] | None, issues: list[PlanIssue]
) -> dict[str, JsonValue]:
    supplied = {} if options is None else options
    if not isinstance(supplied, Mapping):
        issues.append(PlanIssue(code="INVALID_OPTION", field="critical_options", message="Expected an object of options"))
        supplied = {}

    declared = method.critical_options
    for name in supplied:
        if name not in declared:
            field = f"critical_options.{name}" if isinstance(name, str) and name.strip() else "critical_options"
            issues.append(PlanIssue(code="UNKNOWN_OPTION", field=field, message="The method does not declare this option"))

    values = {}
    for name, spec in declared.items():
        if name not in supplied:
            if isinstance(spec, ParameterSpec) and spec.default is not None:
                values[name] = spec.default
            continue
        value = supplied[name]
        try:
            if isinstance(spec, ParameterSpec):
                if spec.type in {"integer", "number"} and spec.unit is not None and isinstance(value, Mapping):
                    spec.validate_value(_convert_quantity(spec, value))
                    value = _JSON_VALUE.validate_python(value)
                else:
                    spec.validate_value(value)
            elif isinstance(spec, ReferenceOption):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("Expected a nonempty reference identifier")
            else:
                value = _JSON_VALUE.validate_python(value)
        except (ValueError, OverflowError) as error:
            message = "Expected a finite JSON value" if isinstance(error, ValidationError) else str(error)
            issues.append(PlanIssue(code="INVALID_OPTION", field=f"critical_options.{name}", message=message))
        else:
            values[name] = value

    selected_conditions = {
        value for name, value in values.items()
        if isinstance(value, str) and not isinstance(declared[name], ReferenceOption)
    }
    bound_options = {
        binding.option for step in method.steps_for(values) for binding in step.parameters.values()
        if isinstance(binding, OptionBinding)
    }
    inactive_options = method.inactive_variant_options(values)
    for name, spec in declared.items():
        if name in inactive_options:
            if name in supplied:
                issues.append(PlanIssue(
                    code="OPTION_NOT_APPLICABLE", field=f"critical_options.{name}",
                    message="This option does not apply to the selected method variant",
                ))
                values.pop(name, None)
            continue
        if name in supplied or name in values:
            continue
        if isinstance(spec, ReferenceOption):
            required = spec.required_for is None or spec.required_for in selected_conditions
        elif isinstance(spec, ParameterSpec):
            required = spec.required
        else:
            required = True
        if required or name in bound_options:
            issues.append(PlanIssue(
                code="OPTION_REQUIRED", field=f"critical_options.{name}", message=f"Provide a value for {name}",
            ))
    return values


def _safe_component(name: str) -> str:
    if _SAFE_NAME.fullmatch(name) and not name.lower().startswith(("gpkg_", "sqlite_")):
        return name
    return f"encoded_{sha256(name.encode('utf-8')).hexdigest()}"


def _output_target(step: str, port: str, kind: str) -> PlanOutput | None:
    family = kind.split("_", 1)[0] if kind.startswith(("vector_", "raster_")) else kind
    extension = _OUTPUT_EXTENSIONS.get(family)
    if extension is None:
        return None
    output_name = _safe_component(port)
    return PlanOutput(
        type=kind, target=f"intermediate/{_safe_component(step)}/{output_name}.{extension}",
        layer=output_name if extension == "gpkg" else None,
    )


def _expand_steps(
    method: MethodDefinition, options: dict[str, JsonValue], issues: list[PlanIssue]
) -> list[PlanStep]:
    steps = []
    templates = method.steps_for(options)
    variants = method.step_variants
    awaiting_mode = variants is not None and variants.selected(options) is None
    if not templates:
        issues.append(PlanIssue(code="STEP_REQUIRED", field="steps", message="The method needs at least one step"))
    for index, step in enumerate(templates):
        field = f"steps.{index}"
        if step.tool is None and not awaiting_mode:
            issues.append(PlanIssue(code="TOOL_REQUIRED", field=f"{field}.tool", message=f"Bind a tool version for {step.key}"))

        parameters = {}
        for port, binding in step.parameters.items():
            if isinstance(binding, LiteralBinding):
                parameters[port] = binding.literal
            elif binding.option in options:
                parameters[port] = options[binding.option]
            elif binding.option not in method.critical_options:
                issues.append(PlanIssue(
                    code="UNKNOWN_OPTION", field=f"{field}.parameters.{port}.option",
                    message=f"The method does not declare {binding.option!r}",
                ))

        outputs = {}
        for port, kind in step.outputs.items():
            output = _output_target(step.key, port, kind)
            if output is None:
                issues.append(PlanIssue(
                    code="UNSUPPORTED_OUTPUT_TYPE", field=f"{field}.outputs.{port}",
                    message=f"The output type {kind!r} has no supported file format",
                ))
            else:
                outputs[port] = output
        steps.append(PlanStep(key=step.key, tool=step.tool, inputs=step.inputs, parameters=parameters, outputs=outputs))
    return steps


def _expected_artifacts(
    method: MethodDefinition, outputs: dict[str, dict[str, PlanOutput]], issues: list[PlanIssue]
) -> list[ExpectedArtifactBinding]:
    artifacts = []
    for index, artifact in enumerate(method.expected_artifacts):
        field = f"expected_artifacts.{index}"
        if isinstance(artifact, str):
            issues.append(PlanIssue(code="ARTIFACT_UNBOUND", field=field, message=f"Bind the output for {artifact!r}"))
            continue
        artifacts.append(artifact)
        if artifact.step not in outputs:
            issues.append(PlanIssue(code="ARTIFACT_UNBOUND", field=f"{field}.step", message=f"Unknown step {artifact.step!r}"))
            continue
        output = outputs[artifact.step].get(artifact.output)
        if output is None:
            issues.append(PlanIssue(
                code="ARTIFACT_UNBOUND", field=f"{field}.output",
                message=f"Step {artifact.step!r} has no available output {artifact.output!r}",
            ))
        elif output.type != artifact.type:
            issues.append(PlanIssue(
                code="ARTIFACT_TYPE_MISMATCH", field=f"{field}.type", message=f"The bound output has type {output.type!r}",
            ))
    return artifacts


def _apply_tool_contracts(
    steps: list[PlanStep], method: MethodDefinition, values: dict[str, JsonValue], registry: ToolRegistry,
    issues: list[PlanIssue],
) -> None:
    variants = method.step_variants
    selected_mode = variants is not None and variants.selected(values) is not None
    for index, (step, template) in enumerate(zip(steps, method.steps_for(values), strict=True)):
        if step.tool is None:
            continue
        tool_field = f"steps.{index}.tool"
        try:
            registration = registry.get(step.tool.id, step.tool.version)
        except KeyError:
            if selected_mode:
                issues.append(PlanIssue(
                    code="TOOL_NOT_REGISTERED", field=tool_field, message="The selected tool version is not registered",
                ))
            continue
        contract = registration.contract
        steps[index] = step.model_copy(update={"tool_check_rules": tool_check_references(contract.check_rules)})
        if not selected_mode:
            continue
        if not registration.available:
            issues.append(PlanIssue(
                code="TOOL_UNAVAILABLE", field=tool_field, message=registration.reason or "The selected tool is unavailable",
            ))
        elif template.parameters.keys() == step.parameters.keys():
            try:
                normalize_parameters(contract, step.parameters)
            except ParameterNormalizationError as error:
                binding = template.parameters.get(error.field.removeprefix("parameters."))
                field = (f"critical_options.{binding.option}" if isinstance(binding, OptionBinding)
                         else f"steps.{index}.{error.field}")
                issue = PlanIssue(code=error.code, field=field, message=error.message)
                if issue not in issues:
                    issues.append(issue)


def build_draft(
    context: TaskContext,
    method: MethodDefinition,
    *,
    plan_id: str,
    version: int = 1,
    options: Mapping[str, JsonValue] | None = None,
    registry: ToolRegistry | None = None,
) -> AnalysisPlan:
    """Build an ordered draft and report unresolved choices and bindings."""
    if context.method != method.ref:
        raise ValueError("The task context must match the method id and version")
    issues: list[PlanIssue] = []
    inputs = _selected_inputs(context, method, issues)
    values = _option_values(method, options, issues)
    steps = _expand_steps(method, values, issues)
    if registry is not None:
        _apply_tool_contracts(steps, method, values, registry, issues)
    outputs = {step.key: step.outputs for step in steps}
    issues.extend(
        PlanIssue(code=issue.code, field=issue.field, message=issue.message)
        for issue in validate_dependencies(method.inputs, steps)
    )
    artifacts = _expected_artifacts(method, outputs, issues)
    return AnalysisPlan(
        id=plan_id, task_id=context.task_id, version=version, status=PlanStatus.DRAFT,
        goal=context.goal, inputs=inputs, critical_options=values, method=method.ref,
        steps=steps, expected_artifacts=artifacts, check_rules=method.check_rules, issues=issues,
    )
