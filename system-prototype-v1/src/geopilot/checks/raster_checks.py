"""Verify the DEM method's recorded windows, units, pixels and class totals."""

import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from functools import partial
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from pydantic import JsonValue
from pyproj import CRS
from rasterio.windows import Window, bounds as window_bounds, transform as window_transform

from geopilot.artifacts.raster_metadata import raster_tiles
from geopilot.artifacts.service import _file_state
from geopilot.checks.artifact_checks import (
    ArtifactCheckContext, ArtifactCheckResult, ArtifactRule, NumericTolerance, _DATA_ERRORS, _finding, _grid,
    _staged_reference, compare_raster_pixels,
)
from geopilot.checks.process_checks import _json
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.step_io import ExecutionStatus, StepFileRef, StepRequest
from geopilot.execution.step_support import resolve_step_path
from geopilot.gis.raster.aspect import aspect_from_gradients
from geopilot.gis.raster.clip_back import analysis_scope, pixel_centers_covered, read_analysis_polygons
from geopilot.gis.raster.dem_prepare import _verify_crop, _windows_record
from geopilot.gis.raster.extent_expansion import expand_analysis_window
from geopilot.gis.raster.terrain import (
    HORN_ALGORITHM_VERSION, HORN_RADIUS_PIXELS, TERRAIN_NODATA, slope_from_gradients, terrain_gradient_scales,
    terrain_tile_values,
)
from geopilot.gis.raster.threshold import BACKGROUND_CLASS, NODATA_CLASS, ThresholdSpec, threshold_classifier
from geopilot.gis.raster.units import resolve_dem_units
from geopilot.plans.compiler import _same_json
from geopilot.plans.models import PlanStep


_EXPAND = "expand_extent_by_algorithm_neighborhood"
_PREPARE = "read_dem_for_expanded_extent"
_UNITS = "check_horizontal_vertical_units"
_TERRAIN = "calculate_slope_or_aspect"
_THRESHOLD = "extract_by_threshold"
_AREA = "calculate_area_with_equal_area_projection"
_COVERAGE_RULE = "dem_covers_expanded_extent"
_UNIT_RULE = "units_resolved"
_BORDER_RULE = "border_pixels_match_reference"
_COVERAGE_FIELD = f"steps.{_EXPAND}.windows"
_PREPARED_FIELD = f"steps.{_PREPARE}.prepared_dem.grid"
_UNIT_FIELD = f"steps.{_UNITS}.unit_report"
_TERRAIN_FIELD = "artifacts.terrain_raster"
_AREA_FIELD = "artifacts.area_statistics"
_TOLERANCE = NumericTolerance(absolute=0, relative=0, source="Horn v1 stored Float32 values on the original DEM grid")
_READ_ERRORS = (*_DATA_ERRORS, TypeError, OverflowError)


def _step(context: ArtifactCheckContext, key: str) -> PlanStep:
    step = next((step for step in context.plan.steps if step.key == key), None)
    if step is None:
        raise ValueError(f"The confirmed DEM plan requires step {key!r}")
    return step


@contextmanager
def _step_file(context: ArtifactCheckContext, key: str, role: str) -> Iterator[tuple[Path, StepFileRef]]:
    result = context.steps[key]
    declared = _step(context, key).outputs[role]
    output = result.outputs[role]
    if (
        (result.run_id, result.step_key, result.status) != (context.run_id, key, ExecutionStatus.SUCCEEDED)
        or (output.path, output.layer, output.type) != (declared.target, declared.layer, declared.type)
    ):
        raise ValueError(f"Step {key!r} does not provide its confirmed {role!r} output")
    field = f"steps.{key}.{role}"
    path, before = _file_state(context.workdir, output.path, field, key)
    yield path, StepFileRef(path=output.path, layer=output.layer)
    _, after = _file_state(context.workdir, output.path, field, key)
    if before != after:
        raise ValueError(f"Step output {field!r} changed during the check")


def _object(text: str) -> dict[str, JsonValue]:
    value = _json(text)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


@contextmanager
def _dem_inputs(context: ArtifactCheckContext):
    with _staged_reference(context, "dem") as (path, dem), _staged_reference(context, "analysis_extent") as (extent_path, extent):
        grid = read_raster_metadata(path)
        if grid.crs is None:
            raise ValueError("The staged DEM requires a known CRS")
        polygons = read_analysis_polygons({"path": str(extent_path), "layer": extent.layer}, grid.crs)
        radius = _step(context, _EXPAND).parameters["radius_pixels"]
        if type(radius) is not int or radius != HORN_RADIUS_PIXELS:
            raise ValueError(f"Horn requires a {HORN_RADIUS_PIXELS}-pixel source neighborhood")
        windows = expand_analysis_window(grid, tuple(polygons.total_bounds), radius)
        yield path, dem, extent, grid, polygons, windows


