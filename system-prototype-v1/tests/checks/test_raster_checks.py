import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
import rasterio
from affine import Affine
from rasterio.windows import Window
from shapely import box

from geopilot.application.run_inputs import stage_run_inputs
from geopilot.artifacts.raster_metadata import read_raster_artifact_metadata
from geopilot.artifacts.service import insert_artifacts, prepare_artifacts
from geopilot.checks.artifact_checks import ArtifactCheckContext, check_run_artifacts
from geopilot.checks.raster_checks import DEM_ARTIFACT_RULES
from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.step_io import StepRequest, StepResult
from geopilot.gis.raster.area_stats import summarize_threshold_area
from geopilot.gis.raster.window_io import copy_raster_window
from geopilot.persistence.run_repository import insert_run, save_run_step, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.plans.models import AnalysisPlan
from geopilot.runs.models import Run, RunStep


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/raster"
DEM = FIXTURES / "dem-plane.tif"
CRS = "EPSG:32650"
AREA_CRS = "EPSG:6933"
TRANSFORM = Affine(10, 0, 500020, 0, -10, 4400050)
BOUNDS = (500020, 4400020, 500050, 4400050)
STAMP = "2026-11-11T00:00:00Z"
RULE_COVERAGE = "dem_covers_expanded_extent"
RULE_UNITS = "units_resolved"
RULE_BORDER = "border_pixels_match_reference"
KEYS = (
    "expand_extent_by_algorithm_neighborhood", "read_dem_for_expanded_extent", "check_horizontal_vertical_units",
    "calculate_slope_or_aspect", "clip_back_to_analysis_extent", "extract_by_threshold",
    "calculate_area_with_equal_area_projection",
)


def _write_raster(path, data, transform, *, nodata, mask=None, unit=None):
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
        dtype=data.dtype, crs=CRS, transform=transform, nodata=nodata,
    ) as dataset:
        dataset.write(data, 1)
        if mask is not None:
            dataset.write_mask(mask)
        if unit is not None:
            dataset.set_band_unit(1, unit)


def test_raster_artifact_metadata_keeps_the_existing_grid_and_exact_per_band_counts():
    path = FIXTURES / "two-band.tif"
    before = fingerprint_file(path)
    metadata = read_raster_artifact_metadata(path)
    base = read_raster_metadata(path).model_dump(mode="json")

    assert {key: metadata[key] for key in base} == base
    assert metadata["common_valid_pixel_count"] == 10
    red, nir = metadata["band_statistics"]
    assert [band["band"] for band in (red, nir)] == [1, 2]
    assert [band["valid_pixel_count"] for band in (red, nir)] == [11, 11]
    assert [band["masked_pixel_count"] for band in (red, nir)] == [1, 1]
    assert (red["minimum"], red["maximum"], nir["minimum"], nir["maximum"]) == (101, 124, 201, 224)
    assert red["mean"] == pytest.approx(1237 / 11)
    assert fingerprint_file(path) == before


def test_statistics_follow_the_effective_mask_and_exact_valid_cell_bounds(tmp_path):
    path = tmp_path / "rotated.tif"
    values = np.array([[10, 20, np.inf], [40, 50, -9999]], dtype="float32")
    mask = np.array([[0, 255, 255], [255, 0, 0]], dtype="uint8")
    _write_raster(path, values, Affine(2, 1, 100, -1, -3, 200), nodata=-9999, mask=mask)
    with rasterio.open(path, "r+") as dataset:
        dataset.scales = (0.5,)
        dataset.offsets = (2.0,)
    before = fingerprint_file(path)

    metadata = read_raster_artifact_metadata(path)
    band, = metadata["band_statistics"]
    assert metadata["common_valid_pixel_count"] == 2
    assert band == {
        "band": 1, "mask_flags": ["per_dataset"], "scale": 0.5, "offset": 2.0,
        "valid_pixel_count": 2, "masked_pixel_count": 3, "nonfinite_pixel_count": 1,
        "valid_bounds": [101.0, 193.0, 105.0, 199.0], "minimum": 20.0, "maximum": 40.0, "mean": 30.0,
    }
    assert fingerprint_file(path) == before


