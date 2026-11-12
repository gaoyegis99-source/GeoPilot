"""Check registered outputs and evaluate the confirmed method's artifact rules."""

import json
import math
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import numpy as np
import pyogrio
import rasterio
import shapely
from geopandas import GeoDataFrame
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pyogrio.errors import DataLayerError, DataSourceError, FeatureError, GeometryError
from pyproj import CRS
from pyproj.exceptions import ProjError
from rasterio.windows import Window
from shapely.errors import GEOSException

from geopilot.application.run_checks import RUN_CHECKS, RunCheckContext
from geopilot.artifacts.models import Artifact
from geopilot.artifacts.service import _file_state, _input_lineage, _require_workdir, list_artifacts
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.data_asset import DataAsset
from geopilot.domain.errors import _Text
from geopilot.domain.step_io import ExecutionStatus, StepFileRef, StepResult
from geopilot.execution.step_support import resolve_step_path
from geopilot.persistence.asset_repository import asset_for_ref
from geopilot.persistence.plan_repository import plan_from_row, row_for_plan
from geopilot.persistence.run_repository import get_run, list_run_steps
from geopilot.persistence.schema import open_database
from geopilot.plans.compiler import _same_json
from geopilot.plans.models import AnalysisPlan, PlanStatus
from geopilot.scheduler.output_resolver import resolve_named_output


Severity = Literal["blocking", "warning"]
_INTEGRITY = "artifact_integrity"
_DATA_ERRORS = (
    OSError, ValueError, RuntimeError, KeyError, DataLayerError, DataSourceError,
    FeatureError, GeometryError, ProjError, GEOSException,
)


class ArtifactCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    rule: _Text
    phase: Literal["artifact"] = "artifact"
    field: _Text
    severity: Severity = "blocking"
    passed: bool | None
    observed: JsonValue
    expected: JsonValue
    message: _Text


class NumericTolerance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    absolute: float = Field(ge=0)
    relative: float = Field(ge=0)
    source: _Text


@dataclass(frozen=True)
class ArtifactCheckContext:
    run_id: str
    workdir: Path
    plan: AnalysisPlan
    artifacts: Mapping[str, Artifact]
    steps: Mapping[str, StepResult]
    assets: Mapping[str, DataAsset]


ArtifactRule = Callable[[ArtifactCheckContext], tuple[ArtifactCheckResult, ...]]


def _finding(
    rule: str, field: str, passed: bool | None, observed: JsonValue, expected: JsonValue,
    message: str, *, severity: Severity = "blocking",
) -> ArtifactCheckResult:
    return ArtifactCheckResult(
        rule=rule, field=field, severity=severity, passed=passed,
        observed=observed, expected=expected, message=message,
    )


