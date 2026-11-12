"""Audit local configuration references and exported JSON schemas without running GIS."""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import TypeAdapter, ValidationError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from geopilot.api.server import _qgis_bindings, _worker_paths, _yaml_files
from geopilot.application.land_use_checks import LAND_USE_CHECKS
from geopilot.application.method_preconditions import METHOD_PRECONDITIONS
from geopilot.application.remote_sensing_checks import REMOTE_SENSING_CHECKS
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.checks.raster_checks import DEM_ARTIFACT_RULES
from geopilot.checks.tool_checks import tool_check_version
from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import ExecutionStatus, StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.qgis_runner import _validate_binding
from geopilot.execution.step_support import AdapterError
from geopilot.llm.clarification import ClarificationReport
from geopilot.llm.response_parser import ModelSuggestion
from geopilot.methods.loader import _UniqueKeySafeLoader, load_method_catalog
from geopilot.methods.models import MethodDefinition, MethodRef
from geopilot.methods.validator import validate_method
from geopilot.plans.dependency_validator import validate_dependencies
from geopilot.plans.models import AnalysisPlan
from geopilot.plans.service import _BUILTIN_PRECONDITIONS
from geopilot.remote_sensing.config_models import SensorProductConfig
from geopilot.remote_sensing.config_registry import load_sensor_registry
from geopilot.runs.models import Run, RunStep
from geopilot.tools.registry import ToolRegistry


SCHEMAS = {
    "analysis-plan": AnalysisPlan,
    "clarification-report": ClarificationReport,
    "error-detail": ErrorDetail,
    "execution-status": ExecutionStatus,
    "method-definition": MethodDefinition,
    "method-ref": MethodRef,
    "p9-model-response": ModelSuggestion,
    "run": Run,
    "run-step": RunStep,
    "sensor-product-config": SensorProductConfig,
    "step-request": StepRequest,
    "step-result": StepResult,
}
DRAFT = "https://json-schema.org/draft/2020-12/schema"
CONFIG_ERRORS = (OSError, ValueError, RecursionError, yaml.YAMLError)
METHOD_RULES = {*RUN_CHECKS, *LAND_USE_CHECKS, *REMOTE_SENSING_CHECKS, *DEM_ARTIFACT_RULES}
# The API adds the sensor-aware precondition when building its plan routes.
METHOD_CHECKS = {*_BUILTIN_PRECONDITIONS, *METHOD_PRECONDITIONS, "remote_sensing_inputs_are_consistent"}
ROAD_V1_EXAMPLE = {
    *(("MISSING_TOOL_BINDING", f"steps.{i}.tool") for i in range(7)),
    *(("MISSING_ARTIFACT_BINDING", f"expected_artifacts.{i}") for i in range(3)),
}


@dataclass(frozen=True)
class Finding:
    path: str
    code: str
    field: str
    message: str
    subject: str | None = None


class StaticToolRegistry(ToolRegistry):
    """Reuse version registration without probing any QGIS executable."""

    def _unavailable_reason(self, contract, required_modules):
        if contract.runner == "qgis":
            return None
        return super()._unavailable_reason(contract, required_modules)


def read_yaml(path: Path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)


def read_json(path: Path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key {key!r}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)


def pointer(parts) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts) or "/"


def schema_differences(expected, actual, parts=()):
    if type(expected) is not type(actual):
        yield pointer(parts), f"Expected {type(expected).__name__}, found {type(actual).__name__}"
    elif isinstance(expected, dict):
        for key in sorted(expected.keys() | actual.keys()):
            if key not in actual:
                yield pointer((*parts, key)), "Schema keyword is missing"
            elif key not in expected:
                yield pointer((*parts, key)), "Unexpected schema keyword"
            else:
                yield from schema_differences(expected[key], actual[key], (*parts, key))
    elif isinstance(expected, list) and len(expected) == len(actual):
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            yield from schema_differences(left, right, (*parts, index))
    elif expected != actual:
        yield pointer(parts), f"Expected {expected!r}, found {actual!r}"


