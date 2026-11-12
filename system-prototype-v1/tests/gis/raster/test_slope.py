import json
import math
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
from rasterio.transform import Affine
from rasterio.windows import Window

from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.gis.raster.terrain import validate_terrain_unit_report, write_slope_raster, write_terrain_raster
from geopilot.gis.raster.window_io import copy_raster_window
from geopilot.persistence.workspace import create_run_workspace
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "tests/fixtures/raster"
CRS = "EPSG:32650"
GRID = Affine(10, 0, 500000, 0, -10, 4400070)
RUN_ID = "slope-verification"
UNITS = "check_horizontal_vertical_units"
SLOPE = "calculate_slope_or_aspect"
DEM = "intermediate/prepared_dem.tif"
UNIT_REPORT = "intermediate/units.json"
TERRAIN = "outputs/slope.tif"
TOLERANCE = 1e-5


@pytest.fixture
def workdir(tmp_path):
    workspace = create_run_workspace(tmp_path, "dem-terrain", RUN_ID)
    for step in (UNITS, SLOPE):
        workspace.create_step(step)
    return workspace.path


def _run(workdir, *, tool_id, step, inputs, outputs, parameters):
    path = ROOT / f"config/tools/{tool_id}.yaml"
    tool = ToolContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    worker = ROOT / f"src/geopilot/gis/tools/{tool.implementation.rsplit('.', 1)[-1]}.py"
    implementations = {tool.implementation: worker}
    registry = ToolRegistry(python_implementations=implementations)
    registry.register(tool, required_modules=("rasterio", "numpy"))
    request = StepRequest(
        run_id=RUN_ID, step_key=step, workdir_ref=RUN_ID, tool=tool.ref, inputs=inputs, parameters=parameters,
        output_targets=outputs, log_path=f"steps/{step}/process.log", cancel_path="cancel.flag",
    )
    result = run_step(
        request, registry=registry, workdir=workdir, python_implementations=implementations,
        input_preflight=preflight_step_inputs,
    )
    assert StepRequest.model_validate_json((workdir / "steps" / step / "request.json").read_text()) == request
    assert StepResult.model_validate_json((workdir / "steps" / step / "result.json").read_text()) == result
    return result


def _run_units(workdir, *, band=1, z_factor=1.0):
    result = _run(
        workdir, tool_id="python-dem-unit-check", step=UNITS, inputs={"dem": {"path": DEM}},
        outputs={"unit_report": {"path": UNIT_REPORT}},
        parameters={"band": band, "z_factor": z_factor, "vertical_unit": "metadata"},
    )
    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    return result.outputs["unit_report"].path


def _run_slope(workdir, unit_report, *, unit, band=1, z_factor=1.0):
    return _run(
        workdir, tool_id="python-dem-slope", step=SLOPE, inputs={"dem": {"path": DEM}, "unit_report": {"path": unit_report}},
        outputs={"terrain_raster": {"path": TERRAIN}}, parameters={"band": band, "z_factor": z_factor, "slope_unit": unit},
    )


def _write_dem(path, values, *, transform=GRID, crs=CRS, units=("m",), scales=None, offsets=None, valid=None):
    bands = values[np.newaxis] if values.ndim == 2 else values
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        path, "w", driver="GTiff", width=bands.shape[2], height=bands.shape[1], count=bands.shape[0],
        dtype=bands.dtype, crs=crs, transform=transform, nodata=-9999,
    ) as dataset:
        dataset.write(bands)
        dataset.units = units
        if scales is not None:
            dataset.scales = scales
        if offsets is not None:
            dataset.offsets = offsets
        if valid is not None:
            dataset.write_mask(valid)