def _check_integrity(context: ArtifactCheckContext) -> tuple[list[ArtifactCheckResult], dict[str, object]]:
    expected = {item.role: item for item in context.plan.expected_artifacts}
    actual_roles, expected_roles = sorted(context.artifacts), sorted(expected)
    findings = [_finding(
        _INTEGRITY, "artifacts", actual_roles == expected_roles, actual_roles, expected_roles,
        "Compare registered artifact roles with the confirmed plan",
    )]
    verified_states = {}
    steps = {step.key: step for step in context.plan.steps}
    for role, binding in expected.items():
        record = context.artifacts.get(role)
        if record is None:
            continue
        field = f"artifacts.{role}"
        step = steps.get(binding.step)
        declared = step.outputs.get(binding.output) if step else None
        result = context.steps.get(binding.step)
        output = result.outputs.get(binding.output) if result else None
        if (
            declared is None or output is None or result.status is not ExecutionStatus.SUCCEEDED
            or record.run_id != context.run_id
            or (record.source.plan_id, record.source.plan_version) != (context.plan.id, context.plan.version)
            or (record.source.step_key, record.source.output_name, record.source.tool) != (binding.step, binding.output, step.tool)
            or record.source.inputs != _input_lineage(context.plan, binding.step)
            or record.output != output or record.uri != declared.target
            or record.type != binding.type or record.type != declared.type
            or (declared.layer is not None and record.layer != declared.layer)
        ):
            findings.append(_finding(
                _INTEGRITY, f"{field}.source", False, record.source.model_dump(mode="json"),
                {"plan_id": context.plan.id, "plan_version": context.plan.version,
                 "step_key": binding.step, "output_name": binding.output},
                "Registered artifact and producing result must match the exact confirmed plan",
            ))
            continue

        try:
            path, before = _file_state(context.workdir, record.uri, f"{field}.path", binding.step)
            digest = fingerprint_file(path).digest
            if digest != record.fingerprint:
                findings.append(_finding(
                    _INTEGRITY, f"{field}.fingerprint", False, digest, record.fingerprint,
                    "Artifact bytes differ from the registered content fingerprint",
                ))
                continue
            reopened = resolve_named_output(context.workdir, record.output, field=f"{field}.path")
            _, after = _file_state(context.workdir, record.uri, f"{field}.path", binding.step)
            if reopened.layer != record.layer or before != after:
                raise ValueError("Artifact layer or file state changed during verification")
        except _DATA_ERRORS as error:
            findings.append(_finding(_INTEGRITY, f"{field}.path", False, record.uri, record.uri, str(error)))
            continue
        verified_states[role] = after
        findings.append(_finding(
            _INTEGRITY, f"{field}.fingerprint", True, digest, record.fingerprint,
            "Reopened the registered artifact and verified its content fingerprint",
        ))
    return findings, verified_states


@contextmanager
def _staged_reference(context: ArtifactCheckContext, role: str) -> Iterator[tuple[Path, StepFileRef]]:
    asset = context.assets[role]
    snapshot_path = resolve_step_path(context.workdir, "request.json", field="request")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict) or snapshot.get("run_id") != context.run_id or not _same_json(
        snapshot.get("plan"), context.plan.model_dump(mode="json"),
    ):
        raise ValueError("Run request does not contain the exact confirmed plan snapshot")
    asset_files = snapshot.get("asset_files")
    if not isinstance(asset_files, dict) or asset_files.keys() != context.plan.inputs.keys():
        raise ValueError("Run request does not name the confirmed input roles")
    reference = StepFileRef.model_validate(asset_files[role])
    if PurePosixPath(reference.path).parts[0] != "inputs" or reference.layer != asset.source.layer:
        raise ValueError(f"Staged reference for {role!r} differs from the confirmed asset source")
    field = f"inputs.{role}"
    path, before = _file_state(context.workdir, reference.path, field, "")
    if fingerprint_file(path).digest != asset.fingerprint:
        raise ValueError(f"Staged reference for {role!r} differs from the confirmed asset fingerprint")
    yield path, reference
    _, after = _file_state(context.workdir, reference.path, field, "")
    if before != after:
        raise ValueError(f"Staged reference for {role!r} changed during the check")


def _legacy_rule(
    name: str, field: str, expected: JsonValue, *, reference_role: str | None = None,
) -> ArtifactRule:
    def check(context: ArtifactCheckContext) -> tuple[ArtifactCheckResult, ...]:
        try:
            reference = _staged_reference(context, reference_role) if reference_role is not None else nullcontext(None)
            with reference as staged:
                asset_files = {reference_role: staged[1]} if staged is not None else {}
                result = RUN_CHECKS[name](RunCheckContext(
                    context.plan, context.workdir, asset_files, context.steps,
                    {role: artifact.output for role, artifact in context.artifacts.items()},
                ))
        except _DATA_ERRORS as error:
            return (_finding(name, field, False, None, expected, str(error)),)
        return (_finding(name, field, result.passed, result.observed, expected, result.message),)
    return check


