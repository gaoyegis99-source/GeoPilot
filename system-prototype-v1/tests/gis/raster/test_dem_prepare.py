import json
import shutil
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
import rasterio
import yaml
from pyproj import CRS
from rasterio.transform import Affine
from shapely.geometry import box

from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepFileRef, StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.persistence.workspace import create_run_workspace
from geopilot.plans.compiler import compile_step_request
from geopilot.plans.models import AnalysisPlan
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DEM = ROOT / "tests/fixtures/raster/dem-plane.tif"
EXPAND = "expand_extent_by_algorithm_neighborhood"
PREPARE = "read_dem_for_expanded_extent"
WINDOWS = f"intermediate/{EXPAND}/windows.json"
CROPPED = f"intermediate/{PREPARE}/prepared_dem.tif"
DEM = "inputs/dem.tif"
EXTENT = "inputs/extent.gpkg"
RUN_ID = "dem-preparation"
ANALYSIS = (500020.0, 4400020.0, 500050.0, 4400050.0)
EXPANDED = (500010.0, 4400010.0, 500060.0, 4400060.0)
GRID = Affine(10, 0, 500000, 0, -10, 4400070)
COMPOUND_CRS = CRS.from_user_input("EPSG:32650+5773")


@pytest.fixture
def workdir(tmp_path):
    workspace = create_run_workspace(tmp_path, "terrain", RUN_ID)
    for key in (EXPAND, PREPARE):
        workspace.create_step(key)
        (workspace.path / "intermediate" / key).mkdir()
    return workspace.path


@pytest.fixture
def tools():
    tool_ids = {EXPAND: "python-dem-expand-extent", PREPARE: "python-dem-prepare"}
    contracts = {
        step: ToolContract.model_validate(yaml.safe_load((ROOT / f"config/tools/{tool_id}.yaml").read_text()))
        for step, tool_id in tool_ids.items()
    }
    workers = {
        contract.implementation: ROOT / f"src/geopilot/gis/tools/{contract.implementation.split('.')[1]}.py"
        for contract in contracts.values()
    }
    registry = ToolRegistry(python_implementations=workers)
    for contract in contracts.values():
        registry.register(contract, required_modules=("rasterio", "pyogrio"))
    return contracts, workers, registry


def _confirmed_plan(contracts):
    return AnalysisPlan(
        id="dem-prepare", task_id="terrain", version=1, status="confirmed", goal={"summary": "Prepare a DEM"},
        inputs={
            "analysis_extent": {"asset_id": "area", "asset_version": 1},
            "dem": {"asset_id": "elevation", "asset_version": 1},
        },
        method={"id": "dem-preparation-verification", "version": 1}, critical_options={}, check_rules=["output_exists"],
        steps=[
            {
                "key": EXPAND, "tool": contracts[EXPAND].ref, "parameters": {"radius_pixels": 1},
                "inputs": {"analysis_extent": {"asset": "analysis_extent"}, "dem": {"asset": "dem"}},
                "outputs": {"windows": {"type": "json", "target": WINDOWS}},
            },
            {
                "key": PREPARE, "tool": contracts[PREPARE].ref, "parameters": {},
                "inputs": {"dem": {"asset": "dem"}, "windows": {"step": EXPAND, "output": "windows"}},
                "outputs": {"prepared_dem": {"type": "raster_elevation", "target": CROPPED}},
            },
        ],
        expected_artifacts=[{"role": "prepared_dem", "step": PREPARE, "output": "prepared_dem", "type": "raster_elevation"}],
        issues=[],
    )


def _run(workdir, tools, step, *, prior_results=None, dem_path=DEM):
    contracts, workers, registry = tools
    request = compile_step_request(
        _confirmed_plan(contracts), step, run_id=RUN_ID, prior_results=prior_results or {},
        asset_files={"analysis_extent": StepFileRef(path=EXTENT, layer="analysis_extent"), "dem": StepFileRef(path=dem_path)},
    )
    result = run_step(
        request, registry=registry, workdir=workdir, python_implementations=workers, input_preflight=preflight_step_inputs,
    )
    folder = workdir / "steps" / step
    assert StepRequest.model_validate_json((folder / "request.json").read_text()) == request
    assert StepResult.model_validate_json((folder / "result.json").read_text()) == result
    return request, result


def _write_extent(workdir, bounds=ANALYSIS, crs="EPSG:32650"):
    path = workdir / EXTENT
    pyogrio.write_dataframe(gpd.GeoDataFrame(geometry=[box(*bounds)], crs=crs), path, layer="analysis_extent")
    pyogrio.write_dataframe(gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)], crs=crs), path, layer="decoy")


def _write_compound_dem(path):
    values = np.tile(np.arange(100, 170, 10, dtype="float32"), (7, 1))
    values[1, 1] = -9999
    with rasterio.open(
        path, "w", driver="GTiff", width=7, height=7, count=1, dtype="float32", nodata=-9999,
        crs=rasterio.crs.CRS.from_wkt(COMPOUND_CRS.to_wkt()), transform=GRID,
    ) as dem:
        dem.write(values, 1)
        dem.set_band_unit(1, "m")
        dem.set_band_description(1, "elevation")
    return values[1:6, 1:6]


