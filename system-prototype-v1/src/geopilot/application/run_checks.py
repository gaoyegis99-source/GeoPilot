"""Check actual artifacts and process evidence for the initial local methods."""

import csv
import json
import math
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Literal

import pyogrio
import shapely
from geopandas import GeoDataFrame
from pydantic import JsonValue
from pyogrio.errors import DataLayerError, DataSourceError
from pyproj import CRS
from pyproj.exceptions import ProjError
from shapely.errors import GEOSException

from geopilot.data.errors import SourceReadError
from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.vector_reader import read_layer_metadata
from geopilot.domain.data_asset import VectorMetadata
from geopilot.domain.step_io import ExecutionStatus, StepFileRef, StepOutput, StepRequest, StepResult
from geopilot.domain.tool_contract import ParameterSpec
from geopilot.execution.step_support import _inspect_output, resolve_step_path
from geopilot.gis.measurement.geodesic_buffer import MAX_CAP_ANGLE_DEG, MAX_SEGMENT_STEP_M
from geopilot.gis.measurement.geodesic_road_scope import (
    CANDIDATE_PADDING_M,
    ROAD_STATION_SPACING_M,
    WGS84,
    expand_wgs84_analysis_extent,
    prepare_wgs84_geometry,
    road_intersects_influence,
)
from geopilot.gis.validation.crs_validator import inspect_crs
from geopilot.plans.models import AnalysisPlan
from geopilot.tools.parameter_normalizer import _convert_quantity


@dataclass(frozen=True)
class RunCheckContext:
    plan: AnalysisPlan
    workdir: Path
    asset_files: Mapping[str, StepFileRef]
    steps: Mapping[str, StepResult]
    artifacts: Mapping[str, StepOutput]


@dataclass(frozen=True)
class RunCheckResult:
    passed: bool
    message: str
    observed: JsonValue = None
    severity: Literal["blocking", "warning"] = "blocking"


RunCheck = Callable[[RunCheckContext], RunCheckResult]
_DATA_ERRORS = (
    KeyError, OSError, ValueError, csv.Error, SourceReadError,
    DataLayerError, DataSourceError, ProjError, GEOSException,
)
_ID = "source_feature_id"
_POLYGONS = {"Polygon", "MultiPolygon"}
_LINES = {"LineString", "MultiLineString"}
_POINTS = {"Point"}
_SUMMARY_COLUMNS = {"zone_id", "assignment_status", "count", "source_feature_ids"}
_BUFFER_DISTANCE = ParameterSpec(type="number", unit="meter")
_GEODESIC_MEASUREMENTS = {
    "expand_analysis_extent": ("python-expand-wgs84-analysis-extent", "wgs84_ellipsoid_influence_envelope"),
    "create_buffer": ("python-wgs84-geodesic-buffer", "wgs84_geodesic_line_buffer"),
}
_GEOD = WGS84.get_geod()


def _report_data_errors(check: RunCheck) -> RunCheck:
    @wraps(check)
    def run(context: RunCheckContext) -> RunCheckResult:
        try:
            return check(context)
        except _DATA_ERRORS as error:
            return RunCheckResult(False, str(error))
    return run


def _file(context: RunCheckContext, relative: str, field: str) -> Path:
    path = resolve_step_path(context.workdir.resolve(strict=True), relative, field=field)
    if not path.is_file():
        raise ValueError(f"{field} must refer to an existing file: {relative}")
    return path


def _vector_metadata(
    context: RunCheckContext, reference: StepFileRef, field: str,
) -> tuple[Path, str | None, VectorMetadata]:
    path = _file(context, reference.path, field)
    layer = reference.layer
    if path.suffix.lower() == ".gpkg":
        layer, metadata = read_geopackage_metadata(path, layer)
    else:
        metadata = read_layer_metadata(path, layer)
    if not isinstance(metadata, VectorMetadata) or metadata.crs is None:
        raise ValueError(f"{field} must be a vector layer with known CRS")
    return path, layer, metadata