ARTIFACT_RULES: Mapping[str, ArtifactRule] = {
    "output_exists": _legacy_rule("output_exists", "artifacts", {"problems": {}}),
    "output_clipped_to_analysis_extent": _legacy_rule(
        "output_clipped_to_analysis_extent", "artifacts.selected_targets.scope", {"outside_ids": []},
        reference_role="analysis_extent",
    ),
    "detail_count_matches_summary": _legacy_rule(
        "detail_count_matches_summary", "artifacts.zone_statistics",
        {"missing_ids": [], "unexpected_ids": [], "duplicate_ids": [], "count_mismatch_rows": [],
         "summary_total_count": "selected_unique_count"},
    ),
}


def _vector(path: Path, layer: str | None) -> GeoDataFrame:
    frame = pyogrio.read_dataframe(path, layer=layer)
    if not isinstance(frame, GeoDataFrame):
        raise ValueError("Expected a vector layer with geometry")
    return frame


def _invalid_geometry(frame: GeoDataFrame) -> int:
    return int((~frame.geometry.is_valid | frame.geometry.is_empty).sum())


def _scope(
    context: ArtifactCheckContext, frame: GeoDataFrame, scope_role: str, name: str, field: str, severity: Severity,
) -> ArtifactCheckResult:
    expected = {"reference_role": scope_role, "outside_rows": 0}
    try:
        with _staged_reference(context, scope_role) as (path, reference):
            extent = _vector(path, reference.layer)
            if extent.empty or _invalid_geometry(extent) or not extent.geom_type.isin({"Polygon", "MultiPolygon"}).all():
                raise ValueError("Scope reference requires valid, nonempty polygons")
            if frame.crs is None or extent.crs is None or not frame.crs.equals(extent.crs, ignore_axis_order=True):
                raise ValueError("Output and scope require equivalent known CRSs")
            coverage = shapely.union_all(extent.geometry.to_numpy())
            outside = int(np.count_nonzero(~shapely.covers(coverage, frame.geometry.to_numpy())))
    except _DATA_ERRORS as error:
        return _finding(name, field, False, None, expected, str(error), severity=severity)
    return _finding(name, field, outside == 0, {"outside_rows": outside}, expected,
                    "Compare output geometry with the staged scope", severity=severity)


def vector_rule(
    name: str, role: str, *, scope_role: str | None = None, expected_count: int | None = None,
    severity: Severity = "blocking",
) -> ArtifactRule:
    """Check actual vector geometry, optionally against a scope or exact feature count."""
    if expected_count is not None and (type(expected_count) is not int or expected_count < 0):
        raise ValueError("expected_count must be a nonnegative integer")

    def check(context: ArtifactCheckContext) -> tuple[ArtifactCheckResult, ...]:
        field = f"artifacts.{role}"
        try:
            artifact = context.artifacts[role]
            if artifact.kind != "vector":
                raise ValueError(f"Artifact {role!r} is not a vector layer")
            frame = _vector(resolve_step_path(context.workdir, artifact.uri, field=field), artifact.layer)
        except _DATA_ERRORS as error:
            return (_finding(name, field, False, None, "vector", str(error), severity=severity),)
        invalid = _invalid_geometry(frame)
        findings = [_finding(
            name, f"{field}.geometry", invalid == 0, {"invalid_rows": invalid, "feature_count": len(frame)},
            {"invalid_rows": 0}, "Check vector geometries for missing, empty or invalid rows", severity=severity,
        )]
        if expected_count is not None:
            findings.append(_finding(name, f"{field}.feature_count", len(frame) == expected_count, len(frame),
                                     expected_count, "Compare reopened feature count", severity=severity))
        if scope_role is not None:
            findings.append(
                _finding(name, f"{field}.scope", None, None, {"reference_role": scope_role, "outside_rows": 0},
                         "Scope check requires valid output geometries", severity=severity)
                if invalid else _scope(context, frame, scope_role, name, f"{field}.scope", severity)
            )
        return tuple(findings)
    return check


def _json_number(value: object) -> JsonValue:
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        return value
    return str(value)


