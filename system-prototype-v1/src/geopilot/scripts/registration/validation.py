"""Verify a candidate against a published trial and independent local evidence."""

import shlex
from collections.abc import Iterable, Mapping
from pathlib import Path

import pyogrio
from pyogrio.errors import DataLayerError, DataSourceError
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely import from_wkt, normalize, to_wkb
from shapely.errors import GEOSException

from geopilot.checks.input_checks import check_step_inputs
from geopilot.data.errors import SourceReadError
from geopilot.data.fingerprint import _sidecars, fingerprint_file
from geopilot.domain.data_asset import AssetAvailability
from geopilot.domain.step_io import ExecutionStatus
from geopilot.execution.step_support import AdapterError, PreparedStep, _inspect_output, resolve_step_path
from geopilot.methods.validator import validate_method
from geopilot.persistence.asset_repository import asset_for_ref
from geopilot.persistence.schema import open_database
from geopilot.persistence.workspace import _open_path
from geopilot.plans.compiler import _same_json
from geopilot.plans.dependency_validator import validate_dependencies
from geopilot.scripts.models import ScriptSnapshot
from geopilot.scripts.registration.models import ReferenceOutput, RegistrationCandidate, RegistrationEvidence
from geopilot.scripts.runner import SCRIPT_LOG_PATH, SCRIPT_RESULT_PATH, ScriptRunResult, load_script_result
from geopilot.scripts.snapshot import SCRIPT_REQUEST_PATH, _read_file, load_script_snapshot
from geopilot.tools.parameter_normalizer import ParameterNormalizationError, normalize_parameters
from geopilot.tools.registry import ToolRegistry


_READ_ERRORS = (OSError, ValueError, SourceReadError, AdapterError, DataSourceError, DataLayerError)


class RegistrationError(ValueError):
    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


def _validate_declarations(candidate: RegistrationCandidate, registered_rules: Iterable[str]) -> None:
    tool, method, reference = candidate.tool, candidate.method, candidate.reference
    if tool.runner != "python":
        raise RegistrationError("METHOD_TOOL_MISMATCH", "tool.runner", "A local script must use the Python runner")
    for field, value in (
        ("tool.outputs", tool.outputs), ("tool.preconditions", tool.preconditions), ("tool.check_rules", tool.check_rules),
        ("method.preconditions", method.preconditions), ("method.steps", method.steps),
        ("method.expected_artifacts", method.expected_artifacts), ("method.check_rules", method.check_rules),
    ):
        if not value:
            raise RegistrationError("CONTRACT_INCOMPLETE", field, f"Provide a declaration for {field}")
    for index, step in enumerate(method.steps):
        if step.tool != tool.ref:
            raise RegistrationError(
                "METHOD_TOOL_MISMATCH", f"method.steps.{index}.tool", "Use the proposed tool ID and exact version",
            )

    rules = frozenset(registered_rules)
    for index, name in enumerate(tool.check_rules):
        if name not in rules:
            raise RegistrationError("UNKNOWN_CHECK_RULE", f"tool.check_rules.{index}", f"Unknown check rule {name!r}")
    registry = ToolRegistry(python_implementations=(tool.implementation,))
    registry.register(tool)
    issues = (*validate_method(method, registry=registry, registered_rules=rules),
              *validate_dependencies(method.inputs, method.steps))
    if issues:
        first = issues[0]
        raise RegistrationError(first.code, f"method.{first.field}", first.message)

    if reference.inputs.keys() != tool.inputs.keys():
        raise RegistrationError("REFERENCE_MISMATCH", "reference.inputs", "Reference inputs must match the tool roles")
    if reference.outputs.keys() != tool.outputs.keys():
        raise RegistrationError("REFERENCE_MISMATCH", "reference.outputs", "Supply a reference for each tool output role")
    if len({output.path for output in reference.outputs.values()}) != len(reference.outputs):
        raise RegistrationError("REFERENCE_MISMATCH", "reference.outputs", "Each output role requires a separate file")
    for role, output in reference.outputs.items():
        if output.features is not None and not tool.outputs[role].startswith("vector"):
            raise RegistrationError("REFERENCE_MISMATCH", f"reference.outputs.{role}.features", "Expected a vector output role")


