import json
import math
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
from pyproj import Geod, Transformer
from rasterio.transform import Affine
from rasterio.windows import Window

from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.gis.measurement.coverage import MeasurementPolicyError
from geopilot.gis.raster.area_stats import summarize_threshold_area
from geopilot.gis.raster.terrain import write_slope_raster
from geopilot.gis.raster.threshold import classify_raster
from geopilot.gis.raster.window_io import copy_raster_window
from geopilot.persistence.workspace import create_run_workspace
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "tests/fixtures/raster/dem-plane.tif"
INPUT = "inputs/threshold-regions.tif"
OUTPUT = "outputs/area-statistics.json"
STEP = "calculate_area_with_equal_area_projection"
RUN_ID = "dem-area-verification"
TARGET_CRS = "EPSG:6933"
UTM_CRS = "EPSG:32650"


@pytest.fixture
def workdir(tmp_path):
    workspace = create_run_workspace(tmp_path, "dem-terrain", RUN_ID)
    workspace.create_step(STEP)
    return workspace.path


def _write_classification(path, values, *, crs, transform, valid=None, nodata=255):
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        path, "w", driver="GTiff", width=values.shape[1], height=values.shape[0], count=1,
        dtype=values.dtype, crs=crs, transform=transform, nodata=nodata,
    ) as dataset:
        dataset.write(values, 1)
        dataset.set_band_description(1, "threshold_class")
        dataset.set_band_unit(1, "class")
        if valid is not None:
            dataset.write_mask(valid)


def _run_area(workdir, *, target_crs=TARGET_CRS):
    tool_path = ROOT / "config/tools/python-dem-area-stats.yaml"
    contract = ToolContract.model_validate(yaml.safe_load(tool_path.read_text(encoding="utf-8")))
    worker = ROOT / "src/geopilot/gis/tools/summarize_dem_area.py"
    implementations = {contract.implementation: worker}
    registry = ToolRegistry(python_implementations=implementations)
    registry.register(contract, required_modules=("rasterio", "pyproj"))
    request = StepRequest(
        run_id=RUN_ID, step_key=STEP, workdir_ref=RUN_ID, tool=contract.ref,
        inputs={"threshold_regions": {"path": INPUT}}, parameters={"area_crs": target_crs},
        output_targets={"area_statistics": {"path": OUTPUT}}, log_path=f"steps/{STEP}/process.log", cancel_path="cancel.flag",
    )
    result = run_step(
        request, registry=registry, workdir=workdir, python_implementations=implementations,
        input_preflight=preflight_step_inputs,
    )
    assert StepRequest.model_validate_json((workdir / "steps" / STEP / "request.json").read_text()) == request
    assert StepResult.model_validate_json((workdir / "steps" / STEP / "result.json").read_text()) == result
    return result


def _geodetic_cell_area(affine, row, col, *, segments=256):
    vertices = [affine * index for index in [(col, row), (col + 1, row), (col + 1, row + 1), (col, row + 1)]]
    rings = [np.linspace(start, end, segments, endpoint=False) for start, end in zip(vertices, vertices[1:] + vertices[:1])]
    x, y = np.concatenate(rings).T
    lon, lat = Transformer.from_crs(UTM_CRS, 4326, always_xy=True).transform(x, y, errcheck=True)
    area, _ = Geod(ellps="WGS84").polygon_area_perimeter(lon, lat)
    return abs(area)