def test_all_nodata_has_no_valid_bounds_or_statistics(tmp_path):
    path = tmp_path / "empty.tif"
    _write_raster(path, np.full((2, 3), 255, dtype="uint8"), TRANSFORM, nodata=255)
    metadata = read_raster_artifact_metadata(path)
    band, = metadata["band_statistics"]
    assert metadata["common_valid_pixel_count"] == band["valid_pixel_count"] == 0
    assert band["masked_pixel_count"] == 6 and band["nonfinite_pixel_count"] == 0
    assert all(band[key] is None for key in ("valid_bounds", "minimum", "maximum", "mean"))


def test_valid_bounds_and_mean_merge_across_windows_without_overflow(tmp_path):
    path = tmp_path / "large-values.tif"
    values = np.full((257, 258), -9999, dtype="float64")
    values[0, 0] = values[-1, -1] = 1.7e308
    _write_raster(path, values, TRANSFORM, nodata=-9999)

    metadata = read_raster_artifact_metadata(path)

    band, = metadata["band_statistics"]
    assert metadata["common_valid_pixel_count"] == band["valid_pixel_count"] == 2
    assert band["masked_pixel_count"] == values.size - 2
    assert band["valid_bounds"] == metadata["bounds"]
    assert band["minimum"] == band["maximum"] == band["mean"] == 1.7e308


def _write_json(path, value):
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


def _dem_plan(inputs):
    threshold = {"intervals": [
        {"class_id": label, "lower": value, "upper": value, "include_lower": True, "include_upper": True}
        for label, value in ((7, 45), (8, 60))
    ]}
    unit_parameters = {"band": 1, "vertical_unit": "metadata", "z_factor": 1.0}
    declarations = (
        ("python-dem-expand-extent", {"dem": {"asset": "dem"}, "analysis_extent": {"asset": "analysis_extent"}},
         {"radius_pixels": 1}, "windows", "json", ".json"),
        ("python-dem-prepare", {"dem": {"asset": "dem"}, "windows": {"step": KEYS[0], "output": "windows"}},
         {}, "prepared_dem", "raster_elevation", ".tif"),
        ("python-dem-unit-check", {"dem": {"step": KEYS[1], "output": "prepared_dem"}}, unit_parameters,
         "unit_report", "json", ".json"),
        ("python-dem-terrain", {"dem": {"step": KEYS[1], "output": "prepared_dem"},
                                "unit_report": {"step": KEYS[2], "output": "unit_report"}},
         {"band": 1, "z_factor": 1.0, "terrain_output": "slope", "slope_unit": "degree"},
         "terrain_raster", "raster_terrain", ".tif"),
        ("python-dem-clip-back", {"terrain_raster": {"step": KEYS[3], "output": "terrain_raster"},
                                  "windows": {"step": KEYS[0], "output": "windows"},
                                  "analysis_extent": {"asset": "analysis_extent"}},
         {}, "terrain_raster", "raster_terrain", ".tif"),
        ("python-dem-threshold", {"terrain_raster": {"step": KEYS[4], "output": "terrain_raster"}},
         {"threshold": threshold}, "threshold_regions", "raster_classification", ".tif"),
        ("python-dem-area-stats", {"threshold_regions": {"step": KEYS[5], "output": "threshold_regions"}},
         {"area_crs": AREA_CRS}, "area_statistics", "json", ".json"),
    )
    steps = [
        {"key": key, "tool": {"id": tool, "version": 1}, "inputs": bindings, "parameters": parameters,
         "outputs": {role: {"type": kind, "target": f"intermediate/{index}-{role}{suffix}"}}}
        for index, (key, (tool, bindings, parameters, role, kind, suffix)) in enumerate(zip(KEYS, declarations, strict=True))
    ]
    return AnalysisPlan.model_validate({
        "id": "dem-check-plan", "version": 1, "task_id": "terrain", "status": "confirmed",
        "goal": {"summary": "Check the independent P0 raster reference"},
        "inputs": inputs, "method": {"id": "dem-terrain-analysis", "version": 1},
        "critical_options": {"terrain_output": "slope", "slope_unit": "degree", "threshold": threshold},
        "steps": steps, "issues": [], "check_rules": [RULE_COVERAGE, RULE_UNITS, RULE_BORDER],
        "expected_artifacts": [{"role": role, "step": step["key"], "output": role, "type": output["type"]}
                               for step in steps[-3:] for role, output in step["outputs"].items()],
    })