@pytest.mark.parametrize(("unit", "reference_key"), [("degree", "slope_degrees"), ("percent", "slope_percent")])
def test_real_slope_child_matches_golden_target_edges_and_interior(workdir, unit, reference_key):
    copy_raster_window(GOLDEN / "dem-plane.tif", workdir / DEM, Window(1, 1, 5, 5))
    before = fingerprint_file(workdir / DEM)
    unit_report = _run_units(workdir)

    result = _run_slope(workdir, unit_report, unit=unit)

    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    assert result.outputs["terrain_raster"].type == "raster_terrain"
    assert result.outputs["terrain_raster"].path == TERRAIN
    assert fingerprint_file(workdir / DEM) == before
    reference = json.loads((GOLDEN / "expected.json").read_text())["dem-plane.tif"]["terrain_reference"][reference_key]
    with rasterio.open(workdir / TERRAIN) as slope:
        assert slope.crs.to_string() == CRS
        assert slope.transform == Affine(10, 0, 500010, 0, -10, 4400060)
        assert slope.units == (unit,) and slope.dtypes == ("float32",) and slope.nodata == -9999
        values = slope.read(1, masked=True)
    neighborhood_edge = np.ones((5, 5), dtype=bool)
    neighborhood_edge[1:4, 1:4] = False
    np.testing.assert_array_equal(np.ma.getmaskarray(values), neighborhood_edge)
    assert (values.data[neighborhood_edge] == -9999).all()
    analysis_boundary = np.ones((3, 3), dtype=bool)
    analysis_boundary[1, 1] = False
    np.testing.assert_allclose(values[1:4, 1:4][analysis_boundary], reference, rtol=0, atol=TOLERANCE)
    assert values[2, 2] == pytest.approx(reference, abs=TOLERANCE)
    log = (workdir / result.log_path).read_text()
    evidence = json.loads(next(line.removeprefix("terrain: ") for line in log.splitlines() if line.startswith("terrain: ")))
    assert evidence["algorithm"] == "Horn" and evidence["algorithm_version"] == 1
    assert evidence["edge_interpolation"] is False and evidence["radius_pixels"] == 1
    assert evidence["z_factor"] == 1.0 and evidence["output_unit"] == unit
    assert evidence["rasterio"] and evidence["gdal"] and "exit_code: 0" in log


def test_slope_uses_nonsquare_pixels_selected_band_scale_and_feet_ratio(workdir):
    rows, columns = np.indices((5, 6))
    elevation = 100 + 6 * columns - 10 * rows
    _write_dem(
        workdir / DEM, np.stack((np.zeros_like(elevation), elevation)).astype("int16"),
        transform=Affine(10, 0, 500000, 0, -20, 4400070), units=("m", "ft"), scales=(1, 2.5), offsets=(0, 9000),
    )
    report = _run_units(workdir, band=2, z_factor=0.3048)

    result = _run_slope(workdir, report, unit="percent", band=2, z_factor=0.3048)

    assert result.status == "SUCCEEDED", result
    expected = 100 * math.hypot(6 * 2.5 * 0.3048 / 10, 10 * 2.5 * 0.3048 / 20)
    with rasterio.open(workdir / TERRAIN) as slope:
        assert slope.res == (10, 20) and slope.scales == (1.0,) and slope.offsets == (0.0,)
        values = slope.read(1, masked=True)
    assert values.count() == 12
    np.testing.assert_allclose(values.compressed(), expected, rtol=0, atol=TOLERANCE)
    log = (workdir / result.log_path).read_text()
    assert '"z_factor": 0.3048' in log and '"source_scale": 2.5' in log and '"source_offset": 9000.0' in log


@pytest.mark.parametrize(("elevations", "expected"), [
    ([[2, 4, 10], [3, 5, 6], [1, 1, 2]], 100 * math.hypot(15 / 40, 15 / 80)),
    ([[50, 50, 50]] * 3, 0.0),
])
def test_nonplanar_stencil_uses_horn_weights_and_flat_slope_is_valid(tmp_path, elevations, expected):
    source, output = tmp_path / "dem.tif", tmp_path / "slope.tif"
    _write_dem(source, np.array(elevations, dtype="float32"), transform=Affine(5, 0, 500000, 0, -10, 4400070))

    write_slope_raster(source, output, band=1, z_factor=1.0, slope_unit="percent")

    with rasterio.open(output) as dataset:
        observed = dataset.read(1, masked=True)
    assert observed.count() == 1
    assert observed[1, 1] == pytest.approx(expected, abs=TOLERANCE)