def _check_coverage(context: ArtifactCheckContext) -> tuple[ArtifactCheckResult, ...]:
    field = _COVERAGE_FIELD
    findings = []
    try:
        with _dem_inputs(context) as (dem_path, dem, extent, grid, _, windows):
            with _step_file(context, _EXPAND, "windows") as (window_path, _):
                observed = _object(window_path.read_text(encoding="utf-8"))
                source = {"path": dem.path, "metadata_fingerprint": fingerprint_file(dem_path, strategy="metadata").digest,
                          "grid": grid.model_dump(mode="json")}
                expected = _windows_record(source, extent, HORN_RADIUS_PIXELS, windows)
                matches = _same_json(observed, expected)
                findings.append(_finding(_COVERAGE_RULE, field, matches, observed, expected,
                                         "Compare the recorded neighborhood with the staged DEM and analysis polygons"))
            if matches:
                field = _PREPARED_FIELD
                with _step_file(context, _PREPARE, "prepared_dem") as (prepared, _):
                    cropped = read_raster_metadata(prepared)
                    _verify_crop(grid, cropped, windows.expanded_window)
                    findings.append(_finding(_COVERAGE_RULE, field, True, cropped.model_dump(mode="json"),
                                             expected["expanded_window"], "Reopened the prepared DEM on the expected grid"))
    except _READ_ERRORS as error:
        findings.append(_finding(_COVERAGE_RULE, field, False, None, "verified neighborhood and prepared grid", str(error)))
    return tuple(findings)


def _request_and_log(context: ArtifactCheckContext, key: str, prefix: str) -> tuple[StepRequest, dict[str, JsonValue]]:
    request_path = resolve_step_path(context.workdir, f"steps/{key}/request.json", field=f"steps.{key}.request")
    request = StepRequest.model_validate(_object(request_path.read_text(encoding="utf-8")))
    result = context.steps[key]
    step = _step(context, key)
    if (request.run_id, request.step_key, request.tool, request.log_path) != (context.run_id, key, step.tool, result.log_path):
        raise ValueError(f"Request and log evidence for {key!r} do not match this run")
    if not _same_json(request.parameters, step.parameters):
        raise ValueError(f"Step {key!r} parameters differ from the confirmed plan")
    log = resolve_step_path(context.workdir, request.log_path, field=f"steps.{key}.log_path")
    records = [line.removeprefix(prefix) for line in log.read_text(encoding="utf-8").splitlines() if line.startswith(prefix)]
    if len(records) != 1:
        raise ValueError(f"Step {key!r} must provide exactly one {prefix.strip()} log record")
    return request, _object(records[0])


def _check_units(context: ArtifactCheckContext) -> tuple[ArtifactCheckResult, ...]:
    expected = "matching unit resolution, confirmed parameters and process logs"
    try:
        with (
            _step_file(context, _PREPARE, "prepared_dem") as (prepared, dem_reference),
            _step_file(context, _UNITS, "unit_report") as (report_path, report_reference),
        ):
            unit_request, unit_log = _request_and_log(context, _UNITS, "measurement: ")
            terrain_request, terrain_log = _request_and_log(context, _TERRAIN, "terrain: ")
            if unit_request.inputs["dem"] != dem_reference or terrain_request.inputs != {
                "dem": dem_reference, "unit_report": report_reference,
            }:
                raise ValueError("Unit and terrain requests must reference the same prepared DEM and unit report")
            band = unit_request.parameters["band"]
            selection = unit_request.parameters["vertical_unit"]
            units = resolve_dem_units(read_raster_metadata(prepared), band=band,
                                      vertical_unit=None if selection == "metadata" else selection)
            expected_report = {"band": band, **asdict(units)}
            report = _object(report_path.read_text(encoding="utf-8"))
            confirmed_factors = [request.parameters["z_factor"] for request in (unit_request, terrain_request)]
            observed = {"report": report, "unit_log": unit_log, "confirmed_factors": confirmed_factors,
                        "terrain_log_factor": terrain_log.get("z_factor")}
            matches = (
                _same_json(report, expected_report) and _same_json(unit_log, expected_report)
                and all(type(value) in (float, int) and math.isclose(value, units.z_factor, rel_tol=1e-12)
                        for value in (*confirmed_factors, terrain_log.get("z_factor")))
                and terrain_request.parameters["band"] == band and terrain_log.get("source_band", band) == band
                and terrain_log.get("algorithm") == "Horn" and terrain_log.get("algorithm_version") == HORN_ALGORITHM_VERSION
            )
    except _READ_ERRORS as error:
        return (_finding(_UNIT_RULE, _UNIT_FIELD, False, None, expected, str(error)),)
    return (_finding(_UNIT_RULE, _UNIT_FIELD, matches, observed, expected_report,
                     "Compare resolved DEM units with the report, requests and process logs"),)