@pytest.fixture
def dem_case(tmp_path):
    database = tmp_path / "dem.sqlite"
    initialize_database(database)
    extent = tmp_path / "extent.gpkg"
    pyogrio.write_dataframe(gpd.GeoDataFrame(geometry=[box(*BOUNDS)], crs=CRS), extent, layer="analysis_extent")
    assets = {"dem": register_asset(database, DEM), "analysis_extent": register_asset(database, extent, layer="analysis_extent")}
    plan = _dem_plan({role: asset.ref for role, asset in assets.items()})
    workspace = create_run_workspace(tmp_path / "workspace", "terrain", "terrain-run")
    staged = stage_run_inputs(database, plan.inputs, workspace.path)
    _write_json(workspace.request_path, {"run_id": "terrain-run", "plan": plan.model_dump(mode="json"),
                "asset_files": {role: reference.model_dump(mode="json") for role, reference in staged.items()}})

    paths = {step.key: workspace.path / next(iter(step.outputs.values())).target for step in plan.steps}
    dem = workspace.path / staged["dem"].path
    _write_json(paths[KEYS[0]], {
        "source": {"path": staged["dem"].path, "metadata_fingerprint": fingerprint_file(dem, strategy="metadata").digest,
                   "grid": read_raster_metadata(dem).model_dump(mode="json")},
        "analysis_extent": staged["analysis_extent"].model_dump(mode="json"), "radius_pixels": 1,
        "requested_bounds": list(map(float, BOUNDS)), "analysis_bounds": list(map(float, BOUNDS)),
        "analysis_window": {"col_offset": 2, "row_offset": 2, "width": 3, "height": 3},
        "expanded_bounds": [500010.0, 4400010.0, 500060.0, 4400060.0],
        "expanded_window": {"col_offset": 1, "row_offset": 1, "width": 5, "height": 5},
    })
    copy_raster_window(dem, paths[KEYS[1]], Window(1, 1, 5, 5))
    units = {"band": 1, "horizontal_unit": "metre", "vertical_unit": "meter", "horizontal_metres_per_unit": 1.0,
             "vertical_metres_per_unit": 1.0, "z_factor": 1.0, "vertical_source": "band"}
    _write_json(paths[KEYS[2]], units)
    expanded = np.full((5, 5), -9999, dtype="float32")
    expanded[1:4, 1:4] = 45
    _write_raster(paths[KEYS[3]], expanded, Affine(10, 0, 500010, 0, -10, 4400060), nodata=-9999, unit="degree")
    _write_raster(paths[KEYS[4]], np.full((3, 3), 45, dtype="float32"), TRANSFORM, nodata=-9999, unit="degree")
    _write_raster(paths[KEYS[5]], np.full((3, 3), 7, dtype="uint8"), TRANSFORM, nodata=255, unit="class")
    _write_json(paths[KEYS[6]], summarize_threshold_area(paths[KEYS[5]], area_crs=AREA_CRS))

    outputs = {}
    results = {}
    for step in plan.steps:
        role, output = next(iter(step.outputs.items()))
        inputs = {
            name: staged[binding.asset] if hasattr(binding, "asset") else outputs[binding.step][binding.output]
            for name, binding in step.inputs.items()
        }
        directory = workspace.path / "steps" / step.key
        directory.mkdir()
        log = directory / "process.log"
        if step.key == KEYS[2]:
            log.write_text("measurement: " + json.dumps(units) + "\n", encoding="utf-8")
        elif step.key == KEYS[3]:
            log.write_text('terrain: {"algorithm": "Horn", "algorithm_version": 1, "source_band": 1, "z_factor": 1.0}\n',
                           encoding="utf-8")
        else:
            log.touch()
        relative_log = log.relative_to(workspace.path).as_posix()
        request = StepRequest(run_id="terrain-run", step_key=step.key, tool=step.tool, workdir_ref="terrain-run",
                              inputs=inputs, parameters=step.parameters,
                              output_targets={role: {"path": output.target}}, log_path=relative_log, cancel_path="cancel.flag")
        _write_json(directory / "request.json", request.model_dump(mode="json"))
        result = StepResult(run_id="terrain-run", step_key=step.key, status="SUCCEEDED", exit_code=0,
                            outputs={role: {"path": output.target, "type": output.type, "layer": None}},
                            log_path=relative_log, warnings=[], error=None, started_at=STAMP, finished_at=STAMP, environment={})
        outputs[step.key] = request.output_targets
        results[step.key] = result

    prepared = prepare_artifacts("terrain-run", plan, workspace.path, results)
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES ('terrain', ?, ?)",
                           (plan.goal.model_dump_json(), STAMP))
        connection.execute("""INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at)
                              VALUES (?, 1, 'terrain', 'confirmed', ?, ?)""", (plan.id, plan.model_dump_json(), STAMP))
        run = insert_run(connection, Run(id="terrain-run", plan_id=plan.id, plan_version=1, workdir=str(workspace.path),
                                         created_at=STAMP, execution_status="RUNNING"))
        for position, result in enumerate(results.values()):
            save_run_step(connection, RunStep(run_id=run.id, step_key=result.step_key, position=position,
                                              status=result.status, result=result))
        records = insert_artifacts(connection, prepared)
        update_run(connection, run.model_copy(update={"execution_status": "SUCCEEDED", "check_status": "CHECKING"}))
    context = ArtifactCheckContext("terrain-run", workspace.path, plan, {record.role: record for record in records}, results, assets)
    return database, context, paths