def test_shared_horn_reads_tile_halos_and_preserves_mask_across_tile_seams(tmp_path, monkeypatch):
    source, output = tmp_path / "large-dem.tif", tmp_path / "gradient.tif"
    rows, columns = np.indices((517, 519))
    elevations = (3 * columns - 4 * rows).astype("float32")
    elevations[12, 12] = np.nan
    mask = np.full(elevations.shape, 255, dtype="uint8")
    mask[256, 255] = 0
    _write_dem(source, elevations, transform=Affine(3, 0, 500000, 0, -2, 4400070), valid=mask)
    actual_open = rasterio.open
    windows = []

    @contextmanager
    def track_reads(path, *args, **kwargs):
        with actual_open(path, *args, **kwargs) as dataset:
            if Path(path) == source:
                actual_read = dataset.read

                def read(*args, **kwargs):
                    windows.append(kwargs.get("window"))
                    return actual_read(*args, **kwargs)

                dataset.read = read
            yield dataset

    def check_directions(east, north):
        np.testing.assert_allclose(east.compressed(), 1, rtol=0, atol=1e-12)
        np.testing.assert_allclose(north.compressed(), 2, rtol=0, atol=1e-12)
        return east + north

    with monkeypatch.context() as patch:
        patch.setattr(rasterio, "open", track_reads)
        write_terrain_raster(source, output, band=1, z_factor=1.0, output_unit="degree", derive=check_directions)

    assert len(windows) == 9
    assert all(window is not None and window.width <= 258 and window.height <= 258 for window in windows)
    expected_mask = np.ones(elevations.shape, dtype=bool)
    expected_mask[1:-1, 1:-1] = False
    expected_mask[11:14, 11:14] = expected_mask[255:258, 254:257] = True
    with rasterio.open(output) as dataset:
        observed = dataset.read(1, masked=True)
    np.testing.assert_array_equal(np.ma.getmaskarray(observed), expected_mask)
    assert (observed.compressed() == 3).all()


@pytest.mark.parametrize("change", ["request_ratio", "report_ratio", "report_band"])
def test_actual_slope_rejects_a_unit_report_or_request_inconsistent_with_dem(workdir, change):
    copy_raster_window(GOLDEN / "dem-plane.tif", workdir / DEM, Window(1, 1, 5, 5))
    report = workdir / _run_units(workdir)
    validate_terrain_unit_report(workdir / DEM, report, band=1, z_factor=1.0)
    ratio = 1.0
    if change == "request_ratio":
        ratio = 0.5
    else:
        payload = json.loads(report.read_text())
        if change == "report_ratio":
            payload["z_factor"] = 0.5
        else:
            payload["band"] = True
        report.write_text(json.dumps(payload))

    result = _run_slope(workdir, UNIT_REPORT, unit="degree", z_factor=ratio)

    assert result.status == "FAILED" and result.exit_code == 1
    assert "unit report" in (workdir / result.log_path).read_text().lower()
    assert not (workdir / TERRAIN).exists()


@pytest.mark.parametrize("crs", ["EPSG:4326", "EPSG:3857"])
def test_terrain_writer_rejects_an_angular_or_web_mercator_grid(tmp_path, crs):
    source, output = tmp_path / "dem.tif", tmp_path / "slope.tif"
    _write_dem(source, np.ones((3, 3), dtype="float32"), crs=crs)

    with pytest.raises(ValueError, match="projected DEM"):
        write_slope_raster(source, output, band=1, z_factor=1.0, slope_unit="degree")

    assert not output.exists()
