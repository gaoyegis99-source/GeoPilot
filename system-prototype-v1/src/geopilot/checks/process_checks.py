"""Compare actual run records and process evidence with a confirmed plan."""

import json
import math
import shlex
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from geopilot.checks.cache_reuse import reuse_output_sha256, verify_reuse_source
from geopilot.domain.step_io import ExecutionStatus, StepFileRef, StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.python_runner import _unique_json_object
from geopilot.execution.qgis_runner import _VERSION_FIELDS, _build_parameters, _validate_binding, _verify_report
from geopilot.execution.step_support import AdapterError, PreparedStep, resolve_step_path
from geopilot.persistence.plan_repository import plan_from_row, row_for_plan
from geopilot.persistence.run_repository import get_run, list_run_steps
from geopilot.persistence.schema import open_database
from geopilot.plans.compiler import PlanCompilationError, _same_json, compile_step_request
from geopilot.plans.models import AnalysisPlan, PlanStatus, PlanStep
from geopilot.runs.models import RunStep
from geopilot.tools.registry import ToolRegistry


class ProcessCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    rule: str
    phase: Literal["process"] = "process"
    step_key: str | None = None
    field: str
    severity: Literal["blocking", "warning"] = "blocking"
    passed: bool
    observed: JsonValue
    expected: JsonValue
    message: str


class _RunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    run_id: str
    plan: AnalysisPlan
    asset_files: dict[str, StepFileRef]


@dataclass(frozen=True)
class _ProcessEvidence:
    argv: list[str]
    stdout: str
    exit_code: int


_Model = TypeVar("_Model", bound=BaseModel)
_REQUEST = "step_request_matches"
_RESULT = "step_result_matches"
_SNAPSHOT = "run_snapshot_matches"
_ORDER = "step_order_matches"
_PROCESS = "step_process_matches"
_ENVIRONMENT = "step_environment_matches"
_REUSE = "step_cache_reuse_matches"
_ABSENT = {"present": False}


def _finding(
    rule: str, step: str | None, field: str, observed: JsonValue, expected: JsonValue, message: str, *, passed: bool = False,
) -> ProcessCheckResult:
    return ProcessCheckResult(
        rule=rule, step_key=step, field=field, observed=observed, expected=expected, message=message, passed=passed,
    )


def _differences(
    observed: JsonValue, expected: JsonValue, field: str, *, engine_numbers: bool,
) -> Iterator[tuple[str, JsonValue, JsonValue]]:
    if isinstance(observed, dict) and isinstance(expected, dict):
        for key in sorted(observed.keys() | expected.keys()):
            location = f"{field}.{key}"
            if key not in observed or key not in expected:
                yield location, observed.get(key, _ABSENT), expected.get(key, _ABSENT)
            else:
                yield from _differences(observed[key], expected[key], location, engine_numbers=engine_numbers)
    elif isinstance(observed, list) and isinstance(expected, list) and len(observed) == len(expected):
        for index, (actual, required) in enumerate(zip(observed, expected, strict=True)):
            yield from _differences(actual, required, f"{field}.{index}", engine_numbers=engine_numbers)
    elif engine_numbers and type(observed) in (int, float) and type(expected) in (int, float):
        if observed != expected:
            yield field, observed, expected
    elif not _same_json(observed, expected):
        yield field, observed, expected


def _compare(
    checks: list[ProcessCheckResult], rule: str, step: str | None, field: str,
    observed: JsonValue, expected: JsonValue, *, engine_numbers: bool = False,
) -> None:
    differences = list(_differences(observed, expected, field, engine_numbers=engine_numbers))
    if differences:
        checks.extend(_finding(rule, step, location, actual, required, "Recorded evidence differs")
                      for location, actual, required in differences)
    else:
        checks.append(_finding(rule, step, field, observed, expected, "Recorded evidence matches", passed=True))


def _finite_number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Process evidence must contain finite JSON numbers")
    return value