def test_registered_dem_rules_use_real_files_and_distinguish_edge_from_interior(dem_case):
    database, context, _ = dem_case
    evidence = check_run_artifacts(database, context.run_id, rules=DEM_ARTIFACT_RULES)

    assert set(DEM_ARTIFACT_RULES) == {RULE_COVERAGE, RULE_UNITS, RULE_BORDER}
    assert {item.rule for item in evidence} == {"artifact_integrity", *DEM_ARTIFACT_RULES}
    assert all(item.passed is True for item in evidence), evidence
    pixels, = [item for item in evidence if item.field == "artifacts.terrain_raster.pixels"]
    assert pixels.observed["edge"] == {"common_valid_pixels": 8, "mask_mismatches": 0, "value_mismatches": 0}
    assert pixels.observed["interior"] == {"common_valid_pixels": 1, "mask_mismatches": 0, "value_mismatches": 0}
    terrain = context.artifacts["terrain_raster"].metadata
    assert terrain["width"] == terrain["height"] == 3 and terrain["transform"] == list(TRANSFORM)[:6]
    assert terrain["nodata"] == [-9999.0] and terrain["band_units"] == ["degree"]
    assert terrain["band_statistics"][0]["valid_pixel_count"] == 9
    assert terrain["band_statistics"][0]["valid_bounds"] == list(BOUNDS)


