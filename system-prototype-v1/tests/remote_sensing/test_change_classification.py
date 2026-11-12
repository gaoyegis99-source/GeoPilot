import math

import numpy as np
import pytest
import rasterio
from pyproj import Geod, Transformer
from rasterio.transform import Affine

from geopilot.data.fingerprint import fingerprint_file
from geopilot.gis.measurement.coverage import MeasurementPolicyError
from geopilot.remote_sensing.alignment import align_ndvi
from geopilot.remote_sensing.change import NDVI_CHANGE_BAND, write_ndvi_change
from geopilot.remote_sensing.classification import ChangeClassificationError, classify_ndvi_change


EQUAL_AREA = "EPSG:6933"
EQUAL_AREA_GRID = Affine(20, 3, 100, 4, -10, 100)
UTM = "EPSG:32632"
UTM_GRID = Affine(30, 0, 500000, 0, -30, 5100000)
CLASS_NAMES = ("decrease", "stable", "increase")


def _write_raster(path, values, *, crs=EQUAL_AREA, grid=EQUAL_AREA_GRID, band=NDVI_CHANGE_BAND, mask=None, scale=1.0):
    data = np.asarray(values, dtype=np.float64)
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
        dtype="float64", crs=crs, transform=grid, nodata=np.nan,
    ) as dataset:
        dataset.write(data, 1)
        dataset.set_band_description(1, band)
        dataset.set_band_unit(1, "1")
        dataset.scales = (scale,)
        if mask is not None:
            dataset.write_mask(np.array(mask, dtype="uint8") * 255)


def _independent_ellipsoid_area(column):
    vertices = [UTM_GRID * pixel for pixel in [(column, 0), (column + 1, 0), (column + 1, 1), (column, 1)]]
    edges = [np.linspace(a, b, 256, endpoint=False) for a, b in zip(vertices, vertices[1:] + vertices[:1])]
    x, y = np.concatenate(edges).T
    lon, lat = Transformer.from_crs(UTM, 4326, always_xy=True).transform(x, y)
    area, _ = Geod(ellps="WGS84").polygon_area_perimeter(lon, lat)
    return abs(area)


def test_inclusive_stable_boundaries_and_masks_on_rotated_equal_area_grid(tmp_path):
    source, target = tmp_path / "delta.tif", tmp_path / "classes.tif"
    lower, upper = -0.25, 0.25
    _write_raster(source, [
        [np.nextafter(lower, -np.inf), lower, 0, upper, np.nextafter(upper, np.inf)],
        [np.nan, -2, 2, 0.5, -0.5],
    ], mask=[[1, 1, 1, 1, 1], [1, 1, 1, 0, 1]])
    before = fingerprint_file(source)

    result = classify_ndvi_change(source, target, threshold=0.25, area_crs=EQUAL_AREA)

    expected = np.array([[1, 2, 2, 2, 3], [255, 1, 3, 255, 1]])
    with rasterio.open(target) as classes:
        assert classes.transform == EQUAL_AREA_GRID and classes.crs.to_string() == EQUAL_AREA
        assert classes.dtypes == ("uint8",) and classes.nodata == 255
        assert classes.units == ("class",)
        np.testing.assert_array_equal(classes.read(1), expected)
        np.testing.assert_array_equal(classes.read(1, masked=True).mask, expected == 255)
    assert result.metadata.transform == tuple(EQUAL_AREA_GRID)[:6]
    assert result.metadata.nodata == (255,)
    statistics = result.statistics
    assert statistics["threshold"] == 0.25 and statistics["direction"] == "second_minus_first"
    assert statistics["source_crs"] == statistics["area_crs"] == EQUAL_AREA
    assert statistics["area_unit"] == "square_meter" and statistics["resampling"] == "none"
    assert statistics["cell_area_method"] == "affine_determinant"
    assert statistics["valid_pixel_count"] == statistics["selected_pixel_count"] == 8
    assert statistics["unselected_pixel_count"] == 0 and statistics["nodata_pixel_count"] == 2
    assert statistics["classes"] == [
        {"class_id": 1, "class_name": "decrease", "pixel_count": 3, "area_m2": 636.0},
        {"class_id": 2, "class_name": "stable", "pixel_count": 3, "area_m2": 636.0},
        {"class_id": 3, "class_name": "increase", "pixel_count": 2, "area_m2": 424.0},
    ]
    assert statistics["total_area_m2"] == 1696.0
    assert fingerprint_file(source) == before