def _json(text: str) -> JsonValue:
    return json.loads(text, object_pairs_hook=_unique_json_object, parse_float=_finite_number, parse_constant=_finite_number)


def _read_text(workdir: Path, relative: str, field: str) -> str:
    path = resolve_step_path(workdir, relative, field=field)
    if not path.is_file():
        raise ValueError(f"Expected a local evidence file at {relative}")
    return path.read_text(encoding="utf-8")


def _read_model(
    model: type[_Model], workdir: Path, relative: str, checks: list[ProcessCheckResult],
    rule: str, step: str | None, field: str,
) -> _Model | None:
    try:
        return model.model_validate(_json(_read_text(workdir, relative, field)))
    except ValidationError as error:
        detail = error.errors(include_url=False, include_input=False, include_context=False)[0]
        location = ".".join(map(str, detail["loc"]))
        field = f"{field}.{location}" if location else field
        checks.append(_finding(rule, step, field, relative, model.__name__, detail["msg"]))
    except (OSError, ValueError) as error:
        checks.append(_finding(rule, step, field, relative, model.__name__, str(error)))
    return None


def _read_process(log: str, runner: str) -> _ProcessEvidence:
    lines = log.splitlines()
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("$ ") or lines[index + 1] != "stdout:":
            continue
        argv = shlex.split(line[2:])
        if runner == "qgis" and argv[1:3] != ["--json", "run"]:
            continue
        stdout, separator, remaining = "\n".join(lines[index + 2:]).partition("\nstderr:\n")
        _, exit_marker, exit_text = remaining.rpartition("\nexit_code: ")
        if not separator or not exit_marker:
            raise ValueError("The calculation log lacks a complete process record")
        return _ProcessEvidence(argv, stdout, int(exit_text.split("\n", 1)[0]))
    raise ValueError("The calculation command is missing from the process log")


def _relative_argument(argument: str, workdir: Path) -> str:
    path = Path(argument)
    return path.relative_to(workdir).as_posix() if path.is_absolute() and path.is_relative_to(workdir) else path.as_posix()


def _python_evidence(
    checks: list[ProcessCheckResult], evidence: _ProcessEvidence, contract: ToolContract,
    request: StepRequest, result: StepResult, workdir: Path, log_ref: PurePosixPath,
    implementations: Mapping[str, str | Path] | None,
) -> None:
    key = request.step_key
    request_file = log_ref.with_name("request.json").as_posix()
    manifest_file = log_ref.with_name("python-result.json").as_posix()
    argv = evidence.argv
    observed_args = argv[2:]
    if len(argv) == 6:
        observed_args = [argv[2], _relative_argument(argv[3], workdir), argv[4], _relative_argument(argv[5], workdir)]
    _compare(checks, _PROCESS, key, "log.arguments", observed_args, ["--request", request_file, "--result", manifest_file])
    if implementations is not None:
        configured = implementations.get(contract.implementation)
        expected_script = str(Path(configured).resolve()) if configured is not None else contract.implementation
        script = argv[1] if len(argv) > 1 else None
        _compare(checks, _PROCESS, key, "log.implementation", script, expected_script)

    try:
        manifest = _json(_read_text(workdir, manifest_file, "python_manifest"))
        if not isinstance(manifest, dict) or set(manifest) - {"outputs", "environment"}:
            raise ValueError("The Python child manifest has an invalid shape")
    except (OSError, ValueError) as error:
        checks.append(_finding(_PROCESS, key, "python_manifest", manifest_file, "Readable child manifest", str(error)))
        return
    outputs = manifest.get("outputs")
    if isinstance(outputs, dict):
        outputs = {
            role: {"path": ref, "layer": None} if isinstance(ref, str) else {"layer": None, **ref} if isinstance(ref, dict) else ref
            for role, ref in outputs.items()
        }
    _compare(checks, _PROCESS, key, "python_manifest.outputs", outputs,
             {role: ref.model_dump(mode="json") for role, ref in request.output_targets.items()})

    environment = manifest.get("environment", {})
    if not isinstance(environment, dict) or any(not isinstance(value, str) or not value.strip() for value in environment.values()):
        checks.append(_finding(_ENVIRONMENT, key, "environment", environment, result.environment,
                               "The Python child manifest contains invalid environment versions"))
        return
    expected_environment = result.environment.copy()
    if "python" not in expected_environment:
        checks.append(_finding(_ENVIRONMENT, key, "environment.python", None, "Recorded interpreter version", "Interpreter version is missing"))
    if "python" not in environment:
        expected_environment.pop("python", None)
    _compare(checks, _ENVIRONMENT, key, "environment", environment, expected_environment)