def check_schemas(root: Path, findings: list[Finding], draft_validator):
    directory = root / "artifacts/schemas"
    documents = {}
    matched = 0
    for name, model in SCHEMAS.items():
        path = directory / f"{name}.schema.json"
        label = path.relative_to(root).as_posix()
        try:
            actual = read_json(path)
        except (OSError, ValueError, RecursionError) as error:
            findings.append(Finding(label, "SCHEMA_READ_ERROR", "/", str(error)))
            continue
        if not isinstance(actual, dict):
            findings.append(Finding(label, "INVALID_SCHEMA", "/", "Expected a JSON Schema object"))
            continue
        expected = TypeAdapter(model).json_schema()
        if "$schema" in actual:
            expected["$schema"] = DRAFT
        differences = list(schema_differences(expected, actual))
        findings.extend(Finding(label, "SCHEMA_DRIFT", field, message) for field, message in differences)
        matched += not differences
        if draft_validator is not None:
            from jsonschema.exceptions import SchemaError

            try:
                draft_validator.check_schema(actual)
            except SchemaError as error:
                findings.append(Finding(label, "INVALID_DRAFT_SCHEMA", pointer(error.path), error.message))
                continue
        if not differences:
            documents[name] = actual
    for path in sorted(directory.glob("*.schema.json")):
        if path.name.removesuffix(".schema.json") not in SCHEMAS:
            findings.append(Finding(
                path.relative_to(root).as_posix(), "UNMAPPED_SCHEMA", "/", "Declare its owning Pydantic model",
            ))
    return documents, matched


def check_tools(root: Path, findings: list[Finding]) -> tuple[StaticToolRegistry, dict]:
    tools = root / "config/tools"
    contracts = []
    try:
        paths = _yaml_files(tools)
        if not paths:
            raise ValueError("Expected at least one tool contract")
    except (OSError, ValueError) as error:
        findings.append(Finding("config/tools", "TOOL_DIRECTORY_ERROR", "$", str(error)))
        paths = []
    for path in paths:
        label = path.relative_to(root).as_posix()
        try:
            contracts.append((label, ToolContract.model_validate(read_yaml(path))))
        except ValidationError as error:
            findings.extend(
                Finding(label, "INVALID_TOOL_CONFIG", ".".join(map(str, detail["loc"])) or "$", detail["msg"])
                for detail in error.errors(include_url=False, include_input=False, include_context=False)
            )
        except CONFIG_ERRORS as error:
            findings.append(Finding(label, "INVALID_TOOL_YAML", "$", str(error)))

    try:
        workers = _worker_paths([contract for _, contract in contracts])
    except ValueError as error:
        findings.append(Finding("config/tools", "INVALID_PYTHON_WORKER", "implementation", str(error)))
        workers = {}
    try:
        bindings = _qgis_bindings(root / "config/runner_bindings")
    except CONFIG_ERRORS as error:
        findings.append(Finding("config/runner_bindings", "INVALID_QGIS_BINDINGS", "$", str(error)))
        bindings = {}

    registry = StaticToolRegistry(python_implementations=workers)
    used_bindings = set()
    for label, contract in contracts:
        subject = f"{contract.id}@{contract.version}"
        try:
            registry.register(contract)
        except ValueError as error:
            findings.append(Finding(label, "DUPLICATE_TOOL", "version", str(error), subject))
            continue
        registration = registry.get(contract.id, contract.version)
        if not registration.available:
            findings.append(Finding(label, "INVALID_PYTHON_WORKER", "implementation", registration.reason, subject))
        for index, rule in enumerate(contract.check_rules):
            if tool_check_version(rule) is None:
                findings.append(Finding(
                    label, "UNKNOWN_TOOL_CHECK_RULE", f"check_rules.{index}", f"Unregistered rule {rule!r}", subject,
                ))
        if contract.runner == "qgis":
            try:
                _, field = _validate_binding(contract, bindings)
                used_bindings.add(field.removeprefix("bindings."))
            except AdapterError as error:
                findings.append(Finding("config/runner_bindings", error.code, error.field, error.message, subject))
    for key in sorted(bindings.keys() - used_bindings, key=str):
        findings.append(Finding("config/runner_bindings", "UNUSED_QGIS_BINDING", str(key), "No matching tool binding"))
    return registry, {"tools": len(registry.list()), "qgis_bindings": len(bindings)}


