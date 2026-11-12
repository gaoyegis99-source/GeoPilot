"""Statically check method definitions with the local versioned tool registry."""

import sys
from collections import Counter
from pathlib import Path

import yaml
from pydantic import ValidationError

# Use the same registry, YAML reader and rule sets as the P12 configuration audit.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.quality.validate_config_schemas import (
    METHOD_CHECKS,
    METHOD_RULES,
    ROAD_V1_EXAMPLE,
    StaticToolRegistry,
    read_yaml,
)

from geopilot.api.server import _worker_paths
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.models import MethodDefinition
from geopilot.methods.validator import validate_method
from geopilot.plans.dependency_validator import validate_dependencies


EXAMPLE_PATH = "config/methods/road-buffer-statistics.v1.yaml"


class LintError(ValueError):
    def __init__(self, errors: list[dict[str, str]], *, exit_code: int = 1) -> None:
        super().__init__(errors[0]["message"])
        self.errors = errors
        self.exit_code = exit_code


def _path_label(path: Path, config: Path) -> str:
    if path.is_relative_to(config):
        return (Path("config") / path.relative_to(config)).as_posix()
    return path.as_posix()


def _issue(path: Path, config: Path, code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message, "path": _path_label(path, config)}


def _files(config: Path, family: str) -> list[Path]:
    directory = config / ("methods" if family == "METHOD" else "tools")
    try:
        paths = sorted(
            path for path in directory.iterdir()
            if path.suffix in {".yaml", ".yml"} and not path.is_dir()
        )
    except OSError as error:
        raise LintError([_issue(directory, config, "INPUT_READ_ERROR", "config", str(error))], exit_code=2) from error
    if not paths:
        raise LintError([_issue(directory, config, f"INVALID_{family}_CONFIG", "$", "No YAML definitions found")])
    return paths


def _yaml_message(error: Exception) -> str:
    if isinstance(error, yaml.MarkedYAMLError):
        message = error.problem or "Invalid YAML"
        if error.problem_mark is not None:
            mark = error.problem_mark
            return f"{message} (line {mark.line + 1}, column {mark.column + 1})"
        return message
    if isinstance(error, UnicodeError):
        return "Expected UTF-8 YAML"
    return "Invalid YAML"


def _definitions(paths, config: Path, model: type[ToolContract] | type[MethodDefinition], family: str):
    definitions = []
    errors = []
    exit_code = 1
    for path in paths:
        try:
            if not path.is_file():
                raise OSError("Expected a regular YAML configuration file")
            definition = model.model_validate(read_yaml(path))
        except OSError as error:
            errors.append(_issue(path, config, "INPUT_READ_ERROR", "$", str(error)))
            exit_code = 2
        except ValidationError as error:
            errors.extend(
                _issue(path, config, f"INVALID_{family}_CONFIG", ".".join(map(str, detail["loc"])) or "$", detail["msg"])
                for detail in error.errors(include_url=False, include_context=False, include_input=False)
            )
        except (UnicodeError, RecursionError, yaml.YAMLError) as error:
            errors.append(_issue(path, config, f"INVALID_{family}_YAML", "$", _yaml_message(error)))
        else:
            definitions.append((path, definition))
    counts = Counter((definition.id, definition.version) for _, definition in definitions)
    for path, definition in definitions:
        if counts[definition.id, definition.version] > 1:
            errors.append(_issue(
                path, config, f"DUPLICATE_{family}_REFERENCE", "version",
                f"{definition.id}@{definition.version} is defined in multiple files",
            ))
    if errors:
        raise LintError(errors, exit_code=exit_code)
    return definitions


def _registry(config: Path) -> StaticToolRegistry:
    contracts = [contract for _, contract in _definitions(_files(config, "TOOL"), config, ToolContract, "TOOL")]
    try:
        workers = _worker_paths(contracts)
    except ValueError as error:
        raise LintError([
            _issue(config / "tools", config, "INVALID_PYTHON_WORKER", "implementation", str(error)),
        ]) from error
    registry = StaticToolRegistry(python_implementations=workers)
    for contract in contracts:
        registry.register(contract)
    return registry


def _method_issues(path: Path, config: Path, method: MethodDefinition, registry: StaticToolRegistry) -> list[dict]:
    errors = [
        _issue(path, config, finding.code, finding.field, finding.message)
        for finding in validate_method(method, registry=registry, registered_rules=METHOD_RULES, source=path)
    ]
    # validate_method already checks dependencies for every expanded variant.
    if method.step_variants is None:
        errors.extend(
            _issue(path, config, finding.code, finding.field, finding.message)
            for finding in validate_dependencies(method.inputs, method.steps)
            if finding.code != "DUPLICATE_STEP_KEY"
        )
    errors.extend(
        _issue(path, config, "UNKNOWN_PRECONDITION", f"preconditions.{index}", f"Unregistered precondition {name!r}")
        for index, name in enumerate(method.preconditions)
        if name not in METHOD_CHECKS
    )
    return errors


def lint_methods(config: Path, method: Path | None = None) -> dict:
    """Validate an exact file or the catalog with no runtime GIS discovery."""
    config = config.absolute()
    registry = _registry(config)
    paths = _files(config, "METHOD") if method is None else [method.absolute()]
    definitions = _definitions(paths, config, MethodDefinition, "METHOD")
    methods = []
    examples = []
    errors = []
    for path, definition in definitions:
        issues = _method_issues(path, config, definition, registry)
        entry = {
            "id": definition.id, "version": definition.version,
            "path": _path_label(path, config), "locked_order": definition.locked_order,
        }
        if not issues:
            methods.append(entry)
        elif (
            method is None
            and entry["path"] == EXAMPLE_PATH
            and (definition.id, definition.version) == ("road-buffer-statistics", 1)
            and len(issues) == len(ROAD_V1_EXAMPLE)
            and {(issue["code"], issue["field"]) for issue in issues} == ROAD_V1_EXAMPLE
        ):
            examples.append({**entry, "disposition": "catalog_example_only", "diagnostics": issues})
        else:
            errors.extend(issues)
    if errors:
        raise LintError(errors)
    return {
        "scope": "method_static_lint",
        "selection": "catalog" if method is None else "single_method",
        "tools_checked": len(registry.list()),
        "methods": methods,
        "catalog_examples": examples,
    }