def _named_qgis_arguments(arguments: Mapping, binding: Mapping) -> dict:
    names = {
        alias: ("output_targets" if group == "outputs" else group, role)
        for group in ("inputs", "parameters", "outputs") for role, alias in binding[group].items()
    }
    named: dict[str, dict] = {}
    for alias, value in arguments.items():
        group, port = names.get(alias, ("unbound_arguments", alias))
        named.setdefault(group, {})[port] = value
    return named


def _qgis_evidence(
    checks: list[ProcessCheckResult], evidence: _ProcessEvidence, contract: ToolContract,
    request: StepRequest, result: StepResult, workdir: Path, log_ref: PurePosixPath, bindings: Mapping | None,
) -> None:
    key = request.step_key
    _compare(checks, _PROCESS, key, "log.arguments", evidence.argv[1:], ["--json", "run", contract.implementation, "-"])
    report = _json(evidence.stdout)
    if not isinstance(report, dict) or not isinstance(report.get("algorithm_details"), dict):
        raise ValueError("The QGIS calculation did not report an algorithm")
    _compare(checks, _PROCESS, key, "engine.algorithm", report["algorithm_details"].get("id"), contract.implementation)
    environment = {key: report[key] for key in _VERSION_FIELDS if isinstance(report.get(key), str) and report[key]}
    if "qgis_version" not in environment:
        checks.append(_finding(_ENVIRONMENT, key, "environment.qgis_version", None, "Recorded QGIS version", "QGIS version is missing"))
    _compare(checks, _ENVIRONMENT, key, "environment", environment, result.environment)

    if bindings is None:
        return
    observed = report.get("inputs")
    if not isinstance(observed, dict):
        raise ValueError("The QGIS calculation did not report its actual arguments")
    binding, _ = _validate_binding(contract, bindings)
    log_path = workdir / log_ref
    prepared = PreparedStep(request.model_dump(mode="json"), contract, workdir, log_path,
                            log_path.with_name("request.json"), log_path.with_name("result.json"))
    expected = _build_parameters(prepared, binding)
    _compare(checks, _PROCESS, key, "engine", _named_qgis_arguments(observed, binding), _named_qgis_arguments(expected, binding),
             engine_numbers=True)
    try:
        _verify_report(prepared, binding, evidence.stdout)
    except AdapterError as error:
        checks.append(_finding(_PROCESS, key, f"engine.{error.field or 'results'}", report.get("results"),
                               "Declared QGIS output references", error.message))