def _grid(dataset: rasterio.DatasetReader) -> dict[str, JsonValue]:
    return {
        "width": dataset.width, "height": dataset.height, "band_count": dataset.count,
        "crs": dataset.crs.to_string() if dataset.crs is not None else None,
        "transform": list(dataset.transform)[:6], "resolution": list(dataset.res), "dtypes": list(dataset.dtypes),
        "nodata": [_json_number(value) for value in dataset.nodatavals],
        "descriptions": list(dataset.descriptions), "units": list(dataset.units),
    }


def compare_raster_pixels(
    actual: rasterio.DatasetReader, windows: Iterable[Window],
    read_reference: Callable[[Window], np.ma.MaskedArray], tolerance: NumericTolerance,
) -> dict[str, JsonValue]:
    """Compare matching grids blockwise and report border and interior separately."""
    count_keys = ("common_valid_pixels", "mask_mismatches", "value_mismatches")
    counts = {name: dict.fromkeys(count_keys, 0) for name in ("edge", "interior")}
    first_value_mismatch = None
    for window in windows:
        observed = actual.read(window=window, masked=True)
        expected = read_reference(window)
        actual_mask = np.ma.getmaskarray(observed)
        reference_mask = np.ma.getmaskarray(expected)
        common = ~(actual_mask | reference_mask)
        common_pixels = np.all(common, axis=0)
        mask_differences = actual_mask != reference_mask
        # Integer DN and other exact comparisons retain their stored dtype.
        if tolerance.absolute == 0 and tolerance.relative == 0:
            close = np.equal(observed.data, expected.data)
        else:
            dtype = np.result_type(observed.dtype, expected.dtype, np.float64)
            close = np.isclose(observed.data.astype(dtype), expected.data.astype(dtype),
                               atol=tolerance.absolute, rtol=tolerance.relative)
        finite = np.isfinite(observed.data) & np.isfinite(expected.data)
        bad = common & ~(close & finite)
        rows = int(window.row_off) + np.arange(common_pixels.shape[0])
        columns = int(window.col_off) + np.arange(common_pixels.shape[1])
        edge = ((rows == 0) | (rows == actual.height - 1))[:, None] | ((columns == 0) | (columns == actual.width - 1))
        for name, positions in (("edge", edge), ("interior", ~edge)):
            counts[name]["common_valid_pixels"] += int(np.count_nonzero(common_pixels & positions))
            counts[name]["mask_mismatches"] += int(np.count_nonzero(mask_differences & positions))
            counts[name]["value_mismatches"] += int(np.count_nonzero(bad & positions))
        if first_value_mismatch is None and np.any(bad):
            band, row, column = map(int, np.unravel_index(bad.argmax(), bad.shape))
            first_value_mismatch = {
                "band": band + 1, "row": row + int(window.row_off), "column": column + int(window.col_off),
                "observed": _json_number(observed.data[band, row, column]),
                "expected": _json_number(expected.data[band, row, column]),
            }
    totals = {key: sum(subset[key] for subset in counts.values()) for key in count_keys}
    return {**totals, **counts, "first_value_mismatch": first_value_mismatch}


