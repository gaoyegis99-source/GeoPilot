import json
import shutil
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
import rasterio
import yaml
from rasterio.transform import Affine
from rasterio.windows import Window
from shapely.geometry import MultiPolygon, Polygon, box

from geopilot.application.plan_runner import run_confirmed_plan
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.application.task_context import build_task_context
from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepFileRef, StepResult
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.gis.raster.clip_back import clip_terrain_to_analysis_extent
from geopilot.gis.raster.dem_prepare import plan_dem_windows, prepare_dem
from geopilot.gis.raster.terrain import write_slope_raster
from geopilot.gis.raster.window_io import copy_raster_window
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, load_plan, save_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "tests/fixtures/raster/dem-plane.tif"
CRS = "EPSG:32650"
ANALYSIS = (500020, 4400020, 500050, 4400050)
GRID = Affine(10, 0, 500000, 0, -10, 4400090)
KEYS = (
    "expand_extent_by_algorithm_neighborhood", "read_dem_for_expanded_extent", "check_horizontal_vertical_units",
    "calculate_slope_or_aspect", "clip_back_to_analysis_extent",
)
TOOL_IDS = ("python-dem-expand-extent", "python-dem-prepare", "python-dem-unit-check", "python-dem-slope", "python-dem-clip-back")


def _step(index, inputs, output_role, output_type, **parameters):
    return {
        "key": KEYS[index], "tool": {"id": TOOL_IDS[index], "version": 1},
        "inputs": inputs, "outputs": {output_role: output_type},
        "parameters": {name: {"literal": value} for name, value in parameters.items()},
    }


def _method_definition():
    dem = {"asset": "dem"}
    extent = {"asset": "analysis_extent"}
    windows = {"step": KEYS[0], "output": "windows"}
    prepared = {"step": KEYS[1], "output": "prepared_dem"}
    units = {"step": KEYS[2], "output": "unit_report"}
    terrain = {"step": KEYS[3], "output": "terrain_raster"}
    return {
        "id": "t09-dem-terrain", "version": 1, "locked_order": True, "critical_options": {},
        "inputs": {"analysis_extent": "vector_polygon", "dem": "raster_elevation"},
        "steps": [
            _step(0, {"analysis_extent": extent, "dem": dem}, "windows", "json", radius_pixels=1),
            _step(1, {"dem": dem, "windows": windows}, "prepared_dem", "raster_elevation"),
            _step(2, {"dem": prepared}, "unit_report", "json", band=1, z_factor=1.0, vertical_unit="metadata"),
            _step(3, {"dem": prepared, "unit_report": units}, "terrain_raster", "raster_terrain",
                  band=1, z_factor=1.0, slope_unit="degree"),
            _step(4, {"terrain_raster": terrain, "windows": windows, "analysis_extent": extent}, "terrain_raster", "raster_terrain"),
        ],
        "expected_artifacts": [{"role": "terrain_raster", "step": KEYS[4], "output": "terrain_raster", "type": "raster_terrain"}],
        "check_rules": ["output_exists"],
    }


def _write_extent(path, geometry, *, layer="analysis_extent"):
    pyogrio.write_dataframe(gpd.GeoDataFrame(geometry=[geometry], crs=CRS), path, layer=layer)