def _read_vector(context: RunCheckContext, reference: StepFileRef, field: str, kinds: set[str]) -> GeoDataFrame:
    path, layer, metadata = _vector_metadata(context, reference, field)
    if metadata.geometry_type.split()[0] not in kinds:
        raise ValueError(f"{field} has unexpected geometry type {metadata.geometry_type}")
    frame = pyogrio.read_dataframe(path, layer=layer)
    if (
        not isinstance(frame, GeoDataFrame) or frame.crs is None
        or not frame.crs.equals(CRS.from_user_input(metadata.crs), ignore_axis_order=True)
        or len(frame) != metadata.feature_count
    ):
        raise ValueError(f"{field} differs from its inspected metadata")
    geometry = frame.geometry
    if not geometry.geom_type.isin(kinds).all() or geometry.is_empty.any() or not geometry.is_valid.all():
        raise ValueError(f"{field} has invalid, empty, or unexpected geometries")
    return frame


def _identifiers(frame: GeoDataFrame, field: str) -> list[str]:
    if _ID not in frame:
        raise ValueError(f"{field} requires a {_ID} field")
    source = frame[_ID]
    identifiers = source.astype(str)
    if source.isna().any() or identifiers.str.strip().eq("").any():
        raise ValueError(f"{field} has empty {_ID} values")
    return identifiers.tolist()


def _successful_step(context: RunCheckContext, key: str) -> StepResult:
    result = context.steps.get(key)
    if result is None or result.step_key != key or result.status != ExecutionStatus.SUCCEEDED:
        raise ValueError(f"Expected a successful result for step {key}")
    return result


def _output_exists(context: RunCheckContext) -> RunCheckResult:
    opened = {}
    problems = {}
    for expected in context.plan.expected_artifacts:
        role = expected.role
        try:
            output = context.artifacts.get(role)
            if output is None:
                raise ValueError("Expected artifact is missing from the result references")
            if output.type != expected.type:
                raise ValueError(f"Expected artifact type {expected.type}, found {output.type}")
            path = _file(context, output.path, f"artifacts.{role}")
            layer = _inspect_output(path, output.type, output.layer)
            opened[role] = {"path": output.path, "layer": layer, "type": output.type}
        except _DATA_ERRORS as error:
            problems[role] = str(error)
    message = "All expected artifacts reopened" if not problems else "Expected artifacts missing or unreadable"
    return RunCheckResult(not problems, message, {"opened": opened, "problems": problems})


def _same_distance(value: JsonValue, expected: int | float) -> bool:
    return type(value) in (int, float) and value == expected


def _qgis_buffer_inputs(log: str) -> dict:
    marker = " --json run native:buffer -\nstdout:\n"
    if log.count(marker) != 1:
        raise ValueError("Expected one QGIS buffer process report")
    stdout, delimiter, _ = log.split(marker, 1)[1].partition("\nstderr:\n")
    if not delimiter:
        raise ValueError("QGIS buffer process report is incomplete")
    report = json.loads(stdout)
    if not isinstance(report, dict) or not isinstance(report.get("inputs"), dict):
        raise ValueError("QGIS buffer report has no engine inputs")
    return report["inputs"]


