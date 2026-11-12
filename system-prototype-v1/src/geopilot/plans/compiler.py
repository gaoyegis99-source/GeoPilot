"""Compile versioned analysis plans and resolve inputs for individual requests."""

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TypeVar

from pydantic import BaseModel, JsonValue, ValidationError

from geopilot.domain.errors import _validate_relative_path
from geopilot.domain.step_io import ExecutionStatus, StepFileRef, StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.models import AssetRoleBinding, MethodDefinition, MethodStep, OptionBinding, StepOutputBinding
from geopilot.methods.validator import _input_types_overlap
from geopilot.persistence.workspace import _RUN_DIRECTORIES, _validate_component
from geopilot.plans.dependency_validator import validate_dependencies
from geopilot.plans.models import AnalysisPlan, PlanStatus, PlanStep
from geopilot.plans.step_compiler import validate_step_order
from geopilot.tools.parameter_normalizer import ParameterNormalizationError, normalize_parameters
from geopilot.tools.registry import ToolRegistry


_Model = TypeVar("_Model", bound=BaseModel)
_STEP_RECORDS = ("process.log", "request.json", "result.json", "python-result.json")


class PlanCompilationError(ValueError):
    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


def _validate_model(model: type[_Model], value: object, *, code: str, field: str = "") -> _Model:
    try:
        return model.model_validate(value)
    except ValidationError as error:
        issue = error.errors(include_input=False, include_url=False, include_context=False)[0]
        location = ".".join(map(str, issue["loc"]))
        field = ".".join(part for part in (field, location) if part) or "$"
        raise PlanCompilationError(code, field, issue["msg"]) from error


def _require_keys(
    expected: Mapping[str, object], actual: Mapping[str, object], *, field: str, missing: str, unknown: str,
) -> None:
    for key in expected:
        if key not in actual:
            raise PlanCompilationError(missing, f"{field}.{key}", f"Missing declaration for {key!r}")
    for key in actual:
        if key not in expected:
            raise PlanCompilationError(unknown, f"{field}.{key}", f"Unexpected declaration for {key!r}")


def _log_path(step_key: str, field: str) -> str:
    try:
        _validate_component(step_key, "step_key")
        return _validate_relative_path(f"steps/{step_key}/process.log")
    except ValueError as error:
        raise PlanCompilationError("INVALID_STEP_KEY", field, str(error)) from error


def _check_output_targets(steps: tuple[PlanStep, ...]) -> tuple[set[PurePosixPath], set[PurePosixPath]]:
    files = {PurePosixPath("request.json"), PurePosixPath("cancel.flag")}
    directories = {PurePosixPath(name) for name in _RUN_DIRECTORIES}
    for index, step in enumerate(steps):
        log = PurePosixPath(_log_path(step.key, f"steps.{index}.key"))
        files.update(log.with_name(name) for name in _STEP_RECORDS)
        directories.update(log.parents)

    for index, step in enumerate(steps):
        for port, output in step.outputs.items():
            target = PurePosixPath(output.target)
            if target in files or target in directories or files.intersection(target.parents):
                raise PlanCompilationError(
                    "OUTPUT_TARGET_CONFLICT", f"steps.{index}.outputs.{port}.target",
                    f"Output target {output.target!r} conflicts with another file or a run directory",
                )
            files.add(target)
            directories.update(target.parents)
    return files, directories


def _same_json(left: JsonValue, right: JsonValue) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_json(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right))
    return left == right


def _bound_parameters(
    step: PlanStep, template: MethodStep, contract: ToolContract,
    plan: AnalysisPlan, method: MethodDefinition, field: str,
) -> dict[str, JsonValue]:
    raw = {}
    for name, binding in template.parameters.items():
        if isinstance(binding, OptionBinding):
            option = binding.option
            if option not in method.critical_options:
                raise PlanCompilationError("UNKNOWN_OPTION", f"{field}.{name}.option", f"Unknown option {option!r}")
            if option not in plan.critical_options:
                raise PlanCompilationError("OPTION_REQUIRED", f"critical_options.{option}", f"Provide a value for {option!r}")
            raw[name] = plan.critical_options[option]
        else:
            raw[name] = binding.literal

    try:
        canonical = normalize_parameters(contract, raw)
    except ParameterNormalizationError as error:
        location = error.field.removeprefix("parameters")
        raise PlanCompilationError(error.code, f"{field}{location}", error.message) from error

    for name in raw:
        if name not in step.parameters:
            raise PlanCompilationError("MISSING_PARAMETER", f"{field}.{name}", f"The snapshot is missing {name!r}")
    for name, stored in step.parameters.items():
        parameter = f"{field}.{name}"
        if name not in canonical:
            raise PlanCompilationError("UNBOUND_PARAMETER", parameter, f"No method binding or contract default for {name!r}")
        if _same_json(stored, canonical[name]) or (name in raw and _same_json(stored, raw[name])):
            continue
        raise PlanCompilationError("PARAMETER_MISMATCH", parameter, f"Stored parameter {name!r} differs from its binding")
    return canonical


