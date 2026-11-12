import json
from contextlib import contextmanager
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
import rasterio
import yaml
from rasterio.transform import Affine
from shapely.geometry import box

from geopilot.api.server import _worker_paths
from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepFileRef, StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.gis.raster.clip_back import clip_terrain_to_analysis_extent
from geopilot.gis.raster.dem_prepare import plan_dem_windows, prepare_dem
from geopilot.gis.raster.terrain import write_slope_raster
from geopilot.gis.raster.threshold import ThresholdSpec, classify_raster
from geopilot.persistence.workspace import create_run_workspace
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[3]
DEM = ROOT / "tests/fixtures/raster/dem-plane.tif"
CRS = "EPSG:32650"
GRID = Affine(10, 2, 500000, 1, -20, 4400070)


def _interval(class_id, lower, upper, *, include_lower=True, include_upper=False):
    return {
        "class_id": class_id, "lower": lower, "upper": upper,
        "include_lower": include_lower, "include_upper": include_upper,
    }


def _write_raster(path, values, *, nodata=-9999, mask=None, scale=1.0, offset=0.0):
    bands = values[np.newaxis] if values.ndim == 2 else values
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        path, "w", driver="GTiff", width=bands.shape[2], height=bands.shape[1], count=bands.shape[0],
        dtype=bands.dtype, crs=CRS, transform=GRID, nodata=nodata,
    ) as raster:
        raster.write(bands)
        raster.units = ("degree",) * raster.count
        raster.scales = (scale,) * raster.count
        raster.offsets = (offset,) * raster.count
        raster.update_tags(AREA_OR_POINT="Point", STATISTICS_MINIMUM="stale")
        if mask is not None:
            raster.write_mask(mask)


def test_classes_mask_and_tags_match_manual_pixels_on_original_grid(tmp_path):
    source, target = tmp_path / "terrain.tif", tmp_path / "classes.tif"
    values = np.array([[-9999, 0, 10, 19.5], [20, 20.5, 35, np.nan], [np.inf, -5, 40, 15]], dtype="float32")
    mask = np.ones(values.shape, dtype=bool)
    mask[0, 0] = mask[2, 3] = False
    _write_raster(source, values, mask=mask)
    before = fingerprint_file(source)
    threshold = {"intervals": [
        _interval(1, 10, 20, include_upper=True), _interval(254, 20, 40, include_lower=False, include_upper=True),
    ]}
    expected = np.array([[255, 0, 1, 1], [1, 254, 254, 255], [255, 0, 254, 255]], dtype="uint8")

    metadata = classify_raster(source, target, threshold=threshold)

    assert metadata.crs == CRS and metadata.transform == tuple(GRID)[:6] and metadata.nodata == (255,)
    assert fingerprint_file(source) == before
    assert set(tmp_path.iterdir()) == {source, target}
    with rasterio.open(target) as raster:
        assert raster.crs.to_string() == CRS and raster.transform == GRID and raster.shape == values.shape
        assert raster.dtypes == ("uint8",) and raster.nodatavals == (255,)
        assert raster.descriptions == ("threshold_class",) and raster.units == ("class",)
        assert raster.scales == (1.0,) and raster.offsets == (0.0,)
        np.testing.assert_array_equal(raster.read(1), expected)
        np.testing.assert_array_equal(raster.read_masks(1), np.where(expected == 255, 0, 255))
        assert raster.read(1, masked=True).count() == 8
        tags = raster.tags()
        assert tags["AREA_OR_POINT"] == "Point" and "STATISTICS_MINIMUM" not in tags
        assert tags["BACKGROUND_CLASS"] == "0" and tags["NODATA_CLASS"] == "255"
        assert tags["SOURCE_UNIT"] == "degree" and tags["RESAMPLING"] == "none"
        assert json.loads(tags["THRESHOLD_INTERVALS"]) == threshold["intervals"]


@pytest.mark.parametrize(("left_closed", "right_closed", "at_endpoint"), [
    (False, True, 2), (True, False, 1), (False, False, 0),
])
def test_touching_bounds_respect_each_open_closed_choice(tmp_path, left_closed, right_closed, at_endpoint):
    source, target = tmp_path / "terrain.tif", tmp_path / "classes.tif"
    _write_raster(source, np.array([[0.0, 10.0, 20.0]], dtype="float32"))
    threshold = {"intervals": [
        _interval(1, None, 10, include_upper=left_closed), _interval(2, 10, None, include_lower=right_closed),
    ]}
    classify_raster(source, target, threshold=threshold)
    with rasterio.open(target) as raster:
        assert raster.read(1).tolist() == [[1, at_endpoint, 2]]