def _measurement_evidence(
    context: RunCheckContext, key: str, output_role: str, distance: int | float, expected_crs: CRS,
) -> dict[str, JsonValue]:
    result = _successful_step(context, key)
    if result.log_path is None:
        raise ValueError("The buffer result has no audit log")
    log = _file(context, result.log_path, f"steps.{key}.log_path").read_text(encoding="utf-8")
    request_ref = PurePosixPath(result.log_path).with_name("request.json").as_posix()
    request_path = _file(context, request_ref, f"steps.{key}.request")
    request = StepRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
    plan_step = next((step for step in context.plan.steps if step.key == key), None)
    if (
        plan_step is None or request.tool != plan_step.tool or request.step_key != key
        or request.run_id != result.run_id or request.log_path != result.log_path
    ):
        raise ValueError("Persisted buffer request does not match the plan and step result")
    requested = request.parameters.get("distance")
    if not _same_distance(plan_step.parameters.get("distance"), distance) or not _same_distance(requested, distance):
        raise ValueError(f"Buffer request distance {requested!r} differs from the confirmed {distance} m")

    audit = [line.removeprefix("measurement: ") for line in log.splitlines() if line.startswith("measurement: ")]
    if len(audit) != 1:
        raise ValueError("Expected one source measurement audit record")
    record = json.loads(audit[0])
    if not isinstance(record, dict) or (record.get("role"), record.get("method"), record.get("unit")) != (
        "source", "local_projected", "meter",
    ):
        raise ValueError("Buffer audit must report local projected measurement in metres")
    if not CRS.from_user_input(record.get("crs")).equals(expected_crs, ignore_axis_order=True):
        raise ValueError("Buffer audit CRS differs from the confirmed CRS")

    _, _, source = _vector_metadata(context, request.inputs["source"], f"steps.{key}.inputs.source")
    actual_crs = inspect_crs(source, require_projected_metres=True).crs
    if not actual_crs.equals(expected_crs, ignore_axis_order=True):
        raise ValueError("Reopened buffer source CRS differs from the confirmed CRS")
    output = result.outputs[output_role]
    target = request.output_targets[output_role]
    if output.path != target.path or (target.layer is not None and output.layer != target.layer):
        raise ValueError("Buffer output reference differs from the request")
    _, _, output_metadata = _vector_metadata(context, output, f"steps.{key}.outputs.{output_role}")
    if not CRS.from_user_input(output_metadata.crs).equals(expected_crs, ignore_axis_order=True):
        raise ValueError("Reopened buffer output CRS differs from the confirmed CRS")

    engine_distance = _qgis_buffer_inputs(log).get("DISTANCE")
    if not _same_distance(engine_distance, distance):
        raise ValueError(f"QGIS reported buffer distance {engine_distance!r}, expected {distance} m")
    return {
        "crs": actual_crs.to_string(), "unit": "meter",
        "request_distance_m": requested, "qgis_distance_m": engine_distance,
    }


def _geodesic_buffer_sample_count(context: RunCheckContext, source: StepFileRef, buffer: StepOutput) -> int:
    roads = _read_vector(context, source, "prepared buffer roads", _LINES)
    polygons = _read_vector(context, buffer, "geodesic road buffer", _POLYGONS)
    if roads.empty:
        if not polygons.empty:
            raise ValueError("An empty candidate layer must produce an empty geodesic buffer")
        return 0
    if len(polygons) != 1:
        raise ValueError("A nonempty road selection requires one dissolved geodesic buffer feature")

    coverage = polygons.geometry.iloc[0]
    shapely.prepare(coverage)
    samples = outside = 0
    for line in shapely.get_parts(roads.geometry.to_numpy()):
        vertices = shapely.get_coordinates(line)
        start, end = vertices[:-1], vertices[1:]
        azimuth, _, lengths = _GEOD.inv(start[:, 0], start[:, 1], end[:, 0], end[:, 1])
        longitude, latitude, _ = _GEOD.fwd(start[:, 0], start[:, 1], azimuth, lengths / 2)
        count = len(vertices) + len(start)
        covered = shapely.intersects_xy(coverage, vertices[:, 0], vertices[:, 1]).sum()
        covered += shapely.intersects_xy(coverage, longitude, latitude).sum()
        samples += count
        outside += count - int(covered)
    if outside:
        raise ValueError(f"{outside} of {samples} prepared road centreline samples lie outside the geodesic buffer")
    return samples