@pytest.mark.parametrize("compound", [False, True])
def test_actual_steps_prepare_golden_grid_and_preserve_nodata(workdir, tools, compound):
    dem_path = workdir / DEM
    if compound:
        expected = _write_compound_dem(dem_path)
    else:
        shutil.copy2(GOLDEN_DEM, dem_path)
        expected = np.tile(np.arange(110, 160, 10, dtype="float32"), (5, 1))
    _write_extent(workdir)
    before = {path: fingerprint_file(workdir / path) for path in (DEM, EXTENT)}

    _, expand = _run(workdir, tools, EXPAND)
    assert expand.status == "SUCCEEDED" and expand.exit_code == 0, expand
    assert expand.outputs["windows"].path == WINDOWS
    evidence = json.loads((workdir / WINDOWS).read_text())
    assert evidence["analysis_extent"] == {"path": EXTENT, "layer": "analysis_extent"}
    assert evidence["source"]["path"] == DEM
    assert evidence["source"]["metadata_fingerprint"] == fingerprint_file(dem_path, strategy="metadata").digest
    assert evidence["source"]["grid"]["band_units"] == ["m"]
    assert evidence["radius_pixels"] == 1
    assert evidence["requested_bounds"] == evidence["analysis_bounds"] == list(ANALYSIS)
    assert evidence["expanded_bounds"] == list(EXPANDED)
    assert evidence["analysis_window"] == {"col_offset": 2, "row_offset": 2, "width": 3, "height": 3}
    assert evidence["expanded_window"] == {"col_offset": 1, "row_offset": 1, "width": 5, "height": 5}
    assert "dem_windows: " in (workdir / expand.log_path).read_text()

    request, prepare = _run(workdir, tools, PREPARE, prior_results={EXPAND: expand})
    assert request.inputs["windows"].path == expand.outputs["windows"].path
    assert prepare.status == "SUCCEEDED" and prepare.exit_code == 0, prepare
    assert prepare.outputs["prepared_dem"].path == CROPPED
    with rasterio.open(workdir / CROPPED) as dem:
        actual_crs = CRS.from_user_input(dem.crs)
        assert actual_crs.equals(COMPOUND_CRS if compound else CRS.from_epsg(32650))
        assert (dem.width, dem.height) == (5, 5)
        assert dem.transform == Affine(10, 0, 500010, 0, -10, 4400060)
        assert tuple(dem.bounds) == EXPANDED
        assert dem.nodata == -9999 and dem.units == ("m",) and dem.descriptions == ("elevation",)
        np.testing.assert_array_equal(dem.read(1), expected)
        np.testing.assert_array_equal(np.ma.getmaskarray(dem.read(1, masked=True)), expected == -9999)

    log = (workdir / prepare.log_path).read_text()
    assert "prepared_dem: " in log and '"rasterio"' in log and '"gdal"' in log and "exit_code: 0" in log
    assert before == {path: fingerprint_file(workdir / path) for path in before}


@pytest.mark.parametrize(("bounds", "crs", "message"), [
    ((500000.0, 4400020.0, 500050.0, 4400050.0), "EPSG:32650", "cannot cover the 1-pixel neighborhood"),
    ((116.0, 39.0, 116.1, 39.1), "EPSG:4326", "same horizontal CRS"),
])
def test_actual_expansion_blocks_insufficient_coverage_and_different_crs(workdir, tools, bounds, crs, message):
    shutil.copy2(GOLDEN_DEM, workdir / DEM)
    _write_extent(workdir, bounds=bounds, crs=crs)

    _, result = _run(workdir, tools, EXPAND)

    assert result.status == "FAILED" and result.exit_code == 1
    assert message in (workdir / result.log_path).read_text()
    assert not (workdir / WINDOWS).exists()
    assert not (workdir / CROPPED).exists()


@pytest.mark.parametrize("change", ["window", "window_type", "other_dem", "edited_dem"])
def test_actual_preparation_rejects_stale_or_inconsistent_evidence(workdir, tools, change):
    source = workdir / DEM
    shutil.copy2(GOLDEN_DEM, source)
    _write_extent(workdir)
    _, expand = _run(workdir, tools, EXPAND)
    assert expand.status == "SUCCEEDED", expand

    selected_dem = DEM
    if change in {"window", "window_type"}:
        record = json.loads((workdir / WINDOWS).read_text())
        if change == "window":
            record["expanded_window"]["width"] = 4
        else:
            record["expanded_window"]["col_offset"] = True
        (workdir / WINDOWS).write_text(json.dumps(record))
        message = "does not match the derived windows"
    elif change == "other_dem":
        selected_dem = "inputs/other-dem.tif"
        shutil.copy2(source, workdir / selected_dem)
        message = "does not refer to this staged DEM"
    else:
        with rasterio.open(source, "r+") as dem:
            dem.write(np.array([[999.0]], dtype="float32"), 1, window=rasterio.windows.Window(1, 1, 1, 1))
        message = "staged DEM changed after window planning"

    _, result = _run(workdir, tools, PREPARE, prior_results={EXPAND: expand}, dem_path=selected_dem)

    assert result.status == "FAILED" and result.exit_code == 1
    assert message in (workdir / result.log_path).read_text()
    assert not (workdir / CROPPED).exists()