def test_unsorted_disjoint_intervals_can_share_class_and_include_singleton(tmp_path):
    source, target = tmp_path / "aspect.tif", tmp_path / "classes.tif"
    _write_raster(source, np.array([[0, 22.5, 90, 90.125, 180, 337.5, 359.5, 360]], dtype="float32"))
    threshold = {"intervals": [
        _interval(7, 337.5, 360), _interval(10, 90, 180, include_lower=False),
        _interval(8, 90, 90, include_upper=True), _interval(7, 0, 22.5),
    ]}
    spec = ThresholdSpec.model_validate(threshold)
    assert spec.model_dump(mode="json") == threshold
    classify_raster(source, target, threshold=threshold)
    with rasterio.open(target) as raster:
        assert raster.read(1).tolist() == [[7, 0, 8, 10, 0, 7, 7, 0]]
        assert json.loads(raster.tags()["THRESHOLD_INTERVALS"]) == threshold["intervals"]


def test_thresholds_compare_physical_values_after_scale_and_offset(tmp_path):
    source, target = tmp_path / "scaled.tif", tmp_path / "classes.tif"
    _write_raster(source, np.array([[0, 4, 5, 8, 10]], dtype="uint16"), nodata=0, scale=0.5, offset=-2.0)
    classify_raster(source, target, threshold={"intervals": [_interval(1, 0, 2, include_upper=True)]})
    with rasterio.open(target) as raster:
        assert raster.read(1).tolist() == [[255, 1, 1, 1, 0]]
        assert float(raster.tags()["SOURCE_SCALE"]) == 0.5 and float(raster.tags()["SOURCE_OFFSET"]) == -2.0


@pytest.mark.parametrize("threshold", [
    None, {}, {"intervals": []}, {"intervals": [_interval(1, 0, 1)], "unknown": 1},
    {"intervals": [{"class_id": 1, "lower": 0, "upper": 1, "include_lower": True}]},
    {"intervals": [_interval(True, 0, 1)]}, {"intervals": [_interval(0, 0, 1)]}, {"intervals": [_interval(255, 0, 1)]},
    {"intervals": [_interval(1, True, 1)]}, {"intervals": [_interval(1, "0", 1)]},
    {"intervals": [_interval(1, float("nan"), 1)]}, {"intervals": [_interval(1, 0, float("inf"))]},
    {"intervals": [_interval(1, 0, 1, include_lower=1)]}, {"intervals": [_interval(1, 4, 3)]},
    {"intervals": [_interval(1, 4, 4)]},
    {"intervals": [_interval(1, 0, 1, include_upper=True), _interval(2, 1, 2)]},
    {"intervals": [_interval(1, 0, 2), _interval(1, 1, 3)]},
    {"intervals": [_interval(1, None, None), _interval(2, 10, None)]},
])
def test_invalid_threshold_never_publishes_output(tmp_path, threshold):
    source, target = tmp_path / "terrain.tif", tmp_path / "classes.tif"
    _write_raster(source, np.ones((2, 2), dtype="float32"))
    with pytest.raises(ValueError):
        classify_raster(source, target, threshold=threshold)
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("values", [np.ones((2, 3, 3), dtype="float32"), np.ones((3, 3), dtype="complex64")])
def test_input_requires_one_real_valued_band(tmp_path, values):
    source, target = tmp_path / "terrain.tif", tmp_path / "classes.tif"
    _write_raster(source, values, nodata=None)
    with pytest.raises(ValueError):
        classify_raster(source, target, threshold={"intervals": [_interval(1, None, None)]})
    assert not target.exists()


@pytest.mark.parametrize("existing_name", ["classes.tif", "classes.tif.msk", "classes.tif.aux.xml", "classes.tif.ovr"])
def test_preexisting_target_or_companion_is_preserved(tmp_path, existing_name):
    source, target = tmp_path / "terrain.tif", tmp_path / "classes.tif"
    _write_raster(source, np.ones((2, 2), dtype="float32"))
    existing = tmp_path / existing_name
    existing.write_bytes(b"already exists")
    before = fingerprint_file(source)
    with pytest.raises(FileExistsError):
        classify_raster(source, target, threshold={"intervals": [_interval(1, None, None)]})
    assert fingerprint_file(source) == before and existing.read_bytes() == b"already exists"
    assert set(tmp_path.iterdir()) == {source, existing}