def _geodesic_measurement_evidence(
    context: RunCheckContext, key: str, distance: int | float,
) -> dict[str, JsonValue]:
    # The process checker imports the plan compiler, which in turn uses this rule catalog.
    from geopilot.checks.process_checks import _json, _read_process
    from geopilot.plans.compiler import _same_json, compile_step_request

    tool_id, algorithm = _GEODESIC_MEASUREMENTS[key]
    result = _successful_step(context, key)
    expected = compile_step_request(
        context.plan, key, run_id=result.run_id, asset_files=context.asset_files, prior_results=context.steps,
    )
    if (expected.tool.id, expected.tool.version) != (tool_id, 1) or result.log_path != expected.log_path:
        raise ValueError("The confirmed step must use the versioned WGS84 geodesic worker")
    request_ref = PurePosixPath(result.log_path).with_name("request.json")
    request_path = _file(context, request_ref.as_posix(), f"steps.{key}.request")
    request = StepRequest.model_validate(_json(request_path.read_text(encoding="utf-8")))
    if not _same_json(request.model_dump(mode="json"), expected.model_dump(mode="json")):
        raise ValueError("The persisted geodesic request differs from the confirmed step")
    requested = request.parameters.get("distance")
    if not _same_distance(requested, distance):
        raise ValueError(f"Geodesic request distance {requested!r} differs from the confirmed {distance} m")

    log = _file(context, result.log_path, f"steps.{key}.log_path").read_text(encoding="utf-8")
    process = _read_process(log, "python")
    script = tool_id.removeprefix("python-").replace("-", "_") + ".py"
    expected_args = ["--request", request_ref.as_posix(), "--result", request_ref.with_name("python-result.json").as_posix()]
    if (len(process.argv) != 6 or Path(process.argv[1]).name != script
            or process.argv[2:] != expected_args or process.exit_code != 0 or result.exit_code != 0):
        raise ValueError("The geodesic worker command or exit status differs from the request")

    decisions = [_json(line.removeprefix("measurement: ")) for line in log.splitlines()
                 if line.startswith("measurement: ")]
    source_decisions = [record for record in decisions if isinstance(record, dict) and record.get("role") == "source"]
    if len(source_decisions) != 1:
        raise ValueError("Expected one geodesic measurement preflight record for the source")
    decision = source_decisions[0]
    if (decision.get("method"), decision.get("unit")) != ("geodesic", "meter") or not WGS84.equals(
        CRS.from_user_input(decision.get("crs")), ignore_axis_order=True,
    ):
        raise ValueError("The geodesic preflight must report WGS84 and metres")

    audit_prefix = "geodesic_measurement: "
    records = [_json(line.removeprefix(audit_prefix)) for line in process.stdout.splitlines() if line.startswith(audit_prefix)]
    if len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError("The child process must record the actual WGS84 calculation")
    audit = records[0]
    if (audit.get("algorithm"), audit.get("ellipsoid")) != (algorithm, "WGS84"):
        raise ValueError("The child process did not use the expected WGS84 ellipsoidal algorithm")
    engine_distance = audit.get("distance_m")
    if not _same_distance(engine_distance, distance):
        raise ValueError(f"Geodesic process reported distance {engine_distance!r}, expected {distance} m")

    _, _, source = _vector_metadata(context, request.inputs["source"], f"steps.{key}.inputs.source")
    source_crs = CRS.from_user_input(source.crs)
    evidence: dict[str, JsonValue] = {
        "crs": WGS84.to_string(), "unit": "meter", "source_crs": source_crs.to_string(),
        "request_distance_m": requested, "engine_distance_m": engine_distance,
        "algorithm": algorithm, "ellipsoid": "WGS84",
    }
    if key == "create_buffer":
        if not WGS84.equals(source_crs, ignore_axis_order=True):
            raise ValueError("The geodesic buffer source must reopen in WGS84")
        if (audit.get("dissolve") is not True or request.parameters.get("dissolve") is not True
                or type(audit.get("road_count")) is not int or audit["road_count"] != source.feature_count
                or not _same_distance(audit.get("max_segment_step_m"), MAX_SEGMENT_STEP_M)
                or not _same_distance(audit.get("max_cap_angle_deg"), MAX_CAP_ANGLE_DEG)):
            raise ValueError("The child geodesic road buffer does not match its source or sampling policy")
        evidence.update(road_count=audit["road_count"], dissolve=True, max_segment_step_m=MAX_SEGMENT_STEP_M,
                        max_cap_angle_deg=MAX_CAP_ANGLE_DEG)
    else:
        padding, spacing = audit.get("candidate_padding_m"), audit.get("road_station_spacing_m")
        if (request.parameters.get("target_crs") != WGS84.to_string()
                or not _same_distance(padding, CANDIDATE_PADDING_M) or not _same_distance(spacing, ROAD_STATION_SPACING_M)):
            raise ValueError("The AOI calculation did not use the declared candidate padding and road sampling")
        evidence.update(candidate_padding_m=padding, road_station_spacing_m=spacing)

    for role, target in request.output_targets.items():
        output = result.outputs[role]
        if output.path != target.path or (target.layer is not None and output.layer != target.layer):
            raise ValueError(f"Geodesic output {role!r} differs from the recorded request")
        _, _, output_metadata = _vector_metadata(context, output, f"steps.{key}.outputs.{role}")
        if not WGS84.equals(CRS.from_user_input(output_metadata.crs), ignore_axis_order=True):
            raise ValueError(f"Geodesic output {role!r} must reopen in WGS84")
    if key == "create_buffer":
        evidence["centerline_samples"] = _geodesic_buffer_sample_count(context, request.inputs["source"], result.outputs["buffer"])
    return evidence