def _check_template(step: PlanStep, template: MethodStep, field: str) -> None:
    _require_keys(template.inputs, step.inputs, field=f"{field}.inputs",
                  missing="INPUT_BINDING_MISMATCH", unknown="INPUT_BINDING_MISMATCH")
    for port, binding in step.inputs.items():
        if binding != template.inputs[port]:
            raise PlanCompilationError("INPUT_BINDING_MISMATCH", f"{field}.inputs.{port}", "Input binding differs from the method")
    _require_keys(template.outputs, step.outputs, field=f"{field}.outputs",
                  missing="OUTPUT_BINDING_MISMATCH", unknown="OUTPUT_BINDING_MISMATCH")
    for port, output in step.outputs.items():
        if output.type != template.outputs[port]:
            raise PlanCompilationError("OUTPUT_BINDING_MISMATCH", f"{field}.outputs.{port}.type", "Output type differs from the method")


def _check_contract_ports(
    step: PlanStep, contract: ToolContract, method: MethodDefinition, steps: Mapping[str, PlanStep], field: str,
) -> None:
    _require_keys(contract.inputs, step.inputs, field=f"{field}.inputs", missing="MISSING_INPUT_PORT", unknown="UNKNOWN_INPUT_PORT")
    _require_keys(contract.outputs, step.outputs, field=f"{field}.outputs", missing="MISSING_OUTPUT_PORT", unknown="UNKNOWN_OUTPUT_PORT")
    for port, binding in step.inputs.items():
        if isinstance(binding, AssetRoleBinding):
            declared = method.inputs[binding.asset]
        else:
            declared = steps[binding.step].outputs[binding.output].type
        expected = contract.inputs[port]
        if not _input_types_overlap(declared, expected):
            raise PlanCompilationError(
                "INPUT_TYPE_MISMATCH", f"{field}.inputs.{port}", f"Input type {declared!r} is incompatible with {expected!r}",
            )
    for port, output in step.outputs.items():
        if output.type != contract.outputs[port]:
            raise PlanCompilationError("OUTPUT_TYPE_MISMATCH", f"{field}.outputs.{port}.type", "Output type differs from the tool contract")


def compile_plan_steps(
    plan: AnalysisPlan, method: MethodDefinition, *, registry: ToolRegistry,
) -> tuple[PlanStep, ...]:
    """Check the template and return steps in the plan's order and contract units."""
    plan = _validate_model(AnalysisPlan, plan, code="INVALID_PLAN")
    method = _validate_model(MethodDefinition, method, code="INVALID_METHOD", field="method")
    if plan.method != method.ref:
        raise PlanCompilationError("METHOD_MISMATCH", "method", "The plan must match the exact method version")
    if plan.issues:
        issue = plan.issues[0]
        raise PlanCompilationError("PLAN_HAS_ISSUES", issue.field, issue.message)
    _require_keys(method.inputs, plan.inputs, field="inputs", missing="MISSING_ASSET_ROLE", unknown="UNKNOWN_ASSET_ROLE")
    for option in plan.critical_options:
        if option not in method.critical_options:
            raise PlanCompilationError("UNKNOWN_OPTION", f"critical_options.{option}", f"Unknown option {option!r}")
    variants = method.step_variants
    if variants is not None and variants.selected(plan.critical_options) is None:
        code = "INVALID_OPTION" if variants.option in plan.critical_options else "OPTION_REQUIRED"
        raise PlanCompilationError(
            code, f"critical_options.{variants.option}", "Select a declared option to bind the method's tools",
        )
    inactive = method.inactive_variant_options(plan.critical_options)
    for option in plan.critical_options:
        if option in inactive:
            raise PlanCompilationError(
                "OPTION_NOT_APPLICABLE", f"critical_options.{option}", "This option does not apply to the selected method variant",
            )

    order_issues = validate_step_order(method, plan.steps)
    if order_issues:
        issue = order_issues[0]
        raise PlanCompilationError(issue.code, issue.field, issue.message)
    if not plan.steps:
        raise PlanCompilationError("MISSING_STEP", "steps", "The plan requires at least one step")
    templates = {}
    for index, template in enumerate(method.steps_for(plan.critical_options)):
        if template.key in templates:
            raise PlanCompilationError("DUPLICATE_STEP_KEY", f"method.steps.{index}.key", f"Duplicate step {template.key!r}")
        templates[template.key] = template
    steps = {step.key: step for step in plan.steps}
    _require_keys(templates, steps, field="steps", missing="MISSING_STEP", unknown="UNKNOWN_STEP")
    _check_output_targets(plan.steps)

    compiled = []
    for index, step in enumerate(plan.steps):
        field = f"steps.{index}"
        template = templates[step.key]
        if step.tool is None or template.tool is None:
            raise PlanCompilationError("TOOL_REQUIRED", f"{field}.tool", "Bind an exact tool version")
        if step.tool != template.tool:
            raise PlanCompilationError("TOOL_MISMATCH", f"{field}.tool", "The tool version differs from the method")
        try:
            registered = registry.get(step.tool.id, step.tool.version)
        except KeyError as error:
            raise PlanCompilationError("TOOL_UNAVAILABLE", f"{field}.tool", "The exact tool version is not registered") from error
        if not registered.available:
            raise PlanCompilationError("TOOL_UNAVAILABLE", f"{field}.tool", registered.reason or "The tool is unavailable")
        _check_template(step, template, field)
        _check_contract_ports(step, registered.contract, method, steps, field)
        parameters = _bound_parameters(step, template, registered.contract, plan, method, f"{field}.parameters")
        compiled.append(PlanStep(key=step.key, tool=step.tool, inputs=step.inputs, parameters=parameters, outputs=step.outputs))
    return tuple(compiled)


