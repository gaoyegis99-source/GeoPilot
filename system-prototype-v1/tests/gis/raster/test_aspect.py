import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
from rasterio.windows import Window

from geopilot.domain.step_io import StepRequest
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.gis.raster.aspect import aspect_from_gradients, calculate_aspect
from geopilot.gis.raster.window_io import copy_raster_window
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[3]
DEM = ROOT / "tests/fixtures/raster/dem-plane.tif"


def test_downhill_azimuth_is_clockwise_from_grid_north():
    east = np.ma.array([0, -1, 0, 1, -1, 1, 0, 1, 1e-12, np.inf], mask=[False] * 7 + [True, False, False])
    north = np.ma.array([-1, 0, 1, 0, -1, 1, 0, 0, -1, 1])
    result = aspect_from_gradients(east, north)
    assert result.mask.tolist() == [False] * 6 + [True, True, False, True]
    assert result.data[:6].tolist() == [0, 90, 180, 270, 45, 225]
    assert (result.compressed() >= 0).all() and (result.compressed() < 360).all()
    assert result.data[-2] == 0


def test_prepared_real_dem_has_nine_west_facing_pixels_and_masked_outer_ring(tmp_path):
    source = tmp_path / "prepared.tif"
    copy_raster_window(DEM, source, Window(1, 1, 5, 5))
    output = tmp_path / "aspect.tif"
    metadata = calculate_aspect(source, output, band=1, z_factor=1)
    with rasterio.open(source) as dem, rasterio.open(output) as dataset:
        result = dataset.read(1, masked=True)
        assert dataset.crs == dem.crs and dataset.transform == dem.transform
        assert dataset.units == ("degree",) and metadata.band_units == ("degree",)
    valid = np.zeros((5, 5), dtype=bool)
    valid[1:-1, 1:-1] = True
    np.testing.assert_array_equal(~result.mask, valid)
    np.testing.assert_array_equal(result.compressed(), np.full(9, 270))


def test_flat_and_missing_neighbors_are_not_assigned_a_direction(tmp_path):
    with rasterio.open(DEM) as original:
        profile = original.profile
        values = original.read(1)
    values[:, :] = 100
    values[3, 3] = profile["nodata"]
    source = tmp_path / "flat.tif"
    with rasterio.open(source, "w", **profile) as dataset:
        dataset.write(values, 1)
        dataset.set_band_unit(1, "m")
    output = tmp_path / "flat-aspect.tif"
    calculate_aspect(source, output, band=1, z_factor=1)
    with rasterio.open(output) as dataset:
        assert dataset.read(1, masked=True).count() == 0

    with rasterio.open(DEM) as original:
        values = original.read(1)
    values[3, 3] = profile["nodata"]
    gradient = tmp_path / "gradient.tif"
    with rasterio.open(gradient, "w", **profile) as dataset:
        dataset.write(values, 1)
        dataset.set_band_unit(1, "m")
    gradient_output = tmp_path / "gradient-aspect.tif"
    calculate_aspect(gradient, gradient_output, band=1, z_factor=1)
    with rasterio.open(gradient_output) as dataset:
        result = dataset.read(1, masked=True)
        assert result.mask[2:5, 2:5].all()
        assert result[1, 5] == 270


@pytest.mark.parametrize(("tool", "mode", "expected"), [
    ("python-dem-aspect", "aspect", 270),
    ("python-dem-terrain", "aspect", 270),
    ("python-dem-terrain", "slope", 45),
])
def test_terrain_worker_consumes_actual_unit_report_and_records_convention(tmp_path, tool, mode, expected):
    for name in ("inputs", "intermediate", "steps/check_units", "steps/aspect"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    source = "inputs/prepared.tif"
    copy_raster_window(DEM, tmp_path / source, Window(1, 1, 5, 5))
    workers = {
        "geopilot.check_dem_units": ROOT / "src/geopilot/gis/tools/check_dem_units.py",
        "geopilot.calculate_dem_aspect": ROOT / "src/geopilot/gis/tools/calculate_dem_aspect.py",
        "geopilot.calculate_dem_terrain": ROOT / "src/geopilot/gis/tools/calculate_dem_terrain.py",
    }
    registry = ToolRegistry(python_implementations=workers)
    for name in ("python-dem-unit-check", tool):
        registry.register(ToolContract.model_validate(yaml.safe_load((ROOT / f"config/tools/{name}.yaml").read_text())))

    def run(key, tool, inputs, parameters, outputs):
        request = StepRequest(
            run_id="aspect-real", step_key=key, tool={"id": tool, "version": 1}, workdir_ref="aspect-real",
            inputs=inputs, parameters=parameters, output_targets=outputs,
            log_path=f"steps/{key}/process.log", cancel_path="cancel.flag",
        )
        return run_step(request, registry=registry, workdir=tmp_path, python_implementations=workers)

    units = run("check_units", "python-dem-unit-check", {"dem": {"path": source}},
                {"band": 1, "vertical_unit": "metadata", "z_factor": 1},
                {"unit_report": {"path": "intermediate/units.json"}})
    assert units.status == "SUCCEEDED"
    parameters = {"band": 1, "z_factor": 1}
    if tool == "python-dem-terrain":
        parameters["terrain_output"] = mode
    result = run("aspect", tool,
                 {"dem": {"path": source}, "unit_report": {"path": units.outputs["unit_report"].path}},
                 parameters, {"terrain_raster": {"path": "intermediate/terrain.tif"}})
    assert result.status == "SUCCEEDED" and result.exit_code == 0
    with rasterio.open(tmp_path / result.outputs["terrain_raster"].path) as dataset:
        assert dataset.read(1, masked=True).compressed().tolist() == [expected] * 9
    log = (tmp_path / result.log_path).read_text()
    evidence, = [json.loads(line.removeprefix("terrain: ")) for line in log.splitlines() if line.startswith("terrain: ")]
    assert evidence["algorithm"] == "Horn" and evidence["algorithm_version"] == 1 and evidence["z_factor"] == 1
    if mode == "aspect":
        assert evidence == {
            "algorithm": "Horn", "algorithm_version": 1, "terrain_output": "aspect", "z_factor": 1,
            "unit": "degree", "compute_edges": False, "direction": "clockwise_from_grid_north", "flat": "nodata",
        }
    else:
        assert evidence["output_unit"] == "degree"