def _confirmed_road_distance(context: RunCheckContext) -> int | float:
    distance = context.plan.critical_options.get("distance_m")
    if isinstance(distance, dict):
        try:
            distance = _convert_quantity(_BUFFER_DISTANCE, distance)
        except OverflowError as error:
            raise ValueError("The confirmed buffer distance must be positive and finite") from error
    if type(distance) not in (int, float) or distance <= 0 or (type(distance) is float and not math.isfinite(distance)):
        raise ValueError("The confirmed buffer distance must be positive and finite")
    return distance


@_report_data_errors
def _measurement_policy_satisfied(context: RunCheckContext) -> RunCheckResult:
    options = context.plan.critical_options
    method = options.get("distance_method")
    if method not in ("local_projected", "geodesic"):
        raise ValueError("The point road method requires confirmed local projected measurement")
    distance = _confirmed_road_distance(context)
    expected_crs = CRS.from_user_input(options.get("distance_crs_ref")) if method == "local_projected" else WGS84
    observations = {}
    problems = {}
    for key, output_role in (("expand_analysis_extent", "expanded_extent"), ("create_buffer", "buffer")):
        try:
            observations[key] = (
                _geodesic_measurement_evidence(context, key, distance) if method == "geodesic"
                else _measurement_evidence(context, key, output_role, distance, expected_crs)
            )
        except _DATA_ERRORS as error:
            problems[key] = str(error)
    message = (
        ("Both buffer steps used the confirmed WGS84 geodesic distance" if method == "geodesic"
         else "Both buffer steps used the confirmed metre CRS and distance") if not problems
        else "Buffer measurement evidence differs from the plan"
    )
    return RunCheckResult(not problems, message, {"steps": observations, "problems": problems})


def _geodesic_output(context: RunCheckContext, step_key: str, role: str, kinds: set[str]) -> GeoDataFrame:
    field = f"steps.{step_key}.outputs.{role}"
    frame = _read_vector(context, _successful_step(context, step_key).outputs[role], field, kinds)
    if not frame.crs.equals(WGS84, ignore_axis_order=True):
        raise ValueError(f"{field} must use WGS84")
    return frame


def _feature_differences(expected: GeoDataFrame, actual: GeoDataFrame, field: str) -> tuple[list[str], list[str]]:
    def records(frame: GeoDataFrame) -> Counter:
        return Counter(zip(_identifiers(frame, field), frame.geometry.to_wkb(), strict=True))

    wanted, found = records(expected), records(actual)
    return sorted({identifier for identifier, _ in wanted - found}), sorted({identifier for identifier, _ in found - wanted})