def _artifact_path(context: ArtifactCheckContext, role: str, kind: str) -> Path:
    artifact = context.artifacts[role]
    if artifact.kind != kind:
        raise ValueError(f"Artifact {role!r} must be {kind}")
    return resolve_step_path(context.workdir, artifact.uri, field=f"artifacts.{role}.path")


def _terrain_findings(context: ArtifactCheckContext) -> tuple[ArtifactCheckResult, ...]:
    pixel_field = f"{_TERRAIN_FIELD}.pixels"
    expected_pixels = {"mask_mismatches": 0, "value_mismatches": 0, "tolerance": _TOLERANCE.model_dump(mode="json")}
    try:
        with _dem_inputs(context) as (path, _, _, grid, polygons, windows):
            analysis = windows.analysis_window
            parameters = _step(context, _TERRAIN).parameters
            kind = parameters["terrain_output"]
            if kind not in {"slope", "aspect"}:
                raise ValueError("Expected confirmed slope or aspect output")
            unit = "degree" if kind == "aspect" else parameters["slope_unit"]
            if unit not in {"degree", "percent"}:
                raise ValueError("Expected confirmed degree or percent terrain units")
            derive = aspect_from_gradients if kind == "aspect" else partial(slope_from_gradients, unit=unit)
            terrain_path = _artifact_path(context, "terrain_raster", "raster")
            with rasterio.open(terrain_path, driver="GTiff") as actual, rasterio.open(path, driver="GTiff") as dem:
                transform = dem.transform
                expected_grid = {
                    "crs": grid.crs, "width": int(analysis.width), "height": int(analysis.height), "band_count": 1,
                    "transform": list(window_transform(analysis, transform))[:6], "resolution": list(grid.resolution),
                    "bounds": list(window_bounds(analysis, transform)), "dtypes": ["float32"],
                    "nodata": [TERRAIN_NODATA], "units": [unit],
                }
                observed_grid = {**_grid(actual), "bounds": list(actual.bounds)}
                grid_matches = actual.crs is not None and CRS(actual.crs).equals(CRS(grid.crs), ignore_axis_order=True)
                grid_matches &= all(observed_grid[key] == value for key, value in expected_grid.items() if key != "crs")
                grid_finding = _finding(_BORDER_RULE, f"{_TERRAIN_FIELD}.grid", grid_matches, observed_grid, expected_grid,
                                        "Compare the terrain grid with the original DEM analysis window")
                if not grid_matches:
                    return grid_finding, _finding(_BORDER_RULE, pixel_field, None, None, expected_pixels,
                                                  "Pixel comparison requires the original analysis grid")
                band = parameters["band"]
                east, north = terrain_gradient_scales(dem, band, parameters["z_factor"])
                scope = analysis_scope(polygons)

                def reference(tile: Window) -> np.ma.MaskedArray:
                    original = Window(analysis.col_off + tile.col_off, analysis.row_off + tile.row_off, tile.width, tile.height)
                    values = terrain_tile_values(dem, original, band, east, north, derive)
                    invalid = (values == TERRAIN_NODATA) | ~pixel_centers_covered(scope, transform, original)
                    return np.ma.array(values[np.newaxis], mask=invalid[np.newaxis])

                pixels = compare_raster_pixels(actual, raster_tiles(actual.width, actual.height), reference, _TOLERANCE)
    except _READ_ERRORS as error:
        return (_finding(_BORDER_RULE, pixel_field, False, None, expected_pixels, str(error)),)
    matches = pixels["mask_mismatches"] == pixels["value_mismatches"] == 0
    return grid_finding, _finding(_BORDER_RULE, pixel_field, matches, pixels, expected_pixels,
                                  "Compare terrain masks and values with neighborhoods from the original staged DEM")