@pytest.mark.parametrize(("key", "field", "value", "rule", "expected_field"), [
    (KEYS[0], "radius_pixels", 0, RULE_COVERAGE, "steps.expand_extent_by_algorithm_neighborhood.windows"),
    (KEYS[2], "z_factor", 2, RULE_UNITS, "steps.check_horizontal_vertical_units.unit_report"),
    (KEYS[6], "selected_pixel_count", 8, RULE_BORDER, "artifacts.area_statistics"),
])
def test_rule_fails_when_readable_json_disagrees_with_the_confirmed_inputs(dem_case, key, field, value, rule, expected_field):
    _, context, paths = dem_case
    document = json.loads(paths[key].read_text(encoding="utf-8"))
    _write_json(paths[key], {**document, field: value})

    evidence = DEM_ARTIFACT_RULES[rule](context)

    assert any(item.passed is False and item.field == expected_field for item in evidence), evidence


@pytest.mark.parametrize(("value", "mismatch"), [(46, "value_mismatches"), (-9999, "mask_mismatches")])
def test_raster_rule_rejects_a_wrong_edge_value_or_mask(dem_case, value, mismatch):
    _, context, paths = dem_case
    with rasterio.open(paths[KEYS[4]], "r+") as terrain:
        terrain.write(np.array([[value]], dtype="float32"), 1, window=Window(0, 0, 1, 1))

    evidence = DEM_ARTIFACT_RULES[RULE_BORDER](context)

    pixels, = [item for item in evidence if item.field == "artifacts.terrain_raster.pixels"]
    assert pixels.passed is False
    assert pixels.observed["edge"][mismatch] == 1 and pixels.observed["interior"][mismatch] == 0


def test_area_rule_rejects_a_non_nodata_value_hidden_by_a_mask(dem_case):
    _, context, paths = dem_case
    with rasterio.open(paths[KEYS[4]], "r+") as terrain:
        terrain.write(np.array([[-9999]], dtype="float32"), 1, window=Window(0, 0, 1, 1))
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(paths[KEYS[5]], "r+") as classified:
        mask = np.full((3, 3), 255, dtype="uint8")
        mask[0, 0] = 0
        classified.write_mask(mask)
    _write_json(paths[KEYS[6]], summarize_threshold_area(paths[KEYS[5]], area_crs=AREA_CRS))

    evidence = DEM_ARTIFACT_RULES[RULE_BORDER](context)

    area, = [item for item in evidence if item.field == "artifacts.area_statistics"]
    assert area.passed is False and area.observed["mask_mismatches"] == 1


def test_area_rule_rejects_two_allowed_classes_swapped_with_the_same_counts(dem_case):
    _, context, paths = dem_case
    with rasterio.open(paths[KEYS[4]], "r+") as terrain:
        terrain.write(np.array([[45, 60]], dtype="float32"), 1, window=Window(0, 0, 2, 1))
    with rasterio.open(paths[KEYS[5]], "r+") as classified:
        classified.write(np.array([[7, 8]], dtype="uint8"), 1, window=Window(0, 0, 2, 1))
    original = summarize_threshold_area(paths[KEYS[5]], area_crs=AREA_CRS)
    _write_json(paths[KEYS[6]], original)
    correct, = [item for item in DEM_ARTIFACT_RULES[RULE_BORDER](context) if item.field == "artifacts.area_statistics"]
    assert correct.passed is True

    with rasterio.open(paths[KEYS[5]], "r+") as classified:
        classified.write(np.array([[8, 7]], dtype="uint8"), 1, window=Window(0, 0, 2, 1))
    swapped = summarize_threshold_area(paths[KEYS[5]], area_crs=AREA_CRS)
    _write_json(paths[KEYS[6]], swapped)
    assert [(row["class_id"], row["pixel_count"]) for row in original["classes"]] == [
        (row["class_id"], row["pixel_count"]) for row in swapped["classes"]
    ] == [(7, 8), (8, 1)]

    wrong, = [item for item in DEM_ARTIFACT_RULES[RULE_BORDER](context) if item.field == "artifacts.area_statistics"]
    assert wrong.passed is False and wrong.observed["class_value_mismatches"] == 2