def _geodesic_candidate_roads(context: RunCheckContext) -> RunCheckResult:
    roads = _read_vector(context, context.asset_files["roads"], "staged roads", _LINES)
    reference = _successful_step(context, "select_roads_from_expanded_extent").outputs["roads"]
    candidates = _read_vector(context, reference, "candidate roads", _LINES)
    if not candidates.crs.equals(roads.crs, ignore_axis_order=True):
        raise ValueError("Candidate road CRS differs from the staged road CRS")
    expanded = _geodesic_output(context, "expand_analysis_extent", "expanded_extent", _POLYGONS).geometry.union_all()
    extent = _geodesic_output(context, "expand_analysis_extent", "analysis_extent", _POLYGONS).geometry.union_all()
    source_extent = _read_vector(context, context.asset_files["analysis_extent"], "staged analysis extent", _POLYGONS)
    original_extent = shapely.union_all(prepare_wgs84_geometry(source_extent.geometry.to_numpy(), source_extent.crs))
    if original_extent.is_empty or not original_extent.equals(extent):
        raise ValueError("The prepared WGS84 analysis extent differs from the staged AOI")
    reference_extent = expand_wgs84_analysis_extent(original_extent, _confirmed_road_distance(context))
    extent_matches = expanded.covers(extent) and expanded.area > extent.area and expanded.equals(reference_extent)
    wgs84 = roads.set_geometry(prepare_wgs84_geometry(roads.geometry.to_numpy(), roads.crs), crs=WGS84)
    included = [road_intersects_influence(line, reference_extent) for line in wgs84.geometry]
    expected_roads = roads.loc[included]
    expected_wgs84 = wgs84.loc[included]
    expected = set(_identifiers(expected_roads, "expected candidate roads"))
    actual = set(_identifiers(candidates, "candidate roads"))
    prepared = _geodesic_output(context, "choose_measurement_method", "roads", _LINES)
    original_missing, original_extra = _feature_differences(expected_roads, candidates, "candidate roads")
    prepared_missing, prepared_extra = _feature_differences(expected_wgs84, prepared, "prepared roads")
    outside = [identifier for identifier, line in zip(_identifiers(expected_wgs84, "candidate roads"),
                                                    expected_wgs84.geometry, strict=True)
               if not road_intersects_influence(line, extent)]
    passed = extent_matches and not (original_missing or original_extra or prepared_missing or prepared_extra)
    message = ("Full candidate roads match the WGS84 influence extent and preparation" if passed
               else "The recorded extent, candidate roads or prepared roads differ from the confirmed WGS84 influence")
    return RunCheckResult(passed, message, {
        "expected_ids": sorted(expected), "actual_ids": sorted(actual), "outside_included_ids": sorted(set(outside) & actual),
        "missing_ids": sorted(expected - actual), "unexpected_ids": sorted(actual - expected),
        "expanded_extent_matches_reference": extent_matches,
        "original_feature_differences": {"missing_ids": original_missing, "unexpected_ids": original_extra},
        "prepared_feature_differences": {"missing_ids": prepared_missing, "unexpected_ids": prepared_extra},
    })


@_report_data_errors
def _outside_roads_within_influence_included(context: RunCheckContext) -> RunCheckResult:
    if context.plan.critical_options.get("distance_method") == "geodesic":
        return _geodesic_candidate_roads(context)
    expanded = _successful_step(context, "expand_analysis_extent").outputs["expanded_extent"]
    candidate = _successful_step(context, "select_roads_from_expanded_extent").outputs["roads"]
    roads = _read_vector(context, context.asset_files["roads"], "staged roads", _LINES)
    extent = _read_vector(context, expanded, "expanded extent", _POLYGONS)
    candidates = _read_vector(context, candidate, "candidate roads", _LINES)
    if not candidates.crs.equals(roads.crs, ignore_axis_order=True):
        raise ValueError("Candidate road CRS differs from the staged road CRS")
    if not extent.crs.equals(roads.crs, ignore_axis_order=True):
        extent = extent.to_crs(roads.crs)
    coverage = shapely.union_all(extent.geometry.to_numpy())
    intersects = shapely.intersects(roads.geometry.to_numpy(), coverage)
    expected = {
        identifier for identifier, included in zip(_identifiers(roads, "staged roads"), intersects, strict=True) if included
    }
    actual = set(_identifiers(candidates, "candidate roads"))
    passed = expected == actual
    message = (
        "Candidate roads match the expanded extent selection" if passed
        else "Candidate road IDs differ from roads intersecting the expanded extent"
    )
    return RunCheckResult(passed, message, {
        "expected_ids": sorted(expected), "actual_ids": sorted(actual),
        "missing_ids": sorted(expected - actual), "unexpected_ids": sorted(actual - expected),
    })