def _check_classification(
    terrain: rasterio.DatasetReader, classes: rasterio.DatasetReader, threshold: ThresholdSpec,
) -> tuple[np.ndarray, int, int]:
    if (
        classes.count != 1 or classes.dtypes != ("uint8",) or classes.nodata != NODATA_CLASS
        or terrain.count != 1 or terrain.crs is None or classes.crs is None
        or not CRS(terrain.crs).equals(CRS(classes.crs), ignore_axis_order=True)
        or (terrain.transform, terrain.shape) != (classes.transform, classes.shape)
    ):
        raise ValueError("Threshold regions must be uint8 with NoData 255 on the exact terrain grid")
    counts = np.zeros(NODATA_CLASS + 1, dtype=np.int64)
    mask_mismatches = 0
    value_mismatches = 0
    classify = threshold_classifier(terrain, threshold)
    for tile in raster_tiles(classes.width, classes.height):
        pixels = classes.read(1, window=tile, masked=True)
        expected = classify(terrain.read(1, window=tile, masked=True))
        masked = np.ma.getmaskarray(pixels)
        nodata = pixels.data == NODATA_CLASS
        invalid = masked | nodata
        expected_invalid = expected == NODATA_CLASS
        mask_mismatches += int(np.count_nonzero((masked != nodata) | (nodata != expected_invalid)))
        value_mismatches += int(np.count_nonzero(~invalid & ~expected_invalid & (pixels.data != expected)))
        counts += np.bincount(pixels.data[~invalid], minlength=NODATA_CLASS + 1)
    return counts, mask_mismatches, value_mismatches


def _area_totals_match(report: dict[str, JsonValue], classes: list, determinant: float | None) -> bool:
    areas = [row.get("area_m2") for row in classes]
    total = report.get("total_area_m2")
    if (
        any(type(area) not in (int, float) or not math.isfinite(area) or area <= 0 for area in areas)
        or type(total) not in (int, float) or total != math.fsum(areas)
    ):
        return False
    return determinant is None or all(row["area_m2"] == row["pixel_count"] * determinant for row in classes)


def _check_area(context: ArtifactCheckContext) -> ArtifactCheckResult:
    expected = "threshold mask, counts, class areas and units consistent with the registered raster"
    try:
        terrain_path = _artifact_path(context, "terrain_raster", "raster")
        threshold_path = _artifact_path(context, "threshold_regions", "raster")
        area_path = _artifact_path(context, "area_statistics", "json")
        area = _step(context, _AREA).parameters
        target = CRS.from_user_input(area["area_crs"]).to_2d()
        threshold = ThresholdSpec.model_validate(_step(context, _THRESHOLD).parameters["threshold"])
        allowed_classes = {interval.class_id for interval in threshold.intervals}
        source = read_raster_metadata(threshold_path)
        same_crs = CRS(source.crs).to_2d().equals(target, ignore_axis_order=True)
        with rasterio.open(terrain_path, driver="GTiff") as terrain, rasterio.open(threshold_path, driver="GTiff") as classes:
            counts, mask_mismatches, class_mismatches = _check_classification(terrain, classes, threshold)
            determinant = abs(classes.transform.determinant) if same_crs else None
        class_ids = np.flatnonzero(counts[1:NODATA_CLASS]) + 1
        background = int(counts[BACKGROUND_CLASS])
        valid = int(counts.sum())
        expected = {
            "source_crs": source.crs, "area_crs": target.to_string(), "area_method": "equal_area", "area_unit": "square_meter",
            "cell_area_method": "affine_determinant" if same_crs else "projected_cell_boundary", "resampling": "none",
            "valid_pixel_count": valid, "selected_pixel_count": valid - background, "unselected_pixel_count": background,
            "nodata_pixel_count": source.width * source.height - valid,
            "classes": [{"class_id": int(label), "pixel_count": int(counts[label])} for label in class_ids],
            "mask_mismatches": 0, "class_value_mismatches": 0, "unexpected_class_ids": [],
        }
        report = _object(area_path.read_text(encoding="utf-8"))
        rows = report.get("classes")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Area statistics require a list of class records")
        observed = {key: report.get(key) for key in expected}
        observed.update({"classes": [{"class_id": row.get("class_id"), "pixel_count": row.get("pixel_count")} for row in rows],
                         "mask_mismatches": mask_mismatches, "class_value_mismatches": class_mismatches,
                         "unexpected_class_ids": sorted(set(map(int, class_ids)) - allowed_classes)})
        matches = _same_json(observed, expected) and _area_totals_match(report, rows, determinant)
        observed["class_areas_m2"] = [row.get("area_m2") for row in rows]
        observed["total_area_m2"] = report.get("total_area_m2")
    except _READ_ERRORS as error:
        return _finding(_BORDER_RULE, _AREA_FIELD, False, None, expected, str(error))
    return _finding(_BORDER_RULE, _AREA_FIELD, matches, observed, expected,
                    "Reconcile threshold pixel values and masks with terrain and verify class counts and area totals")


def _check_border(context: ArtifactCheckContext) -> tuple[ArtifactCheckResult, ...]:
    return (*_terrain_findings(context), _check_area(context))


DEM_ARTIFACT_RULES: Mapping[str, ArtifactRule] = {
    _COVERAGE_RULE: _check_coverage,
    _UNIT_RULE: _check_units,
    _BORDER_RULE: _check_border,
}