def raster_match_rule(
    name: str, role: str, *, reference_role: str, tolerance: NumericTolerance, severity: Severity = "blocking",
) -> ArtifactRule:
    """Compare the actual raster with an exact staged asset and an explicit value tolerance."""
    tolerance = NumericTolerance.model_validate(tolerance)
    expected_pixels = {"mask_mismatches": 0, "value_mismatches": 0, "tolerance": tolerance.model_dump(mode="json")}

    def check(context: ArtifactCheckContext) -> tuple[ArtifactCheckResult, ...]:
        field = f"artifacts.{role}"
        grid_finding = None
        try:
            artifact = context.artifacts[role]
            if artifact.kind != "raster":
                raise ValueError(f"Artifact {role!r} is not a raster")
            path = resolve_step_path(context.workdir, artifact.uri, field=field)
            with _staged_reference(context, reference_role) as (source, _):
                with rasterio.open(path) as actual, rasterio.open(source) as reference:
                    observed_grid, expected_grid = _grid(actual), _grid(reference)
                    differences = [key for key in observed_grid if key != "crs" and observed_grid[key] != expected_grid[key]]
                    if not actual.crs or not reference.crs or not CRS(actual.crs).equals(CRS(reference.crs), ignore_axis_order=True):
                        differences.append("crs")
                    grid_finding = _finding(
                        name, f"{field}.grid", not differences, {**observed_grid, "differences": differences},
                        expected_grid, "Compare raster grid and band metadata with the staged reference", severity=severity,
                    )
                    pixels = None if differences else compare_raster_pixels(
                        actual, (window for _, window in reference.block_windows(1)),
                        lambda window: reference.read(window=window, masked=True), tolerance,
                    )
        except _DATA_ERRORS as error:
            return (_finding(name, field, False, None, {"reference_role": reference_role}, str(error), severity=severity),)
        pixel_finding = _finding(
            name, f"{field}.pixels", None if pixels is None else not (pixels["mask_mismatches"] or pixels["value_mismatches"]),
            pixels, expected_pixels, "Pixel comparison requires equivalent grids" if pixels is None
            else "Compare per-band masks and valid values with the staged reference", severity=severity,
        )
        return grid_finding, pixel_finding
    return check


def check_run_artifacts(
    database_path: str | Path, run_id: str, *, rules: Mapping[str, ArtifactRule] | None = None,
) -> tuple[ArtifactCheckResult, ...]:
    """Read exact stored run evidence and return integrity and requested artifact findings."""
    with open_database(database_path, read_only=True) as connection:
        connection.execute("BEGIN")
        run = get_run(connection, run_id)
        if run is None:
            raise LookupError(f"Run {run_id!r} does not exist")
        row = row_for_plan(connection, run.plan_id, run.plan_version)
        if row is None:
            raise LookupError(f"Plan {run.plan_id!r} v{run.plan_version} does not exist")
        plan = plan_from_row(row)
        records = list_artifacts(connection, run.id)
        steps = list_run_steps(connection, run.id)
        assets = {role: asset for role, ref in plan.inputs.items()
                  if (asset := asset_for_ref(connection, ref)) is not None}

    registry = ARTIFACT_RULES if rules is None else rules
    selected = [name for name in dict.fromkeys(plan.check_rules) if name in registry]
    context = ArtifactCheckContext(run.id, Path(run.workdir), plan, {item.role: item for item in records},
                                   {item.step_key: item.result for item in steps if item.result is not None}, assets)
    states = {}
    if run.execution_status is not ExecutionStatus.SUCCEEDED:
        findings = [_finding(_INTEGRITY, "run.execution_status", None, run.execution_status.value,
                             ExecutionStatus.SUCCEEDED.value, "Artifact checks require a successful execution")]
    else:
        try:
            if plan.status is not PlanStatus.CONFIRMED:
                raise ValueError("Artifact checks require the confirmed plan snapshot")
            _require_workdir(context.workdir)
            findings, states = _check_integrity(context)
        except _DATA_ERRORS as error:
            findings = [_finding(_INTEGRITY, "artifacts", False, None, "registered outputs", str(error))]

    if any(item.passed is not True for item in findings):
        findings.extend(_finding(name, "artifacts", None, None, None, "Artifact integrity prerequisite was not met")
                        for name in selected)
        return tuple(findings)

    for name in selected:
        evidence = registry[name](context)
        if not evidence or any(item.rule != name for item in evidence):
            raise ValueError(f"Artifact rule {name!r} returned missing or misnamed evidence")
        findings.extend(evidence)

    for role, before in states.items():
        record = context.artifacts[role]
        field = f"artifacts.{role}.path"
        try:
            _, after = _file_state(context.workdir, record.uri, field, record.source.step_key)
            if before != after:
                raise ValueError("Artifact file state changed during rule evaluation")
        except _DATA_ERRORS as error:
            findings.append(_finding(_INTEGRITY, field, False, record.uri, record.uri, str(error)))
    return tuple(findings)