def _geodesic_clipped_targets(context: RunCheckContext) -> RunCheckResult:
    extent = _geodesic_output(context, "expand_analysis_extent", "analysis_extent", _POLYGONS).geometry.union_all()
    targets = _geodesic_output(context, "expand_analysis_extent", "targets", _POINTS)
    original = _read_vector(context, context.asset_files["targets"], "staged targets", _POINTS)
    expected = original.set_geometry(prepare_wgs84_geometry(original.geometry.to_numpy(), original.crs), crs=WGS84)
    prepared_missing, prepared_extra = _feature_differences(expected, targets, "prepared targets")
    buffer = _geodesic_output(context, "create_buffer", "buffer", _POLYGONS).geometry.union_all()
    relation = context.plan.critical_options.get("spatial_relation")
    if relation == "intersects":
        matching = targets.loc[shapely.intersects(targets.geometry.to_numpy(), buffer)]
    elif relation == "within":
        matching = targets.loc[shapely.within(targets.geometry.to_numpy(), buffer)]
    else:
        raise ValueError("The road plan needs a confirmed spatial relation")
    actual_related = _geodesic_output(context, "apply_spatial_relation", "targets", _POINTS)
    relation_missing, relation_extra = _feature_differences(matching, actual_related, "targets before clipping")

    retained = matching.loc[shapely.covers(extent, matching.geometry.to_numpy())]
    selected = _read_vector(context, context.artifacts["selected_targets"], "selected targets", _POINTS)
    if not selected.crs.equals(WGS84, ignore_axis_order=True):
        raise ValueError("Selected targets must use the prepared WGS84 analysis extent CRS")
    missing, extra = _feature_differences(retained, selected, "selected targets")
    original_retained = _geodesic_output(context, "clip_back_to_analysis_extent", "original_targets", _POINTS)
    original_missing, original_extra = _feature_differences(retained, original_retained, "retained original targets")
    outside = sorted({identifier for identifier, covered in zip(_identifiers(selected, "selected targets"),
                     shapely.covers(extent, selected.geometry.to_numpy()), strict=True) if not covered})
    passed = not any((prepared_missing, prepared_extra, relation_missing, relation_extra, missing, extra,
                      original_missing, original_extra, outside))
    message = ("Original target identities and geometry match the selected relation and WGS84 extent" if passed
               else "The WGS84 target preparation, relation or clip differs from the original target features")
    return RunCheckResult(passed, message, {
        "selected_rows": len(selected), "outside_ids": outside, "spatial_relation": relation,
        "missing_ids": missing, "unexpected_ids": extra,
        "prepared_feature_differences": {"missing_ids": prepared_missing, "unexpected_ids": prepared_extra},
        "relation_feature_differences": {"missing_ids": relation_missing, "unexpected_ids": relation_extra},
        "original_feature_differences": {"missing_ids": original_missing, "unexpected_ids": original_extra},
    })


@_report_data_errors
def _output_clipped_to_analysis_extent(context: RunCheckContext) -> RunCheckResult:
    if context.plan.critical_options.get("distance_method") == "geodesic":
        return _geodesic_clipped_targets(context)
    extent = _read_vector(context, context.asset_files["analysis_extent"], "analysis extent", _POLYGONS)
    selected = _read_vector(context, context.artifacts["selected_targets"], "selected targets", _POINTS)
    if not selected.crs.equals(extent.crs, ignore_axis_order=True):
        raise ValueError("Selected target CRS differs from the analysis extent CRS")
    coverage = shapely.union_all(extent.geometry.to_numpy())
    covered = shapely.covers(coverage, selected.geometry.to_numpy())
    outside = {
        identifier for identifier, inside in zip(_identifiers(selected, "selected targets"), covered, strict=True) if not inside
    }
    message = "All selected targets are covered by the analysis extent" if not outside else "Selected targets lie outside the analysis extent"
    return RunCheckResult(not outside, message, {"selected_rows": len(selected), "outside_ids": sorted(outside)})


@_report_data_errors
def _detail_count_matches_summary(context: RunCheckContext) -> RunCheckResult:
    return _point_count_summary(
        context, context.artifacts["selected_targets"], context.artifacts["zone_statistics"],
        targets_field="selected targets", summary_field="artifacts.zone_statistics",
        verify_zone_members=context.plan.critical_options.get("distance_method") == "geodesic",
    )


def _geodesic_zone_members(context: RunCheckContext, target_ids: set[str]) -> dict[str, list[str]]:
    original = _geodesic_output(context, "clip_back_to_analysis_extent", "original_targets", _POINTS)
    zones = _geodesic_output(context, "expand_analysis_extent", "zones", _POLYGONS)
    source_zones = _read_vector(context, context.asset_files["zones"], "staged zones", _POLYGONS)
    expected_zones = source_zones.set_geometry(
        prepare_wgs84_geometry(source_zones.geometry.to_numpy(), source_zones.crs), crs=WGS84,
    )
    if any(_feature_differences(expected_zones, zones, "prepared zones")):
        raise ValueError("Prepared zone identities or geometries differ from the staged zones")
    zone_ids = _identifiers(zones, "prepared zones")
    if len(set(zone_ids)) != len(zone_ids):
        raise ValueError("Prepared zones require unique source_feature_id values")
    original_ids = _identifiers(original, "retained original targets")
    if set(original_ids) != target_ids:
        raise ValueError("Retained original target IDs differ from the selected targets")
    tree = shapely.STRtree(zones.geometry.to_numpy())
    members: dict[str, set[str]] = {identifier: set() for identifier in [*zone_ids, ""]}
    for identifier, geometry in zip(original_ids, original.geometry, strict=True):
        covers = tree.query(geometry, predicate="covered_by")
        zone = zone_ids[covers[0]] if len(covers) == 1 else ""
        members[zone].add(identifier)
    return {zone: sorted(ids) for zone, ids in members.items()}


