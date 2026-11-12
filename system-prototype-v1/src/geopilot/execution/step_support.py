"""Shared local paths, process capture and persisted results for step adapters."""

import json
import math
import shlex
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from geopilot.data import csv_output
from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.data.vector_reader import read_vector_metadata
from geopilot.domain.data_asset import TableMetadata, VectorMetadata
from geopilot.domain.tool_contract import ToolContract, ToolRef
from geopilot.execution.process_manager import capture_process
from geopilot.observability.telemetry import StepTelemetry, start_step
from geopilot.tools.parameter_normalizer import ParameterNormalizationError, normalize_parameters
from geopilot.tools.registry import ToolRegistry


_REQUEST_KEYS = {
    "run_id", "step_key", "tool", "workdir_ref", "inputs", "parameters",
    "output_targets", "log_path", "cancel_path",
}
OUTPUT_STAGING_RECEIPT = ".python-output-staging.json"


class AdapterError(ValueError):
    def __init__(
        self, message: str, *, code: str, field: str | None = None,
        exit_code: int | None = None, phase: str = "execute",
        observed: object = None, suggestion: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field
        self.exit_code = exit_code
        self.phase = phase
        self.observed = observed
        self.suggestion = suggestion


@dataclass(frozen=True)
class PreparedStep:
    request: dict
    contract: ToolContract
    workdir: Path
    log_path: Path
    request_path: Path
    result_path: Path
    cancel_requested: Callable[[], bool] | None = None
    telemetry: StepTelemetry | None = None


def _invalid(message: str, field: str) -> AdapterError:
    return AdapterError(message, code="INVALID_STEP_REQUEST", phase="preflight", field=field)


def resolve_step_path(workdir: Path, relative: str, *, field: str) -> Path:
    """Resolve a generated relative path without following workspace symlinks."""
    if not isinstance(relative, str) or any(char in relative for char in "\\:\x00|"):
        raise _invalid("Expected a local relative path", field)
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _invalid("Path must stay within the run directory", field)
    path = workdir
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise _invalid("Symbolic links are not allowed inside the run directory", field)
    return path


def output_staging_receipt_path(workdir: Path, request: Mapping) -> Path:
    """Derive the private receipt path from the validated, persisted step request."""
    log = workdir / request["log_path"]
    occupied = {log, workdir / request["cancel_path"]}
    for key in ("inputs", "output_targets"):
        occupied.update(workdir / ref["path"] for ref in request[key].values())
    receipt = log.with_name(OUTPUT_STAGING_RECEIPT)
    suffix = 1
    while receipt in occupied:
        receipt = log.with_name(f".python-output-staging-{suffix}.json")
        suffix += 1
    return receipt


def _new_file(path: Path, field: str) -> None:
    if not path.parent.is_dir() or path.exists():
        raise _invalid("Expected a new file inside an existing run directory", field)


def _file_references(request: Mapping, key: str, ports: Mapping, root: Path) -> tuple[dict, set[Path]]:
    values = request[key]
    if not isinstance(values, Mapping) or set(values) != set(ports):
        raise _invalid("File roles must match the tool contract", key)

    references = {}
    paths = set()
    for name, ref in values.items():
        field = f"{key}.{name}"
        if not isinstance(ref, Mapping) or "path" not in ref or set(ref) - {"path", "layer"}:
            raise _invalid("Expected a file reference with path and optional layer", field)
        layer = ref.get("layer")
        if layer is not None and (not isinstance(layer, str) or not layer.strip() or "\x00" in layer):
            raise _invalid("Expected a nonempty layer name", f"{field}.layer")
        path = resolve_step_path(root, ref["path"], field=f"{field}.path")
        if key == "inputs":
            if not path.is_file():
                raise AdapterError("Input file is missing", code="SOURCE_UNREADABLE", phase="preflight", field=f"{field}.path")
        else:
            _new_file(path, f"{field}.path")
            if path in paths:
                raise _invalid("Each output role requires a different file", f"{field}.path")
        references[name] = dict(ref)
        paths.add(path)
    return references, paths


def prepare_step(
    request: Mapping, *, registry: ToolRegistry, workdir: Path, runner: str | None,
    cancel_requested: Callable[[], bool] | None = None,
) -> PreparedStep:
    if not isinstance(request, Mapping) or set(request) != _REQUEST_KEYS:
        raise _invalid("Missing or unexpected step request fields", "request")
    for key in ("run_id", "step_key", "workdir_ref"):
        if not isinstance(request[key], str) or not request[key].strip():
            raise _invalid("Expected a nonempty identifier", key)
    try:
        ref = ToolRef.model_validate(request["tool"])
    except ValueError as error:
        raise _invalid(str(error), "tool") from error
    try:
        registered = registry.get(ref.id, ref.version)
    except KeyError as error:
        raise AdapterError("Tool version is not registered", code="TOOL_UNAVAILABLE", phase="preflight", field="tool") from error
    if not registered.available or (runner is not None and registered.contract.runner != runner):
        reason = registered.reason or "Tool runner does not match the selected adapter"
        raise AdapterError(reason, code="TOOL_UNAVAILABLE", phase="preflight", field="tool")

    root = Path(workdir).resolve(strict=True)
    contract = registered.contract
    inputs, input_paths = _file_references(request, "inputs", contract.inputs, root)
    outputs, output_paths = _file_references(request, "output_targets", contract.outputs, root)
    log = resolve_step_path(root, request["log_path"], field="log_path")
    cancel = resolve_step_path(root, request["cancel_path"], field="cancel_path")
    request_file = log.with_name("request.json")
    result_file = log.with_name("result.json")
    records = {log, request_file, result_file, cancel, log.with_name("python-result.json")}
    if len(records) != 5 or records & (input_paths | output_paths):
        raise _invalid("Step record paths overlap with each other or a data file", "log_path")
    if contract.runner == "python" and outputs:
        receipt = output_staging_receipt_path(root, request)
        if receipt.exists() or receipt.is_symlink():
            raise _invalid("The Python staging receipt location is already in use", "log_path")
    for path in (log, request_file, result_file):
        _new_file(path, "log_path")

    try:
        parameters = normalize_parameters(contract, request["parameters"])
    except ParameterNormalizationError as error:
        raise AdapterError(str(error), code=error.code, phase=error.phase, field=error.field) from error
    normalized = {**request, "tool": ref.model_dump(mode="json"), "inputs": inputs,
                  "output_targets": outputs, "parameters": parameters}
    return PreparedStep(normalized, contract, root, log, request_file, result_file, cancel_requested)


def run_process(
    step: PreparedStep, argv: Sequence[str], *, timeout: float,
    stdin: str | None = None, env: Mapping[str, str] | None = None,
    env_context: AbstractContextManager[Mapping[str, str]] | None = None,
    phase: str = "execute",
) -> str:
    """Capture a real child process and keep stdout/stderr in the step log."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise _invalid("Timeout must be positive and finite", "timeout")
    if step.cancel_requested is not None and step.cancel_requested():
        raise AdapterError("The step was cancelled before launching a process.", code="PROCESS_CANCELLED")
    with step.log_path.open("a", encoding="utf-8") as log:
        log.write(f"$ {shlex.join(argv)}\n")
        log.flush()
        probe = None if step.telemetry is None else step.telemetry.start_process(phase)
        try:
            outcome = capture_process(
                argv, cwd=step.workdir, env=env, env_context=env_context, stdin=stdin, timeout=timeout,
                cancel_requested=step.cancel_requested, probe=probe,
            )
        except OSError as error:
            log.write(f"Process could not start: {error}\n")
            raise AdapterError(str(error), code="PROCESS_START_FAILED", field="tool") from error
        finally:
            if probe is not None:
                probe.finish(None)

        log.write(f"stdout:\n{outcome.stdout}\nstderr:\n{outcome.stderr}\nexit_code: {outcome.exit_code}\n")
        if outcome.cancelled:
            raise AdapterError(
                "The process stopped after a user cancellation request.",
                code="PROCESS_CANCELLED", exit_code=outcome.exit_code,
            )
        if outcome.timed_out:
            raise AdapterError(
                f"Process exceeded {timeout:g} seconds", code="PROCESS_TIMEOUT", exit_code=outcome.exit_code,
            )
        if outcome.exit_code != 0:
            raise AdapterError(
                f"Process exited with code {outcome.exit_code}", code="PROCESS_FAILED", exit_code=outcome.exit_code,
            )
    return outcome.stdout


def matches_vector_type(metadata: VectorMetadata, expected: str) -> bool:
    if expected == "vector":
        return True
    kinds = expected.removeprefix("vector_").split("_or_")
    return any(kind in metadata.geometry_type.lower() for kind in kinds)


def _inspect_output(path: Path, expected: str, layer: str | None) -> str | None:
    if expected.startswith("raster"):
        read_raster_metadata(path)
    elif expected in {"table", "table_csv", "json"} and path.suffix.lower() == ".csv":
        if expected == "json":
            raise ValueError("Expected JSON output")
        with path.open(encoding="utf-8", newline="") as stream:
            try:
                header = next(csv_output.reader(stream), [])
            except csv_output.Error as error:
                raise ValueError(str(error)) from error
            if not header:
                raise ValueError("CSV output must have a header")
    elif expected in {"json", "table"} and path.suffix.lower() == ".json":
        with path.open(encoding="utf-8") as stream:
            json.load(stream)
    elif expected.startswith("vector") or expected == "table":
        if path.suffix.lower() == ".gpkg":
            layer, metadata = read_geopackage_metadata(path, layer)
        else:
            metadata = read_vector_metadata(path)
        if expected == "table" and isinstance(metadata, TableMetadata):
            return layer
        if not isinstance(metadata, VectorMetadata) or expected == "table":
            raise ValueError(f"Output is not of type {expected}")
        if not matches_vector_type(metadata, expected):
            raise ValueError(f"Expected {expected}, found {metadata.geometry_type}")
    else:
        raise ValueError(f"Unsupported output type or format: {expected}")
    return layer


def _verify_outputs(step: PreparedStep) -> dict:
    outputs = {}
    for role, ref in step.request["output_targets"].items():
        field = f"outputs.{role}"
        kind = step.contract.outputs[role]
        try:
            path = resolve_step_path(step.workdir, ref["path"], field=field)
            if not path.is_file():
                raise AdapterError("Expected output file is missing", code="OUTPUT_MISSING", field=field, exit_code=0)
            layer = _inspect_output(path, kind, ref.get("layer"))
        except AdapterError as error:
            if error.code == "OUTPUT_MISSING":
                raise
            raise AdapterError(str(error), code="OUTPUT_INVALID", field=field, exit_code=0) from error
        except (OSError, RuntimeError, ValueError) as error:
            raise AdapterError(str(error), code="OUTPUT_INVALID", field=field, exit_code=0) from error
        outputs[role] = {"path": ref["path"], "layer": layer, "type": kind}
    return outputs


def _write_json(path: Path, document: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _record_failure(result: dict, failure: AdapterError) -> None:
    try:
        observed = json.loads(json.dumps(failure.observed, allow_nan=False))
    except (TypeError, ValueError):
        observed = str(failure.observed)
    status = "CANCELLED" if failure.code == "PROCESS_CANCELLED" else "FAILED"
    result.update(status=status, exit_code=failure.exit_code, outputs={}, error={
        "code": failure.code,
        "phase": failure.phase,
        "step_key": result["step_key"],
        "field": failure.field,
        "message": failure.message,
        "suggestion": failure.suggestion or "Check the step inputs, local tool and process log.",
        "log_path": result["log_path"],
        "observed": observed,
    })


def execute_adapter(
    request: Mapping, *, registry: ToolRegistry, workdir: Path, runner: str | None,
    invoke: Callable[[PreparedStep], dict[str, str]],
    preflight: Callable[[PreparedStep], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict:
    """Validate and record one adapter call using the common step result shape."""
    identifiers = request if isinstance(request, Mapping) else {}
    telemetry = start_step(workdir, identifiers)
    result = {
        "run_id": identifiers.get("run_id"), "step_key": identifiers.get("step_key"),
        "status": "FAILED", "exit_code": None, "outputs": {}, "log_path": None,
        "warnings": [], "error": None, "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None, "environment": {},
    }
    step = None
    failure = None
    try:
        step = prepare_step(
            request, registry=registry, workdir=workdir, runner=runner, cancel_requested=cancel_requested,
        )
        if telemetry is not None:
            telemetry.set_tool(step.contract.id, step.contract.version, step.contract.runner)
            step = replace(step, telemetry=telemetry)
        _write_json(step.request_path, step.request)
        with step.log_path.open("x", encoding="utf-8") as log:
            log.write(f"{step.contract.id} version {step.contract.version} ({step.contract.runner})\n")
        result["log_path"] = step.request["log_path"]
        if preflight is not None:
            preflight(step)
        if step.contract.raster_nodata:
            # The input-check module uses PreparedStep and the shared path resolver.
            from geopilot.checks.input_checks import preflight_raster_nodata

            preflight_raster_nodata(step)
        environment = invoke(step)
        if any(not name.strip() or not version.strip() for name, version in environment.items()):
            raise AdapterError(
                "Environment names and versions must be nonempty",
                code="OUTPUT_INVALID", field="environment", exit_code=0,
            )
        result["environment"] = environment
        result["outputs"] = _verify_outputs(step)
        result.update(status="SUCCEEDED", exit_code=0)
    except AdapterError as error:
        failure = error
    except OSError as error:
        failure = AdapterError(str(error), code="STORAGE_FAILED", field="workdir_ref")

    if failure is not None:
        _record_failure(result, failure)
    result["finished_at"] = datetime.now(UTC).isoformat()
    if step is not None and result["log_path"] is not None:
        try:
            if failure is not None:
                with step.log_path.open("a", encoding="utf-8") as log:
                    log.write(f"{failure.phase}: {failure.code}: {failure.message}\n")
            _write_json(step.result_path, result)
        except OSError as error:
            _record_failure(result, AdapterError(
                str(error), code="STORAGE_FAILED", field="workdir_ref", exit_code=result["exit_code"]
            ))
    if telemetry is not None:
        telemetry.finish(result)
    return result