def test_t09_runs_real_confirmed_five_step_plan_and_recovers_all_analysis_edges(tmp_path):
    dem, extent = tmp_path / "dem.tif", tmp_path / "extent.gpkg"
    shutil.copy2(GOLDEN, dem)
    _write_extent(extent, box(*ANALYSIS))
    before = {source: fingerprint_file(source) for source in (dem, extent)}
    catalog_dir = tmp_path / "methods"
    catalog_dir.mkdir()
    (catalog_dir / "t09-dem-terrain.v1.yaml").write_text(yaml.safe_dump(_method_definition(), sort_keys=False))
    catalog = load_method_catalog(catalog_dir)
    assert not catalog.issues
    method = catalog.get("t09-dem-terrain", 1)
    contracts = [
        ToolContract.model_validate(yaml.safe_load((ROOT / f"config/tools/{tool_id}.yaml").read_text()))
        for tool_id in TOOL_IDS
    ]
    implementations = {
        tool.implementation: ROOT / f"src/geopilot/gis/tools/{tool.implementation.rsplit('.', 1)[-1]}.py"
        for tool in contracts
    }
    registry = ToolRegistry(python_implementations=implementations)
    for tool in contracts:
        registry.register(tool)
    database = tmp_path / "terrain.sqlite"
    initialize_database(database)
    context = build_task_context(
        database, task_id="t09", goal=TaskGoal(summary="Measure the DEM analysis boundary"), method=method, registry=registry,
        selected={
            "dem": register_asset(database, dem).ref,
            "analysis_extent": register_asset(database, extent, layer="analysis_extent").ref,
        },
    )
    draft = build_draft(context, method, plan_id="t09-plan")
    assert not draft.issues, draft.issues
    save_draft(database, draft, context=context)
    confirmed = confirm_plan(database, draft.id, draft.version, catalog=catalog, registry=registry, registered_rules=RUN_CHECKS)
    assert confirmed.status == "confirmed" and load_plan(database, confirmed.id, confirmed.version) == confirmed

    run = run_confirmed_plan(
        database, confirmed.id, confirmed.version, workspace_root=tmp_path / "workspace", run_id="t09-run",
        registry=registry, python_implementations=implementations, input_preflight=preflight_step_inputs, check_rules=RUN_CHECKS,
    )

    assert run.execution_status == "SUCCEEDED" and run.check_status == "PASSED", run
    assert tuple(run.steps) == KEYS and set(run.artifacts) == {"terrain_raster"}
    for key, result in run.steps.items():
        assert result.status == "SUCCEEDED" and result.exit_code == 0
        assert StepResult.model_validate_json((run.workdir / "steps" / key / "result.json").read_text()) == result
    source_path = run.workdir / run.steps[KEYS[3]].outputs["terrain_raster"].path
    crop_path = run.workdir / run.artifacts["terrain_raster"].path
    with rasterio.open(source_path) as expanded, rasterio.open(crop_path) as actual:
        assert expanded.shape == (5, 5) and expanded.read(1, masked=True).count() == 9
        assert actual.shape == (3, 3) and actual.crs.to_string() == CRS
        assert actual.transform == Affine(10, 0, 500020, 0, -10, 4400050)
        assert tuple(actual.bounds) == ANALYSIS
        assert actual.units == ("degree",) and actual.nodata == -9999
        assert actual.tags() == expanded.tags()
        observed = actual.read(1, masked=True)
        np.testing.assert_array_equal(observed.data, expanded.read(1)[1:4, 1:4])
    border = np.ones((3, 3), dtype=bool)
    border[1, 1] = False
    assert observed.count() == 9
    np.testing.assert_allclose(observed[border], 45, rtol=0, atol=1e-5)
    assert observed[1, 1] == pytest.approx(45, abs=1e-5)
    log = (run.workdir / run.steps[KEYS[4]].log_path).read_text()
    evidence = json.loads(next(line.removeprefix("terrain_clip: ") for line in log.splitlines() if line.startswith("terrain_clip: ")))
    assert evidence["coverage"] == "pixel_center_covers" and evidence["resampling"] == "none"
    assert evidence["analysis_bounds"] == list(ANALYSIS)

    naive_dem, naive_slope = tmp_path / "direct-crop-dem.tif", tmp_path / "direct-crop-slope.tif"
    copy_raster_window(dem, naive_dem, Window(2, 2, 3, 3))
    write_slope_raster(naive_dem, naive_slope, band=1, z_factor=1.0, slope_unit="degree")
    with rasterio.open(naive_slope) as dataset:
        naive = dataset.read(1, masked=True)
        np.testing.assert_array_equal(np.ma.getmaskarray(naive), border)
        assert naive[1, 1] == pytest.approx(45, abs=1e-5)
    assert before == {source: fingerprint_file(source) for source in before}