def test_actual_runner_uses_true_equal_area_affine_determinant_and_masks(workdir):
    values = np.array([[1, 2, 0], [255, 1, 2]], dtype="uint8")
    valid = np.array([[255, 255, 255], [255, 255, 0]], dtype="uint8")
    # Pixel parallelogram has area abs(20*(-10) - 3*4) = 212 m².
    _write_classification(
        workdir / INPUT, values, crs=TARGET_CRS, transform=Affine(20, 3, 0, 4, -10, 0), valid=valid,
    )

    result = _run_area(workdir)

    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    assert result.outputs["area_statistics"].type == "json"
    report = json.loads((workdir / OUTPUT).read_text())
    assert report["source_crs"] == report["area_crs"] == TARGET_CRS
    assert report["area_method"] == "equal_area" and report["area_unit"] == "square_meter"
    assert report["cell_area_method"] == "affine_determinant" and report["resampling"] == "none"
    assert report["valid_pixel_count"] == 4 and report["selected_pixel_count"] == 3
    assert report["unselected_pixel_count"] == 1 and report["nodata_pixel_count"] == 2
    assert report["classes"] == [
        {"class_id": 1, "pixel_count": 2, "area_m2": 424.0},
        {"class_id": 2, "pixel_count": 1, "area_m2": 212.0},
    ]
    assert report["total_area_m2"] == 636.0
    assert "raster_area: " in (workdir / result.log_path).read_text()


def test_actual_runner_measures_utm_threshold_cells_in_equal_area_without_resampling(workdir):
    expanded = workdir / "intermediate/expanded-slope.tif"
    terrain = workdir / "intermediate/slope.tif"
    write_slope_raster(GOLDEN, expanded, band=1, z_factor=1, slope_unit="degree")
    copy_raster_window(expanded, terrain, Window(2, 2, 3, 3))
    classify_raster(terrain, workdir / INPUT, threshold={"intervals": [
        {"class_id": 7, "lower": 45, "upper": 45, "include_lower": True, "include_upper": True},
    ]})
    before = fingerprint_file(workdir / INPUT)
    affine = Affine(10, 0, 500020, 0, -10, 4400050)
    reference = math.fsum(_geodetic_cell_area(affine, row, col) for row in range(3) for col in range(3))
    assert reference == pytest.approx(900.7204322155151, rel=0, abs=1e-6)

    result = _run_area(workdir)

    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    report = json.loads((workdir / OUTPUT).read_text())
    assert report["source_crs"] == UTM_CRS and report["area_crs"] == TARGET_CRS
    assert report["cell_area_method"] == "projected_cell_boundary" and report["resampling"] == "none"
    assert report["classes"] == [{"class_id": 7, "pixel_count": 9, "area_m2": pytest.approx(reference, rel=0, abs=1e-5)}]
    assert report["total_area_m2"] == pytest.approx(reference, rel=0, abs=1e-5)
    assert report["total_area_m2"] > 900.7
    assert report["boundary_convergence"]["max_segments_per_edge_used"] >= 8
    assert fingerprint_file(workdir / INPUT) == before
    log = (workdir / result.log_path).read_text()
    assert "measurement: " in log and '"crs": "EPSG:6933"' in log and "raster_area: " in log


@pytest.mark.parametrize("target_crs", ["EPSG:3857", UTM_CRS])
def test_actual_runner_blocks_unsuitable_area_target_before_worker(workdir, target_crs):
    _write_classification(
        workdir / INPUT, np.array([[1]], dtype="uint8"), crs=UTM_CRS,
        transform=Affine(10, 0, 500020, 0, -10, 4400050),
    )

    result = _run_area(workdir, target_crs=target_crs)

    assert result.status == "FAILED" and result.exit_code is None
    assert result.error.phase == "preflight" and result.error.field == "parameters.area_crs"
    assert result.error.code == "MEASUREMENT_POLICY_REJECTED"
    assert not (workdir / OUTPUT).exists()
    assert "summarize_dem_area.py --request" not in (workdir / result.log_path).read_text()


def test_large_utm_cell_area_refines_curved_edges_against_independent_reference(tmp_path):
    source = tmp_path / "coarse-classification.tif"
    affine = Affine(100000, 0, 500020, 0, -100000, 4400050)
    _write_classification(source, np.array([[1]], dtype="uint8"), crs=UTM_CRS, transform=affine)
    reference = _geodetic_cell_area(affine, 0, 0, segments=1024)

    report = summarize_threshold_area(source, area_crs=TARGET_CRS)

    assert report["total_area_m2"] == pytest.approx(reference, rel=1e-8, abs=1e-5)
    assert report["boundary_convergence"]["max_segments_per_edge_used"] >= 64
    assert abs(report["total_area_m2"] - 1e10) > 1e5