def _point_count_summary(
    context: RunCheckContext, targets: StepFileRef, summary: StepOutput, *, targets_field: str, summary_field: str,
    verify_zone_members: bool = False,
) -> RunCheckResult:
    selected = _read_vector(context, targets, targets_field, _POINTS)
    target_ids = set(_identifiers(selected, "selected targets"))
    path = _file(context, summary.path, summary_field)
    if summary.type != "table_csv" or path.suffix.lower() != ".csv" or summary.layer is not None:
        raise ValueError("Zone statistics requires a CSV file without a layer")

    summary_ids: Counter[str] = Counter()
    count_mismatches = []
    zones: set[str] = set()
    members: dict[str, list[str]] = {}
    assigned_count = 0
    unassigned_count = None
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames is None or len(reader.fieldnames) != len(_SUMMARY_COLUMNS) or set(reader.fieldnames) != _SUMMARY_COLUMNS:
            raise ValueError("Zone statistics CSV has unexpected columns")
        for row_number, row in enumerate(reader, start=2):
            if None in row or None in row.values():
                raise ValueError(f"Zone statistics row {row_number} has the wrong number of fields")
            zone, status, count = row["zone_id"], row["assignment_status"], int(row["count"])
            ids = json.loads(row["source_feature_ids"])
            if count < 0 or not isinstance(ids, list) or any(not isinstance(value, str) or not value.strip() for value in ids):
                raise ValueError(f"Zone statistics row {row_number} must contain a nonnegative count and a list of IDs")
            if count != len(ids):
                count_mismatches.append(row_number)
            if status == "assigned" and zone.strip() and zone not in zones:
                zones.add(zone)
                assigned_count += count
            elif status == "unassigned" and zone == "" and unassigned_count is None:
                unassigned_count = count
            else:
                raise ValueError(f"Zone statistics row {row_number} has a duplicate zone or invalid assignment status")
            summary_ids.update(ids)
            if verify_zone_members:
                members[zone] = sorted(ids)
    if unassigned_count is None:
        raise ValueError("Zone statistics CSV has no unassigned row")

    missing = sorted(target_ids - summary_ids.keys())
    unexpected = sorted(summary_ids.keys() - target_ids)
    duplicates = sorted(identifier for identifier, count in summary_ids.items() if count > 1)
    total = assigned_count + unassigned_count
    passed = not (missing or unexpected or duplicates or count_mismatches) and total == len(target_ids)
    observations = {
        "selected_rows": len(selected), "selected_unique_count": len(target_ids),
        "assigned_count": assigned_count, "unassigned_count": unassigned_count, "summary_total_count": total,
        "missing_ids": missing, "unexpected_ids": unexpected, "duplicate_ids": duplicates, "count_mismatch_rows": count_mismatches,
    }
    if verify_zone_members:
        expected = _geodesic_zone_members(context, target_ids)
        differences = sorted(zone for zone in members.keys() | expected.keys() if members.get(zone) != expected.get(zone))
        observations.update(expected_zone_members=expected, actual_zone_members=members, zone_member_differences=differences)
        if differences:
            return RunCheckResult(False, "CSV target assignments differ from the prepared zones", observations)
    message = "CSV counts and IDs match the selected targets" if passed else "CSV counts or IDs differ from the selected targets"
    return RunCheckResult(passed, message, observations)


RUN_CHECKS: dict[str, RunCheck] = {
    "output_exists": _output_exists,
    "measurement_policy_satisfied": _measurement_policy_satisfied,
    "outside_roads_within_influence_included": _outside_roads_within_influence_included,
    "output_clipped_to_analysis_extent": _output_clipped_to_analysis_extent,
    "detail_count_matches_summary": _detail_count_matches_summary,
}
