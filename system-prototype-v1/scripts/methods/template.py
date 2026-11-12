"""Create an editable method scaffold for one existing, exact tool version."""

import json
from pathlib import Path

import yaml
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from geopilot.api.server import _yaml_files
from geopilot.domain.tool_contract import ToolContract, ToolRef
from geopilot.methods.loader import _UniqueKeySafeLoader
from geopilot.methods.models import MethodDefinition, MethodRef


class TemplateError(ValueError):
    def __init__(self, code: str, field: str, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.exit_code = exit_code


def parse_reference(value: str, field: str, model: type[ToolRef] | type[MethodRef]) -> ToolRef | MethodRef:
    identifier, separator, version = value.rpartition("@")
    try:
        if not separator or not version.isascii() or not version.isdecimal():
            raise ValueError("Expected a decimal version")
        return model(id=identifier, version=int(version))
    except ValueError as error:
        raise TemplateError("INVALID_REFERENCE", field, "Use ID@VERSION with a positive integer version") from error


def load_tool(config: Path, reference: ToolRef) -> ToolContract:
    directory = config / "tools"
    try:
        files = _yaml_files(directory)
    except OSError as error:
        raise TemplateError("INPUT_READ_ERROR", "config", str(error), exit_code=2) from error
    contracts = {}
    for path in files:
        try:
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
            contract = ToolContract.model_validate(raw)
        except OSError as error:
            raise TemplateError("INPUT_READ_ERROR", "config", str(error), exit_code=2) from error
        except ValidationError as error:
            first = error.errors(include_url=False, include_context=False, include_input=False)[0]
            field = ".".join(map(str, first["loc"])) or "$"
            raise TemplateError("INVALID_TOOL_CONFIG", "config", f"{path.name}: {field}: {first['msg']}") from error
        except (UnicodeError, RecursionError, yaml.YAMLError) as error:
            raise TemplateError("INVALID_TOOL_CONFIG", "config", f"{path.name}: {error}") from error
        if contract.ref in contracts:
            raise TemplateError(
                "DUPLICATE_TOOL_REFERENCE", "config", f"{path.name}: duplicate tool {contract.id}@{contract.version}",
            )
        contracts[contract.ref] = contract

    try:
        return contracts[reference]
    except KeyError as error:
        raise TemplateError(
            "TOOL_NOT_REGISTERED", "tool", f"No registered tool matches {reference.id}@{reference.version}",
        ) from error


def generate_template(config: Path, tool: str, method: str) -> dict:
    tool_reference = parse_reference(tool, "tool", ToolRef)
    method_reference = parse_reference(method, "method", MethodRef)
    contract = load_tool(config, tool_reference)
    options = {}
    bindings = {}
    todos = []
    for name, spec in contract.parameters.items():
        # The plan compiler requires a value for every bound option.
        if spec.required or spec.default is not None:
            options[name] = spec
            bindings[name] = {"option": name}
        if spec.default is None:
            todos.append({
                "code": "REQUIRED_OPTION_VALUE" if spec.required else "OPTIONAL_PARAMETER_UNBOUND",
                "field": f"tool.parameters.{name}",
                "message": (
                    f"Supply a value for the plan option {name!r}; the contract has no default."
                    if spec.required else f"Bind {name!r} explicitly if the method needs this optional parameter."
                ),
            })

    step = "run_tool"
    definition = MethodDefinition.model_validate({
        "id": method_reference.id,
        "version": method_reference.version,
        "locked_order": True,
        "inputs": contract.inputs,
        "critical_options": options,
        "preconditions": ["required_input_roles_available"],
        "steps": [{
            "key": step,
            "tool": tool_reference.model_dump(),
            "inputs": {role: {"asset": role} for role in contract.inputs},
            "parameters": bindings,
            "outputs": contract.outputs,
        }],
        "expected_artifacts": [
            {"role": role, "step": step, "output": role, "type": kind}
            for role, kind in contract.outputs.items()
        ],
        "check_rules": [],
    })
    try:
        return {
            "scope": "method_template",
            "tool": json.loads(contract.model_dump_json(exclude_none=True)),
            "method": json.loads(definition.model_dump_json(exclude_none=True)),
            "todos": todos,
        }
    except PydanticSerializationError as error:
        raise TemplateError("INVALID_TOOL_CONFIG", "config", "Tool values must be valid UTF-8 JSON") from error