def _check_process_log(
    checks: list[ProcessCheckResult], workdir: Path, request: StepRequest, result: StepResult,
    contract: ToolContract, qgis_bindings: Mapping | None, python_implementations: Mapping[str, str | Path] | None,
) -> None:
    key = request.step_key
    log_ref = PurePosixPath(request.log_path)
    try:
        log = _read_text(workdir, request.log_path, "log_path")
    except (OSError, ValueError) as error:
        checks.append(_finding(_PROCESS, key, "log_path", request.log_path, "Readable local process log", str(error)))
        return
    try:
        header = f"{contract.id} version {contract.version} ({contract.runner})"
        _compare(checks, _PROCESS, key, "log.header", log.partition("\n")[0], header)
        evidence = _read_process(log, contract.runner)
        _compare(checks, _PROCESS, key, "log.exit_code", evidence.exit_code, result.exit_code)
        if contract.runner == "python":
            _python_evidence(checks, evidence, contract, request, result, workdir, log_ref, python_implementations)
        else:
            _qgis_evidence(checks, evidence, contract, request, result, workdir, log_ref, qgis_bindings)
    except (OSError, ValueError) as error:
        field = error.field if isinstance(error, AdapterError) and error.field is not None else "process_evidence"
        checks.append(_finding(_PROCESS, key, field, request.log_path, "Readable calculation evidence", str(error)))


def _check_cache_reuse(
    checks: list[ProcessCheckResult], database_path: str | Path, workdir: Path,
    request: StepRequest, result: StepResult,
) -> None:
    reuse = result.reuse
    assert reuse is not None
    key = request.step_key
    marker = f"cache reuse: {reuse.cache_key} from {reuse.source_run_id}/{reuse.source_step_key}"
    try:
        log = _read_text(workdir, request.log_path, "log_path")
    except (OSError, ValueError) as error:
        checks.append(_finding(_REUSE, key, "log_path", request.log_path, "Readable reuse log", str(error)))
    else:
        markers = [line for line in log.splitlines() if line.startswith("cache reuse:")]
        _compare(checks, _REUSE, key, "log.reuse_marker", markers, [marker])

    output, = result.outputs.values()
    try:
        digest = reuse_output_sha256(workdir, output.path)
    except (OSError, ValueError) as error:
        checks.append(_finding(_REUSE, key, "reuse.output_sha256", None, reuse.output_sha256, str(error)))
    else:
        _compare(checks, _REUSE, key, "reuse.output_sha256", digest, reuse.output_sha256)
    _compare(checks, _REUSE, key, "reuse.source", verify_reuse_source(database_path, reuse, request), True)


def _check_result_references(checks: list[ProcessCheckResult], step: PlanStep, record: RunStep, result: StepResult) -> None:
    observed = {role: ref.model_dump(mode="json") for role, ref in result.outputs.items()}
    expected = {
        role: {"path": ref.target, "type": ref.type,
               "layer": ref.layer if ref.layer is not None else observed.get(role, {}).get("layer")}
        for role, ref in step.outputs.items()
    }
    _compare(checks, _RESULT, step.key, "result.outputs", observed, expected)
    _compare(checks, _RESULT, step.key, "log_path", record.log_path, result.log_path)
    timestamps = record.model_dump(mode="json", include={"started_at", "finished_at"})
    _compare(checks, _RESULT, step.key, "result.timestamps", timestamps,
             result.model_dump(mode="json", include={"started_at", "finished_at"}))