def _read_trial(root: Path, run_id: str) -> tuple[ScriptSnapshot, ScriptRunResult]:
    field = "reference.script_run_id"
    try:
        snapshot = load_script_snapshot(root, run_id)
        result = load_script_result(root, run_id)
    except (OSError, ValueError) as error:
        raise RegistrationError("SCRIPT_RUN_INVALID", field, str(error)) from error
    if (result.status is not ExecutionStatus.SUCCEEDED or result.exit_code != 0
            or (result.script_id, result.version) != (snapshot.script_id, snapshot.version)
            or result.started_at < snapshot.created_at):
        raise RegistrationError("SCRIPT_RUN_INVALID", field, "A successful result for the exact snapshot is required")

    try:
        with _open_path(root, ("scripts", run_id)) as directory:
            lines = _read_file(directory, SCRIPT_LOG_PATH).decode("utf-8").splitlines()
        command = shlex.split(lines[0][2:]) if lines and lines[0].startswith("$ ") else []
    except (OSError, ValueError) as error:
        raise RegistrationError("SCRIPT_RUN_INVALID", field, str(error)) from error
    expected = ["-E", "-B", "-u", "-X", "utf8", snapshot.script_path, "--request", SCRIPT_REQUEST_PATH]
    if command[1:] != expected or lines[-1] != "exit_code: 0":
        raise RegistrationError("SCRIPT_RUN_INVALID", field, "The process log does not record a successful snapshot invocation")
    return snapshot, result


def _check_file_group(workdir: Path, path: Path, field: str, manifest: Mapping[str, int] | None = None) -> None:
    for member in (path, *(part for _, part in _sidecars(path))):
        relative = member.relative_to(workdir).as_posix()
        resolve_step_path(workdir, relative, field=field)
        if manifest is not None and manifest.get(relative) != member.stat().st_size:
            raise ValueError(f"File {relative!r} is missing from the run manifest or its size changed")


def _verify_inputs(database_path: str | Path, workdir: Path, snapshot: ScriptSnapshot, candidate: RegistrationCandidate) -> None:
    case, tool = candidate.reference, candidate.tool
    if snapshot.inputs != case.inputs:
        raise RegistrationError("REFERENCE_MISMATCH", "reference.inputs", "Input asset versions differ from the trial")
    if not _same_json(snapshot.parameters, case.parameters):
        raise RegistrationError("REFERENCE_MISMATCH", "reference.parameters", "Parameters differ from the trial")
    try:
        parameters = normalize_parameters(tool, snapshot.parameters)
    except ParameterNormalizationError as error:
        raise RegistrationError(error.code, f"reference.{error.field}", error.message) from error

    with open_database(database_path, read_only=True) as connection, connection:
        connection.execute("BEGIN")
        recorded = {role: asset_for_ref(connection, ref) for role, ref in snapshot.inputs.items()}
    for role, asset in recorded.items():
        field = f"reference.inputs.{role}"
        staged = snapshot.asset_files[role]
        if asset is None or asset.availability is not AssetAvailability.AVAILABLE or staged.layer != asset.source.layer:
            raise RegistrationError("INPUT_CHANGED", field, "The registered input version or exact layer is unavailable")
        try:
            path = resolve_step_path(workdir, staged.path, field=field)
            _check_file_group(workdir, path, field)
            if fingerprint_file(path).digest != asset.fingerprint:
                raise RegistrationError("INPUT_CHANGED", field, "The staged input differs from its registered version")
            _inspect_output(path, tool.inputs[role], staged.layer)
        except RegistrationError:
            raise
        except _READ_ERRORS as error:
            raise RegistrationError("REFERENCE_INPUT_INVALID", field, str(error)) from error

    prepared = PreparedStep(
        request={"inputs": {role: value.model_dump() for role, value in snapshot.asset_files.items()}, "parameters": parameters},
        contract=tool, workdir=workdir, log_path=workdir / SCRIPT_LOG_PATH,
        request_path=workdir / SCRIPT_REQUEST_PATH, result_path=workdir / SCRIPT_RESULT_PATH,
    )
    for finding in check_step_inputs(prepared):
        if not finding.passed and finding.severity == "blocking":
            raise RegistrationError("REFERENCE_INPUT_INVALID", f"reference.{finding.field}", finding.message)