def check_methods(root: Path, registry: ToolRegistry, findings: list[Finding], exceptions: list[Finding]) -> int:
    directory = root / "config/methods"
    label = directory.relative_to(root).as_posix()
    try:
        catalog = load_method_catalog(directory)
    except OSError as error:
        findings.append(Finding(label, "METHOD_DIRECTORY_ERROR", "$", str(error)))
        return 0
    findings.extend(
        Finding(issue.path.relative_to(root).as_posix(), "INVALID_METHOD_CONFIG", issue.field, issue.message)
        for issue in catalog.issues
    )
    methods = catalog.list()
    if not methods and not catalog.issues:
        findings.append(Finding(label, "METHOD_DIRECTORY_ERROR", "$", "Expected at least one method"))
    for method in methods:
        subject = f"{method.id}@{method.version}"
        validation = validate_method(method, registry=registry, registered_rules=METHOD_RULES)
        example = subject == "road-buffer-statistics@1" and {
            (issue.code, issue.field) for issue in validation
        } == ROAD_V1_EXAMPLE
        destination = exceptions if example else findings
        destination.extend(Finding(label, issue.code, issue.field, issue.message, subject) for issue in validation)
        if method.step_variants is None:
            findings.extend(
                Finding(label, issue.code, issue.field, issue.message, subject)
                for issue in validate_dependencies(method.inputs, method.steps)
                if issue.code != "DUPLICATE_STEP_KEY"
            )
        for index, name in enumerate(method.preconditions):
            if name not in METHOD_CHECKS:
                findings.append(Finding(
                    label, "UNKNOWN_PRECONDITION", f"preconditions.{index}", f"Unregistered precondition {name!r}", subject,
                ))
    return len(methods)


def check_sensors(root: Path, findings: list[Finding]) -> int:
    directory = root / "config/sensors"
    try:
        registry = load_sensor_registry(directory)
        files = _yaml_files(directory)
        if not files:
            raise ValueError("Expected at least one sensor product")
    except CONFIG_ERRORS as error:
        path = getattr(error, "path", directory)
        findings.append(Finding(path.relative_to(root).as_posix(), "INVALID_SENSOR_CONFIG", "$", str(error)))
        return 0
    for path in files:
        try:
            config = SensorProductConfig.model_validate(read_yaml(path))
            registry.lookup(config.key)
        except (LookupError, *CONFIG_ERRORS) as error:
            findings.append(Finding(path.relative_to(root).as_posix(), "INVALID_SENSOR_LOOKUP", "$", str(error)))
    return len(files)


def check_draft_configs(root: Path, documents: dict, validator, findings: list[Finding]) -> None:
    for family, schema in (
        ("methods", documents.get("method-definition")),
        ("tools", ToolContract.model_json_schema()),
        ("sensors", documents.get("sensor-product-config")),
    ):
        if schema is None or not (root / "config" / family).is_dir():
            continue
        checker = validator(schema)
        for path in _yaml_files(root / "config" / family):
            label = path.relative_to(root).as_posix()
            try:
                instance = json.loads(json.dumps(read_yaml(path), default=date.isoformat, allow_nan=False))
                findings.extend(
                    Finding(label, "CONFIG_SCHEMA_MISMATCH", pointer(error.path), error.message)
                    for error in checker.iter_errors(instance)
                )
            except (TypeError, *CONFIG_ERRORS) as error:
                findings.append(Finding(label, "INVALID_JSON_INSTANCE", "/", str(error)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="prototype directory containing config and schemas")
    parser.add_argument("--draft-check", action="store_true", help="also validate with installed jsonschema")
    args = parser.parse_args()
    validator = None
    if args.draft_check:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            parser.error("--draft-check requires jsonschema")
        validator = Draft202012Validator

    root = args.root.resolve()
    findings = []
    exceptions = []
    schemas, matched = check_schemas(root, findings, validator)
    registry, counts = check_tools(root, findings)
    counts.update(
        methods=check_methods(root, registry, findings, exceptions),
        sensors=check_sensors(root, findings),
        schemas_expected=len(SCHEMAS),
        schemas_matching=matched,
    )
    if validator is not None:
        check_draft_configs(root, schemas, validator, findings)
    print(json.dumps({
        "status": "FAIL" if findings else "PASS",
        "counts": counts,
        "qgis_executable_probed": False,
        "draft_2020_12_validation": args.draft_check,
        "known_catalog_example_findings": [asdict(item) for item in exceptions],
        "findings": [asdict(item) for item in findings],
    }, ensure_ascii=False, indent=2))
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