@pytest.mark.parametrize(("threshold", "values", "expected", "counts"), [
    (0, [-2, -0.0, 0.0, 2, np.nan], [1, 2, 2, 3, 255], [1, 2, 1]),
    (2, [-2, -0.0, 0.0, 2, np.nan], [2, 2, 2, 2, 255], [0, 4, 0]),
    (0.25, [np.nan, np.nan], [255, 255], [0, 0, 0]),
])
def test_threshold_limits_and_zero_class_statistics(tmp_path, threshold, values, expected, counts):
    source, target = tmp_path / "delta.tif", tmp_path / "classes.tif"
    _write_raster(source, [values])

    result = classify_ndvi_change(source, target, threshold=threshold, area_crs=EQUAL_AREA)

    with rasterio.open(target) as classes:
        assert classes.read(1).tolist() == [expected]
    assert result.statistics["classes"] == [
        {"class_id": number, "class_name": name, "pixel_count": count, "area_m2": 212.0 * count}
        for number, (name, count) in enumerate(zip(CLASS_NAMES, counts), start=1)
    ]
    assert result.statistics["total_area_m2"] == 212.0 * sum(counts)
    assert result.statistics["nodata_pixel_count"] == expected.count(255)


def test_actual_change_raster_is_classified_and_original_utm_cells_measured_in_equal_area(tmp_path):
    first, second = tmp_path / "first.tif", tmp_path / "second.tif"
    aligned, source, target = (tmp_path / name for name in ("aligned.tif", "delta.tif", "classes.tif"))
    _write_raster(first, [[0.75, 0.0, -0.25, np.nan]], crs=UTM, grid=UTM_GRID, band="ndvi")
    _write_raster(second, [[0.25, 0.0, 0.25, 0.75]], crs=UTM, grid=UTM_GRID, band="ndvi")
    align_ndvi(first, second, aligned, resampling="nearest")
    write_ndvi_change(aligned, source)
    before = fingerprint_file(source)

    result = classify_ndvi_change(source, target, threshold=0.25, area_crs=EQUAL_AREA)

    with rasterio.open(target) as classes:
        assert classes.crs.to_string() == UTM and classes.transform == UTM_GRID
        assert classes.read(1).tolist() == [[1, 2, 3, 255]]
    stats = result.statistics
    assert stats["cell_area_method"] == "projected_cell_boundary" and stats["resampling"] == "none"
    assert stats["source_crs"] == UTM and stats["area_crs"] == EQUAL_AREA
    assert stats["valid_pixel_count"] == 3 and stats["nodata_pixel_count"] == 1
    independent = [_independent_ellipsoid_area(column) for column in range(3)]
    assert all(900.7 < area < 900.8 for area in independent)
    assert [entry["area_m2"] for entry in stats["classes"]] == pytest.approx(independent, rel=0, abs=1e-5)
    assert stats["total_area_m2"] == pytest.approx(math.fsum(independent), rel=0, abs=3e-5)
    assert fingerprint_file(source) == before


@pytest.mark.parametrize("threshold", [-0.01, 2.01, np.nan, np.inf, True, "0.25"])
def test_invalid_threshold_is_locatable_and_never_publishes(tmp_path, threshold):
    source, target = tmp_path / "delta.tif", tmp_path / "classes.tif"
    _write_raster(source, [[0.0]])

    with pytest.raises(ChangeClassificationError) as failure:
        classify_ndvi_change(source, target, threshold=threshold, area_crs=EQUAL_AREA)

    assert failure.value.field == "parameters.threshold"
    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("source_options", [{"band": "ndvi"}, {"scale": 0.5}])
def test_raw_ndvi_or_scaled_delta_is_not_a_change_input(tmp_path, source_options):
    source, target = tmp_path / "delta.tif", tmp_path / "classes.tif"
    _write_raster(source, [[0.0]], **source_options)

    with pytest.raises(ChangeClassificationError) as failure:
        classify_ndvi_change(source, target, threshold=0.25, area_crs=EQUAL_AREA)

    assert failure.value.field == "inputs.change_raster"
    assert set(tmp_path.iterdir()) == {source}


def test_unsuitable_projection_leaves_no_target_or_temporary_classification(tmp_path):
    source, target = tmp_path / "delta.tif", tmp_path / "classes.tif"
    _write_raster(source, [[0.5]], crs=UTM, grid=UTM_GRID)
    before = fingerprint_file(source)

    with pytest.raises(MeasurementPolicyError):
        classify_ndvi_change(source, target, threshold=0.25, area_crs="EPSG:3857")

    assert set(tmp_path.iterdir()) == {source}
    assert fingerprint_file(source) == before


@pytest.mark.parametrize("existing_name", ["classes.tif", "classes.tif.aux.xml"])
def test_existing_destination_or_companion_is_preserved(tmp_path, existing_name):
    source, target = tmp_path / "delta.tif", tmp_path / "classes.tif"
    _write_raster(source, [[0.5]])
    existing = tmp_path / existing_name
    existing.write_bytes(b"existing data")

    with pytest.raises(FileExistsError):
        classify_ndvi_change(source, target, threshold=0.25, area_crs=EQUAL_AREA)

    assert set(tmp_path.iterdir()) == {source, existing}
    assert existing.read_bytes() == b"existing data"