def _verify_vector(path: Path, layer: str | None, expected: ReferenceOutput, field: str) -> None:
    frame = pyogrio.read_dataframe(path, layer=layer)
    try:
        crs = CRS.from_user_input(expected.crs)
    except CRSError as error:
        raise RegistrationError("OUTPUT_REFERENCE_MISMATCH", f"{field}.crs", "Reference CRS is invalid") from error
    if frame.crs is None or not frame.crs.equals(crs, ignore_axis_order=True):
        raise RegistrationError("OUTPUT_REFERENCE_MISMATCH", f"{field}.crs", "Output CRS differs from the reference")
    if "source_feature_id" not in frame:
        raise RegistrationError("OUTPUT_REFERENCE_MISMATCH", f"{field}.features", "Output has no source_feature_id column")
    identifiers = frame.source_feature_id.tolist()
    if any(not isinstance(identifier, str) for identifier in identifiers) or len(identifiers) != len(set(identifiers)):
        raise RegistrationError("OUTPUT_REFERENCE_MISMATCH", f"{field}.features", "Output feature IDs must be unique text values")
    assert expected.features is not None
    if set(identifiers) != expected.features.keys():
        raise RegistrationError("OUTPUT_REFERENCE_MISMATCH", f"{field}.features", "Output feature IDs differ from the reference")
    for identifier, observed in zip(identifiers, frame.geometry, strict=True):
        location = f"{field}.features.{identifier}"
        try:
            wanted = from_wkt(expected.features[identifier])
        except GEOSException as error:
            raise RegistrationError("OUTPUT_REFERENCE_MISMATCH", location, "Reference WKT is invalid") from error
        if (observed is None or observed.is_empty or not observed.is_valid or wanted.is_empty or not wanted.is_valid
                or to_wkb(normalize(observed)) != to_wkb(normalize(wanted))):
            raise RegistrationError("OUTPUT_REFERENCE_MISMATCH", location, "Output geometry differs from the exact reference")


def _verify_outputs(workdir: Path, result: ScriptRunResult, candidate: RegistrationCandidate) -> dict[str, str]:
    manifest = {item.path: item.size_bytes for item in result.outputs}
    if len(manifest) != len(result.outputs):
        raise RegistrationError("SCRIPT_RUN_INVALID", "reference.script_run_id", "The trial manifest repeats an output path")

    fingerprints = {}
    for role, reference in candidate.reference.outputs.items():
        field = f"reference.outputs.{role}"
        try:
            path = resolve_step_path(workdir, reference.path, field=f"{field}.path")
            _check_file_group(workdir, path, f"{field}.path", manifest)
            observed = fingerprint_file(path).digest
            if reference.fingerprint is not None and observed != reference.fingerprint:
                raise RegistrationError("OUTPUT_REFERENCE_MISMATCH", f"{field}.fingerprint", "Output fingerprint differs from the reference")
            layer = _inspect_output(path, candidate.tool.outputs[role], reference.layer)
            if reference.features is not None:
                _verify_vector(path, layer, reference, field)
            if fingerprint_file(path).digest != observed:
                raise RegistrationError("OUTPUT_CHANGED", f"{field}.path", "Output changed during verification")
        except RegistrationError:
            raise
        except (*_READ_ERRORS, GEOSException) as error:
            raise RegistrationError("REFERENCE_OUTPUT_INVALID", f"{field}.path", str(error)) from error
        fingerprints[role] = observed
    return fingerprints


def validate_manual_registration(
    database_path: str | Path, workspace_root: str | Path, candidate: RegistrationCandidate, *, registered_rules: Iterable[str],
) -> RegistrationEvidence:
    """Validate the proposal and read its local evidence without publishing or executing it."""
    candidate = RegistrationCandidate.model_validate(candidate)
    _validate_declarations(candidate, registered_rules)
    root = Path(workspace_root).resolve()
    run_id = candidate.reference.script_run_id
    snapshot, result = _read_trial(root, run_id)
    if snapshot.script_sha256 != candidate.reference.script_sha256:
        raise RegistrationError("REFERENCE_MISMATCH", "reference.script_sha256", "Script SHA-256 differs from the reference")
    workdir = root / "scripts" / run_id
    _verify_inputs(database_path, workdir, snapshot, candidate)
    fingerprints = _verify_outputs(workdir, result, candidate)
    return RegistrationEvidence(
        script_run_id=run_id, script_id=snapshot.script_id, script_version=snapshot.version,
        script_sha256=snapshot.script_sha256, tool=candidate.tool.ref, method=candidate.method.ref,
        output_fingerprints=fingerprints,
    )