def check_run_process(
    database_path: str | Path, run_id: str, *, registry: ToolRegistry, qgis_bindings: Mapping | None = None,
    python_implementations: Mapping[str, str | Path] | None = None,
) -> tuple[ProcessCheckResult, ...]:
    """Read the exact run evidence without modifying its lifecycle or files."""
    with open_database(database_path, read_only=True) as connection:
        connection.execute("BEGIN")
        run = get_run(connection, run_id)
        if run is None:
            raise LookupError(f"Run {run_id!r} does not exist")
        row = row_for_plan(connection, run.plan_id, run.plan_version)
        if row is None:
            raise LookupError(f"The exact plan for run {run_id!r} is missing")
        plan = plan_from_row(row)
        records = {record.step_key: record for record in list_run_steps(connection, run_id)}

    checks: list[ProcessCheckResult] = []
    workdir = Path(run.workdir)
    if not workdir.is_absolute() or workdir.resolve() != workdir or not workdir.is_dir():
        return (_finding(_SNAPSHOT, None, "workdir", run.workdir, "Canonical run directory", "Run directory is invalid"),)
    _compare(checks, _SNAPSHOT, None, "plan.status", plan.status.value, PlanStatus.CONFIRMED.value)
    snapshot = _read_model(_RunSnapshot, workdir, "request.json", checks, _SNAPSHOT, None, "snapshot")
    if snapshot is not None:
        _compare(checks, _SNAPSHOT, None, "snapshot.run_id", snapshot.run_id, run.id)
        _compare(checks, _SNAPSHOT, None, "snapshot.plan", snapshot.plan.model_dump(mode="json"), plan.model_dump(mode="json"))
        _compare(checks, _SNAPSHOT, None, "snapshot.asset_roles", sorted(snapshot.asset_files), sorted(plan.inputs))
        for role, reference in snapshot.asset_files.items():
            if PurePosixPath(reference.path).parts[0] != "inputs":
                checks.append(_finding(_SNAPSHOT, None, f"snapshot.asset_files.{role}.path", reference.path,
                                       "Run-relative staged input", "Asset reference is outside the input directory"))

    prior_results: dict[str, StepResult] = {}
    previous: StepResult | None = None
    for position, step in enumerate(plan.steps):
        key = step.key
        record = records.pop(key, None)
        _compare(checks, _ORDER, key, "position", record.position if record else None, position)
        if record is None:
            continue
        _compare(checks, _RESULT, key, "status", record.status.value, ExecutionStatus.SUCCEEDED.value)
        request = record.request
        result = record.result
        if request is None or result is None:
            checks.append(_finding(_PROCESS, key, "step_record", None, "Persisted request and result", "Step evidence is incomplete"))
            continue

        expected = None
        if snapshot is not None:
            try:
                expected = compile_step_request(plan, key, run_id=run.id, asset_files=snapshot.asset_files, prior_results=prior_results)
            except PlanCompilationError as error:
                checks.append(_finding(_REQUEST, key, error.field, None, "Compilable confirmed request", error.message))
        if expected is not None:
            _compare(checks, _REQUEST, key, "request", request.model_dump(mode="json"), expected.model_dump(mode="json"))
        reference = expected or request
        log_ref = PurePosixPath(reference.log_path)
        disk_request = _read_model(StepRequest, workdir, log_ref.with_name("request.json").as_posix(),
                                   checks, _REQUEST, key, "request_file")
        if disk_request is not None:
            _compare(checks, _REQUEST, key, "request_file", disk_request.model_dump(mode="json"), reference.model_dump(mode="json"))
        disk_result = _read_model(StepResult, workdir, log_ref.with_name("result.json").as_posix(),
                                  checks, _RESULT, key, "result_file")
        if disk_result is not None:
            _compare(checks, _RESULT, key, "result_file", disk_result.model_dump(mode="json"), result.model_dump(mode="json"))
        _check_result_references(checks, step, record, result)
        if previous is not None:
            ordered = previous.finished_at <= result.started_at
            checks.append(_finding(_ORDER, key, "started_at", result.started_at.isoformat(), previous.finished_at.isoformat(),
                                   "Step starts after the preceding result" if ordered else "Step overlaps the preceding result",
                                   passed=ordered))
        prior_results[key] = result
        previous = result

        if step.tool is not None:
            try:
                contract = registry.get(step.tool.id, step.tool.version).contract
            except KeyError:
                rule = _REUSE if result.reuse is not None else _PROCESS
                checks.append(_finding(rule, key, "tool", None, step.tool.model_dump(mode="json"),
                                       "The confirmed tool version is missing from the registry"))
            else:
                if result.reuse is not None:
                    _check_cache_reuse(checks, database_path, workdir, reference, result)
                else:
                    _check_process_log(checks, workdir, reference, result, contract, qgis_bindings, python_implementations)

    for key, record in records.items():
        checks.append(_finding(_ORDER, key, "position", record.position, None, "Run step is absent from the confirmed plan"))
    return tuple(checks)