def test_uses_bounded_real_raster_reads_across_output_block_seams(tmp_path, monkeypatch):
    source, target = tmp_path / "terrain.tif", tmp_path / "classes.tif"
    rows, columns = np.indices((517, 519))
    values = ((rows + columns) % 3).astype("float32")
    values[255, 256] = -9999
    _write_raster(source, values)
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

    with monkeypatch.context() as patch:
        patch.setattr(rasterio, "open", track_reads)
        classify_raster(source, target, threshold={"intervals": [_interval(3, 1, 2)]})

    assert len(windows) == 9
    assert all(window is not None and window.width <= 256 and window.height <= 256 for window in windows)
    expected = np.where(values == -9999, 255, np.where(values == 1, 3, 0))
    with rasterio.open(target) as raster:
        np.testing.assert_array_equal(raster.read(1), expected)


def test_real_threshold_worker_reopens_terrain_clipped_to_original_polygon(tmp_path, monkeypatch):
    run_id, step = "threshold-test", "classify_dem_threshold"
    workspace = create_run_workspace(tmp_path, "dem-threshold", run_id)
    workspace.create_step(step)
    workdir = workspace.path
    extent_ref = StepFileRef(path="intermediate/extent.gpkg", layer="analysis_extent")
    polygon = box(500020, 4400020, 500050, 4400050).difference(box(500032, 4400032, 500038, 4400038))
    pyogrio.write_dataframe(gpd.GeoDataFrame(geometry=[polygon], crs=CRS), workdir / extent_ref.path, layer=extent_ref.layer)
    prepared, expanded = workdir / "intermediate/prepared.tif", workdir / "intermediate/expanded-slope.tif"
    terrain = "intermediate/terrain.tif"
    with monkeypatch.context() as patch:
        patch.chdir(workdir)
        windows = plan_dem_windows(DEM, extent_ref, 1)
        prepare_dem(DEM, prepared, windows)
        write_slope_raster(prepared, expanded, band=1, z_factor=1.0, slope_unit="degree")
        clip_terrain_to_analysis_extent(expanded, workdir / terrain, windows=windows, analysis_extent=extent_ref)
    source_before = fingerprint_file(workdir / terrain)
    contract = ToolContract.model_validate(yaml.safe_load((ROOT / "config/tools/python-dem-threshold.yaml").read_text()))
    implementations = _worker_paths([contract])
    registry = ToolRegistry(python_implementations=implementations)
    registry.register(contract, required_modules=("rasterio", "numpy"))
    threshold = {"intervals": [_interval(3, 40, 50)]}
    target = "outputs/threshold.tif"
    request = StepRequest(
        run_id=run_id, step_key=step, tool=contract.ref, workdir_ref=run_id, inputs={"terrain_raster": {"path": terrain}},
        parameters={"threshold": threshold}, output_targets={"threshold_regions": {"path": target}},
        log_path=f"steps/{step}/process.log", cancel_path="cancel.flag",
    )
    result = run_step(
        request, registry=registry, workdir=workdir, python_implementations=implementations,
        input_preflight=preflight_step_inputs,
    )

    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    assert result.outputs["threshold_regions"].type == "raster_classification"
    assert result.outputs["threshold_regions"].path == target
    assert StepResult.model_validate_json((workdir / "steps" / step / "result.json").read_text()) == result
    assert fingerprint_file(workdir / terrain) == source_before
    log = (workdir / result.log_path).read_text()
    evidence = json.loads(next(line.removeprefix("threshold_classification: ") for line in log.splitlines()
                               if line.startswith("threshold_classification: ")))
    assert evidence["threshold"] == threshold and evidence["resampling"] == "none"
    with rasterio.open(workdir / terrain) as source, rasterio.open(workdir / target) as output:
        assert output.crs == source.crs and output.transform == source.transform
        assert output.read(1).tolist() == [[3, 3, 3], [3, 255, 3], [3, 3, 3]]
        np.testing.assert_array_equal(output.read_masks(1), source.read_masks(1))