def test_coarse_sheared_class_areas_match_independent_cell_footprints(tmp_path):
    source = tmp_path / "coarse-sheared-classification.tif"
    affine = Affine(90000, 15000, 420000, 20000, -95000, 4550000)
    values = np.array([[7, 7, 2], [7, 255, 2]], dtype="uint8")
    _write_classification(source, values, crs=UTM_CRS, transform=affine)
    report = summarize_threshold_area(source, area_crs=TARGET_CRS)

    assert report["nodata_pixel_count"] == 1
    assert [(item["class_id"], item["pixel_count"]) for item in report["classes"]] == [(2, 2), (7, 3)]
    for item in report["classes"]:
        rows, columns = np.nonzero(values == item["class_id"])
        reference = math.fsum(
            _geodetic_cell_area(affine, row, col, segments=2048)
            for row, col in zip(rows, columns, strict=True)
        )
        assert item["area_m2"] == pytest.approx(reference, rel=1e-8)


def test_nonzero_affine_determinant_does_not_hide_collapsed_projected_cells(tmp_path):
    source = tmp_path / "collapsed-classification.tif"
    affine = Affine(10, 0, 500000, 0, -4.5e-10, 4400000)
    assert affine.determinant != 0
    _write_classification(source, np.full((4, 2), 7, dtype="uint8"), crs=UTM_CRS, transform=affine)

    with pytest.raises(ValueError, match="A projected classification cell has no finite positive area"):
        summarize_threshold_area(source, area_crs=TARGET_CRS)


@pytest.mark.parametrize(("west", "target_crs"), [
    (179.85, "EPSG:6933"), (89.85, "EPSG:8858"), (-30.15, "EPSG:8859"),
])
def test_rejects_classification_across_equal_area_projection_cut(tmp_path, west, target_crs):
    source = tmp_path / "classification-across-cut.tif"
    _write_classification(
        source, np.full((1, 3), 7, dtype="uint8"), crs="EPSG:4326",
        transform=Affine(0.1, 0, west, 0, -0.05, 0.03),
    )

    with pytest.raises(MeasurementPolicyError, match="within a single longitude interval"):
        summarize_threshold_area(source, area_crs=target_crs)


def test_zero_selected_area_keeps_background_and_nodata_counts(tmp_path):
    source = tmp_path / "empty-classification.tif"
    _write_classification(
        source, np.array([[0, 255], [255, 0]], dtype="uint8"), crs=TARGET_CRS,
        transform=Affine(10, 0, 100, 0, -20, 100),
    )

    report = summarize_threshold_area(source, area_crs=TARGET_CRS)

    assert report["classes"] == [] and report["total_area_m2"] == 0.0
    assert report["valid_pixel_count"] == report["unselected_pixel_count"] == report["nodata_pixel_count"] == 2
    assert report["selected_pixel_count"] == 0


def test_rejects_grid_outside_declared_equal_area_coverage(tmp_path):
    source = tmp_path / "polar-classification.tif"
    x, y = Transformer.from_crs(4326, 6933, always_xy=True).transform(0, 87)
    _write_classification(
        source, np.array([[1]], dtype="uint8"), crs=TARGET_CRS, transform=Affine(10, 0, x, 0, -10, y),
    )

    with pytest.raises(MeasurementPolicyError, match="outside the area of use"):
        summarize_threshold_area(source, area_crs=TARGET_CRS)


@pytest.mark.parametrize(("dtype", "nodata"), [("int16", 255), ("uint8", 0)])
def test_rejects_unrecognized_classification_encoding(tmp_path, dtype, nodata):
    source = tmp_path / "wrong-encoding.tif"
    _write_classification(
        source, np.array([[1]], dtype=dtype), crs=TARGET_CRS, transform=Affine(10, 0, 100, 0, -20, 100), nodata=nodata,
    )

    with pytest.raises(ValueError, match="uint8 classification band with NoData 255"):
        summarize_threshold_area(source, area_crs=TARGET_CRS)