@pytest.fixture
def polygon_case(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rows, cols = np.indices((9, 9))
    values = (rows**2 + 4 * cols).astype("float32")
    values[1, 1] = -9999
    with rasterio.open(
        "dem.tif", "w", driver="GTiff", width=9, height=9, count=1, dtype="float32", crs=CRS, transform=GRID, nodata=-9999,
    ) as dem:
        dem.write(values, 1)
        dem.set_band_unit(1, "m")
    shell = box(500010, 4400030, 500035, 4400080)
    hole = box(500021, 4400051, 500029, 4400059)
    right = box(500055, 4400030, 500070, 4400045)
    geometry = MultiPolygon([Polygon(shell.exterior.coords, [hole.exterior.coords]), right])
    _write_extent("extent.gpkg", geometry)
    _write_extent("extent.gpkg", geometry, layer="decoy")
    extent = StepFileRef(path="extent.gpkg", layer="analysis_extent")
    windows = plan_dem_windows("dem.tif", extent, radius_pixels=1)
    prepare_dem("dem.tif", "prepared.tif", windows)
    write_slope_raster("prepared.tif", "expanded.tif", band=1, z_factor=1.0, slope_unit="percent")
    return windows, extent


def test_clip_uses_covered_pixel_centers_for_holes_parts_and_boundary(polygon_case):
    windows, extent = polygon_case

    metadata = clip_terrain_to_analysis_extent("expanded.tif", "cropped.tif", windows=windows, analysis_extent=extent)

    assert (metadata.width, metadata.height) == (6, 5)
    assert metadata.transform == (10, 0, 500010, 0, -10, 4400080)
    inside = np.zeros((5, 6), dtype=bool)
    inside[:, :3] = True
    inside[2, 1] = False
    inside[3:, 4:] = True
    with rasterio.open("expanded.tif") as expanded, rasterio.open("cropped.tif") as cropped:
        source = expanded.read(1, window=Window(1, 1, 6, 5), masked=True)
        observed = cropped.read(1, masked=True)
        assert cropped.units == expanded.units == ("percent",)
        assert cropped.tags() == expanded.tags()
        expected_mask = ~inside | np.ma.getmaskarray(source)
        np.testing.assert_array_equal(np.ma.getmaskarray(observed), expected_mask)
        np.testing.assert_array_equal(observed.data[~expected_mask], source.data[~expected_mask])
        assert (observed.data[expected_mask] == -9999).all()
        assert not observed.mask[0, 2] and not observed.mask[3, 4]
        assert observed.mask[2, 1] and observed.mask[0, 0]


def test_decimal_grid_uses_original_center_on_exact_polygon_boundary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    affine = Affine(0.1, 0, 500000.1, 0, -0.2, 4400070.7)
    with rasterio.open(
        "dem.tif", "w", driver="GTiff", width=8, height=8, count=1, dtype="float32", crs=CRS, transform=affine,
    ) as dem:
        dem.write(np.tile(np.arange(8, dtype="float32"), (8, 1)), 1)
        dem.set_band_unit(1, "m")
    with rasterio.open("dem.tif") as dem:
        west, south, _, north = dem.window_bounds(Window(2, 2, 2, 2))
        east = dem.xy(2, 3)[0]
    _write_extent("extent.gpkg", box(west, south, east, north))
    extent = StepFileRef(path="extent.gpkg", layer="analysis_extent")
    windows = plan_dem_windows("dem.tif", extent, radius_pixels=1)
    prepare_dem("dem.tif", "prepared.tif", windows)
    write_slope_raster("prepared.tif", "expanded.tif", band=1, z_factor=1.0, slope_unit="degree")

    clip_terrain_to_analysis_extent("expanded.tif", "cropped.tif", windows=windows, analysis_extent=extent)

    with rasterio.open("cropped.tif") as terrain:
        assert terrain.shape == (2, 2)
        assert terrain.xy(0, 1)[0] > east
        assert terrain.read(1, masked=True).count() == 4


@pytest.mark.parametrize(("change", "message"), [
    ("offset", "does not match"), ("transform", "terrain grid"),
    ("crs", "terrain CRS"), ("layer", "analysis extent reference"),
])
def test_clip_rejects_inconsistent_windows_grid_or_original_extent(polygon_case, change, message):
    windows, extent = polygon_case
    if change == "offset":
        windows["analysis_window"]["col_offset"] = 2
    elif change == "layer":
        extent = StepFileRef(path="extent.gpkg", layer="decoy")
    else:
        with rasterio.open("expanded.tif", "r+") as expanded:
            if change == "transform":
                expanded.transform = expanded.transform * Affine.translation(1, 0)
            else:
                expanded.crs = "EPSG:32651"

    with pytest.raises(ValueError, match=message):
        clip_terrain_to_analysis_extent("expanded.tif", "cropped.tif", windows=windows, analysis_extent=extent)

    assert not Path("cropped.tif").exists()