def _prior_output(
    binding: StepOutputBinding, producer: PlanStep, run_id: str, results: Mapping[str, StepResult],
) -> StepFileRef:
    field = f"prior_results.{binding.step}"
    result = results.get(binding.step)
    if result is None:
        raise PlanCompilationError("PRIOR_RESULT_MISSING", field, f"Missing result for step {binding.step!r}")
    result = _validate_model(StepResult, result, code="INVALID_PRIOR_RESULT", field=field)
    if result.run_id != run_id:
        raise PlanCompilationError("PRIOR_RESULT_MISMATCH", f"{field}.run_id", "The result belongs to a different run")
    if result.step_key != binding.step:
        raise PlanCompilationError("PRIOR_RESULT_MISMATCH", f"{field}.step_key", "The result belongs to a different step")
    if result.status is not ExecutionStatus.SUCCEEDED:
        raise PlanCompilationError("PRIOR_STEP_FAILED", f"{field}.status", "The preceding step did not succeed")

    field = f"{field}.outputs.{binding.output}"
    output = result.outputs.get(binding.output)
    if output is None:
        raise PlanCompilationError("PRIOR_OUTPUT_MISSING", field, f"Missing named output {binding.output!r}")
    expected = producer.outputs[binding.output]
    for name, actual, declared in (("path", output.path, expected.target), ("type", output.type, expected.type),
                                  ("layer", output.layer, expected.layer)):
        if name == "layer" and declared is None:
            continue
        if actual != declared:
            raise PlanCompilationError("PRIOR_OUTPUT_MISMATCH", f"{field}.{name}", f"Output {name} differs from the confirmed plan")
    return StepFileRef(path=output.path, layer=output.layer)


def compile_step_request(
    plan: AnalysisPlan, step_key: str, *, run_id: str,
    asset_files: Mapping[str, StepFileRef], prior_results: Mapping[str, StepResult], workdir_ref: str | None = None,
) -> StepRequest:
    """Resolve a confirmed step using staged files and reported earlier outputs."""
    plan = _validate_model(AnalysisPlan, plan, code="INVALID_PLAN")
    if plan.status is not PlanStatus.CONFIRMED:
        raise PlanCompilationError("PLAN_NOT_CONFIRMED", "status", "Confirm the plan before compiling a request")
    dependencies = validate_dependencies(plan.inputs, plan.steps)
    if dependencies:
        issue = dependencies[0]
        raise PlanCompilationError(issue.code, issue.field, issue.message)
    steps = {step.key: (index, step) for index, step in enumerate(plan.steps)}
    if step_key not in steps:
        raise PlanCompilationError("UNKNOWN_STEP", "step_key", f"Unknown step {step_key!r}")
    index, step = steps[step_key]
    files, directories = _check_output_targets(plan.steps)

    inputs = {}
    for port, binding in step.inputs.items():
        if isinstance(binding, StepOutputBinding):
            inputs[port] = _prior_output(binding, steps[binding.step][1], run_id, prior_results)
            continue
        reference = asset_files.get(binding.asset)
        if reference is None:
            raise PlanCompilationError("ASSET_FILE_MISSING", f"steps.{index}.inputs.{port}.asset",
                                       f"Stage a file for asset role {binding.asset!r}")
        field = f"asset_files.{binding.asset}"
        reference = _validate_model(StepFileRef, reference, code="INVALID_ASSET_FILE", field=field)
        path = PurePosixPath(reference.path)
        if path in files or path in directories or files.intersection(path.parents):
            raise PlanCompilationError("ASSET_FILE_CONFLICT", f"{field}.path", "The staged asset conflicts with a run file or target")
        inputs[port] = reference

    outputs = {port: StepFileRef(path=output.target, layer=output.layer) for port, output in step.outputs.items()}
    return _validate_model(StepRequest, {
        "run_id": run_id, "step_key": step.key, "tool": step.tool,
        "workdir_ref": run_id if workdir_ref is None else workdir_ref, "inputs": inputs,
        "parameters": step.parameters, "output_targets": outputs,
        "log_path": _log_path(step.key, f"steps.{index}.key"), "cancel_path": "cancel.flag",
    }, code="INVALID_STEP_REQUEST")
