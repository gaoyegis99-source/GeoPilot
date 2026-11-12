"""Validate final artifacts and insert the complete batch in a run transaction."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from geopilot.artifacts.models import Artifact, ArtifactKind, ArtifactSource
from geopilot.artifacts.raster_metadata import read_raster_artifact_metadata
from geopilot.data import csv_output
from geopilot.data.fingerprint import _sidecars, capture_file_state, fingerprint_file
from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.vector_reader import read_vector_metadata
from geopilot.domain.data_asset import DataAssetRef, FieldMetadata, TableMetadata
from geopilot.domain.step_io import ExecutionStatus, StepOutput, StepResult
from geopilot.execution.step_support import AdapterError, resolve_step_path
from geopilot.methods.models import AssetRoleBinding
from geopilot.persistence.plan_repository import plan_from_row, row_for_plan
from geopilot.persistence.run_repository import get_run, list_run_steps
from geopilot.plans.dependency_validator import validate_dependencies
from geopilot.plans.models import AnalysisPlan, PlanStatus
from geopilot.scheduler.output_resolver import resolve_named_output


ARTIFACT_MANIFEST_PATH = "outputs/manifest.json"


class ArtifactRegistrationError(ValueError):
    def __init__(self, message: str, *, code: str, field: str, step_key: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field
        self.step_key = step_key


@dataclass(frozen=True, slots=True)
class _PreparedArtifact:
    record: Artifact
    file_state: object


@dataclass(frozen=True, slots=True)
class PreparedArtifacts:
    run_id: str
    plan: AnalysisPlan
    workdir: Path
    results: tuple[StepResult, ...]
    _items: tuple[_PreparedArtifact, ...]

    @property
    def outputs(self) -> dict[str, StepOutput]:
        return {item.record.role: item.record.output for item in self._items}

    @property
    def manifest(self) -> dict[str, JsonValue]:
        return {"artifacts": {
            item.record.role: {**item.record.output.model_dump(mode="json"), "fingerprint": item.record.fingerprint}
            for item in self._items
        }}


def _require_workdir(workdir: Path) -> None:
    try:
        if not workdir.is_absolute() or workdir.resolve(strict=True) != workdir or not workdir.is_dir():
            raise ValueError("Expected the canonical local run directory")
    except (OSError, RuntimeError, ValueError) as error:
        raise ArtifactRegistrationError(str(error), code="OUTPUT_INVALID", field="workdir_ref") from error


def _file_state(workdir: Path, uri: str, field: str, step_key: str) -> tuple[Path, object]:
    try:
        path = resolve_step_path(workdir, uri, field=field)
        if not path.is_file():
            raise FileNotFoundError(f"Required artifact {uri!r} is missing")
        if any(companion.is_symlink() for _, companion in _sidecars(path)):
            raise ValueError("Artifact companion files must be local regular files")
        return path, capture_file_state(path)
    except FileNotFoundError as error:
        raise ArtifactRegistrationError(str(error), code="OUTPUT_MISSING", field=field, step_key=step_key) from error
    except (OSError, RuntimeError, ValueError) as error:
        raise ArtifactRegistrationError(str(error), code="OUTPUT_INVALID", field=field, step_key=step_key) from error


def _csv_metadata(path: Path) -> TableMetadata:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv_output.reader(stream, strict=True)
        header = next(reader, [])
        if not header:
            raise ValueError("CSV output requires a header")
        count = 0
        for row in reader:
            if not row:
                continue
            if len(row) != len(header):
                raise ValueError(f"CSV row at line {reader.line_num} does not match its header")
            count += 1
    return TableMetadata(
        format="CSV", row_count=count, fields=[FieldMetadata(name=name, dtype="string") for name in header],
    )


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"Nonstandard JSON constant {value!r}")


def _artifact_metadata(path: Path, output: StepOutput, layer: str | None) -> tuple[ArtifactKind, str, dict[str, JsonValue]]:
    suffix = path.suffix.lower()
    if suffix != ".gpkg" and layer is not None:
        raise ValueError("Layer selection requires a GeoPackage artifact")
    if output.type.startswith("raster"):
        raster = read_raster_artifact_metadata(path)
        return "raster", str(raster["format"]), raster
    elif suffix == ".csv":
        metadata = _csv_metadata(path)
    elif suffix == ".json" and output.type in {"json", "table"}:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream, parse_constant=_invalid_json_constant)
        kind = {dict: "object", list: "array", str: "string", int: "number", float: "number",
                bool: "boolean", type(None): "null"}[type(value)]
        count = len(value) if isinstance(value, (dict, list)) else None
        return "json", "JSON", {"kind": "json", "format": "JSON", "value_type": kind, "item_count": count}
    elif suffix == ".gpkg":
        selected, metadata = read_geopackage_metadata(path, layer)
        if selected != layer:
            raise ValueError("The reopened artifact layer has changed")
    else:
        metadata = read_vector_metadata(path)
    return metadata.kind, metadata.format, metadata.model_dump(mode="json")


def _reopen_artifact(workdir: Path, role: str, output: StepOutput, source: ArtifactSource, run_id: str) -> _PreparedArtifact:
    field = f"artifacts.{role}.path"
    path, before = _file_state(workdir, output.path, field, source.step_key)
    try:
        original = fingerprint_file(path)
        reference = resolve_named_output(workdir, output, field=field)
        kind, format_name, metadata = _artifact_metadata(path, output, reference.layer)
        verified = fingerprint_file(path)
    except AdapterError as error:
        raise ArtifactRegistrationError(error.message, code=error.code, field=field, step_key=source.step_key) from error
    except FileNotFoundError as error:
        raise ArtifactRegistrationError(str(error), code="OUTPUT_MISSING", field=field, step_key=source.step_key) from error
    except (OSError, RuntimeError, ValueError, csv_output.Error) as error:
        raise ArtifactRegistrationError(str(error), code="OUTPUT_INVALID", field=field, step_key=source.step_key) from error
    _, after = _file_state(workdir, output.path, field, source.step_key)
    if before != after or original.digest != verified.digest:
        raise ArtifactRegistrationError(
            "Artifact changed while being reopened", code="OUTPUT_INVALID", field=field, step_key=source.step_key,
        )
    record = Artifact(
        id=uuid4().hex, run_id=run_id, role=role, uri=output.path, layer=reference.layer,
        type=output.type, kind=kind, format=format_name, fingerprint=verified.digest,
        metadata=metadata, source=source, created_at=datetime.now(UTC),
    )
    return _PreparedArtifact(record, after)


def _input_lineage(plan: AnalysisPlan, step_key: str) -> dict[str, DataAssetRef]:
    steps = {step.key: step for step in plan.steps}
    pending = [step_key]
    visited = set()
    roles = set()
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        for binding in steps[key].inputs.values():
            if isinstance(binding, AssetRoleBinding):
                roles.add(binding.asset)
            else:
                pending.append(binding.step)
    return {role: reference for role, reference in plan.inputs.items() if role in roles}


def prepare_artifacts(
    run_id: str, plan: AnalysisPlan, workdir: Path, results: Mapping[str, StepResult],
) -> PreparedArtifacts:
    """Reopen the complete final set without modifying the files or database."""
    plan = AnalysisPlan.model_validate(plan)
    if plan.status is not PlanStatus.CONFIRMED:
        raise ArtifactRegistrationError("Artifact registration requires a confirmed plan", code="OUTPUT_INVALID", field="plan")
    workdir = Path(workdir)
    _require_workdir(workdir)
    issues = validate_dependencies(plan.inputs, plan.steps)
    if issues:
        issue = issues[0]
        raise ArtifactRegistrationError(issue.message, code="OUTPUT_INVALID", field=issue.field)

    verified = {}
    for step in plan.steps:
        result = results.get(step.key)
        if result is None:
            raise ArtifactRegistrationError(
                f"Result for step {step.key!r} is missing", code="OUTPUT_MISSING", field=f"steps.{step.key}", step_key=step.key,
            )
        result = StepResult.model_validate(result)
        if (result.run_id, result.step_key, result.status) != (run_id, step.key, ExecutionStatus.SUCCEEDED):
            raise ArtifactRegistrationError(
                "Expected a successful result of the exact run and step",
                code="OUTPUT_INVALID", field=f"steps.{step.key}", step_key=step.key,
            )
        verified[step.key] = result

    steps = {step.key: step for step in plan.steps}
    seen = set()
    prepared = []
    for binding in plan.expected_artifacts:
        field = f"artifacts.{binding.role}"
        step = steps.get(binding.step)
        if binding.role in seen or step is None or binding.output not in step.outputs:
            raise ArtifactRegistrationError(
                "Expected artifact role or output binding is invalid", code="OUTPUT_INVALID", field=field, step_key=binding.step,
            )
        seen.add(binding.role)
        output = verified[binding.step].outputs.get(binding.output)
        if output is None:
            raise ArtifactRegistrationError(
                f"Required named output {binding.output!r} is missing",
                code="OUTPUT_MISSING", field=field, step_key=binding.step,
            )
        declared = step.outputs[binding.output]
        if (output.type != binding.type or output.type != declared.type or output.path != declared.target
                or (declared.layer is not None and output.layer != declared.layer)):
            raise ArtifactRegistrationError(
                "Reported artifact path, layer or type differs from the confirmed plan",
                code="OUTPUT_INVALID", field=field, step_key=binding.step,
            )
        source = ArtifactSource(
            plan_id=plan.id, plan_version=plan.version, step_key=step.key, output_name=binding.output,
            tool=step.tool, inputs=_input_lineage(plan, step.key),
        )
        prepared.append(_reopen_artifact(workdir, binding.role, output, source, run_id))
    return PreparedArtifacts(run_id, plan, workdir, tuple(verified.values()), tuple(prepared))


def _artifact_values(record: Artifact) -> dict:
    return {
        **record.model_dump(mode="json", exclude={"metadata", "source"}),
        "metadata_json": json.dumps(record.metadata, ensure_ascii=False, allow_nan=False),
        "source_json": record.source.model_dump_json(),
    }


def insert_artifacts(connection: sqlite3.Connection, prepared: PreparedArtifacts) -> tuple[Artifact, ...]:
    """Insert a verified batch in the caller's run-state transaction."""
    if not connection.in_transaction:
        raise ArtifactRegistrationError("An active write transaction is required", code="STORAGE_FAILED", field="artifacts")
    run = get_run(connection, prepared.run_id)
    if (run is None or run.execution_status is not ExecutionStatus.RUNNING or run.cancel_requested
            or (run.plan_id, run.plan_version, Path(run.workdir)) != (prepared.plan.id, prepared.plan.version, prepared.workdir)):
        raise ArtifactRegistrationError("The artifact batch does not match the active run", code="OUTPUT_INVALID", field="run_id")
    row = row_for_plan(connection, run.plan_id, run.plan_version)
    if row is None or plan_from_row(row).model_dump_json() != prepared.plan.model_dump_json():
        raise ArtifactRegistrationError("The artifact batch does not match the confirmed snapshot", code="OUTPUT_INVALID", field="plan")
    steps = list_run_steps(connection, run.id)
    if tuple(step.result for step in steps) != prepared.results:
        raise ArtifactRegistrationError("The artifact batch differs from persisted step results", code="OUTPUT_INVALID", field="steps")

    _require_workdir(prepared.workdir)
    records = tuple(item.record for item in prepared._items)
    for item in prepared._items:
        record = item.record
        field = f"artifacts.{record.role}.path"
        _, state = _file_state(prepared.workdir, record.uri, field, record.source.step_key)
        if state != item.file_state:
            raise ArtifactRegistrationError(
                "Artifact changed before registration", code="OUTPUT_INVALID", field=field, step_key=record.source.step_key,
            )

    try:
        connection.execute("SAVEPOINT geopilot_artifacts")
        try:
            connection.executemany(
                """
                INSERT INTO artifact (
                    id, run_id, role, uri, layer, type, kind, format, fingerprint, metadata_json, source_json, created_at
                ) VALUES (
                    :id, :run_id, :role, :uri, :layer, :type, :kind, :format,
                    :fingerprint, :metadata_json, :source_json, :created_at
                )
                """, [_artifact_values(record) for record in records],
            )
        except BaseException as error:
            try:
                connection.execute("ROLLBACK TO SAVEPOINT geopilot_artifacts")
                connection.execute("RELEASE SAVEPOINT geopilot_artifacts")
            except sqlite3.Error as cleanup:
                error.add_note(f"Could not roll back artifact savepoint: {cleanup}")
            raise
        connection.execute("RELEASE SAVEPOINT geopilot_artifacts")
    except sqlite3.Error as error:
        raise ArtifactRegistrationError(str(error), code="STORAGE_FAILED", field="artifacts") from error
    return records


def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    data = dict(row)
    data["metadata"] = json.loads(data.pop("metadata_json"))
    data["source"] = ArtifactSource.model_validate_json(data.pop("source_json"))
    return Artifact.model_validate(data)


def get_artifact(connection: sqlite3.Connection, run_id: str, role: str) -> Artifact | None:
    row = connection.execute("SELECT * FROM artifact WHERE run_id = ? AND role = ?", (run_id, role)).fetchone()
    return None if row is None else _artifact_from_row(row)


def artifact_by_id(connection: sqlite3.Connection, artifact_id: str) -> Artifact | None:
    row = connection.execute("SELECT * FROM artifact WHERE id = ?", (artifact_id,)).fetchone()
    return None if row is None else _artifact_from_row(row)


def list_artifacts(connection: sqlite3.Connection, run_id: str) -> tuple[Artifact, ...]:
    return tuple(_artifact_from_row(row) for row in connection.execute(
        "SELECT * FROM artifact WHERE run_id = ? ORDER BY role", (run_id,),
    ))
