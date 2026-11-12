import json
from dataclasses import asdict
from math import floor

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.enums import MaskFlags
from rasterio.errors import NotGeoreferencedWarning
from rasterio.transform import from_origin
from shapely.geometry import Point, Polygon, box

from geopilot.data.fingerprint import fingerprint_file
from geopilot.remote_sensing.alignment import AlignmentInputError, align_ndvi
from geopilot.remote_sensing.indices import write_ndvi
from geopilot.remote_sensing.quality_mask import write_quality_mask
from geopilot.remote_sensing.scaling import scale_product_reflectance
from product_fixtures import LANDSAT, LANDSAT_CODES, SENTINEL, SENTINEL_CODES


CRS = "EPSG:32632"
GRID = from_origin(500000, 5100000, 10, 10)
DOMAIN = "GEOPILOT_NDVI_ALIGNMENT"


def _write_ndvi(path, values, *, crs=CRS, transform=GRID, nodata=np.nan, mask=None, band="ndvi"):
    values = np.asarray(values, dtype="float64")
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        path, "w", driver="GTiff", width=values.shape[1], height=values.shape[0], count=1, dtype="float64",
        crs=crs, transform=transform, nodata=nodata,
    ) as dataset:
        dataset.set_band_description(1, band)
        dataset.set_band_unit(1, "1")
        dataset.write(values, 1)
        if mask is not None:
            dataset.write_mask(np.asarray(mask, dtype=bool))
    return path


def _write_area(path, geometry, *, crs=CRS, layer="study_area"):
    pyogrio.write_dataframe(gpd.GeoDataFrame(geometry=[geometry], crs=crs), path, layer=layer, driver="GPKG")
    return path


def _assert_aligned(path, first, result, expected_first, expected_second, *, tolerance=0):
    expected = np.asarray([expected_first, expected_second], dtype="float64")
    assert np.array_equal(np.isnan(expected[0]), np.isnan(expected[1]))
    with rasterio.open(path) as dataset, rasterio.open(first) as source:
        assert (dataset.crs, dataset.transform, dataset.shape, dataset.res) == (
            source.crs, source.transform, source.shape, source.res,
        )
        assert dataset.descriptions == ("first_ndvi", "second_ndvi") and dataset.units == ("1", "1")
        assert dataset.dtypes == ("float64", "float64") and np.isnan(dataset.nodatavals).all()
        assert dataset.scales == (1.0, 1.0) and dataset.offsets == (0.0, 0.0)
        assert all(MaskFlags.per_dataset in flags for flags in dataset.mask_flag_enums)
        assert dataset.files == [str(path)]
        np.testing.assert_array_equal(dataset.read_masks(), 255 * ~np.isnan(expected))
        np.testing.assert_allclose(dataset.read(), expected, rtol=0, atol=tolerance, equal_nan=True)
        assert json.loads(dataset.tags(ns=DOMAIN)["SUMMARY"]) == json.loads(json.dumps(asdict(result.summary)))
    assert result.metadata.nodata == ("nan", "nan")


def test_same_grid_preserves_endpoints_zero_and_uses_common_source_validity(tmp_path):
    first = _write_ndvi(tmp_path / "first.tif", [[0, 1, -1, 2], [0.25, np.nan, 0.3, 0.4]])
    second = _write_ndvi(
        tmp_path / "second.tif", [[-1, 0, -0.5, 0.3], [np.inf, 0.5, 0.6, 0.4]],
        nodata=-0.5, mask=[[True] * 4, [True, True, False, True]],
    )
    before = tuple(map(fingerprint_file, (first, second)))
    target = tmp_path / "aligned.tif"

    result = align_ndvi(first, second, target, resampling="bilinear")

    _assert_aligned(
        target, first, result, [[0, 1, np.nan, np.nan], [np.nan, np.nan, np.nan, 0.4]],
        [[-1, 0, np.nan, np.nan], [np.nan, np.nan, np.nan, 0.4]],
    )
    summary = result.summary
    assert summary.resampling == "bilinear" and summary.warped is False
    assert (summary.analysis_pixels, summary.first_valid_pixels, summary.second_valid_pixels, summary.common_valid_pixels) == (8, 6, 5, 3)
    assert (summary.first_range, summary.second_range) == ((0, 1), (-1, 0.4))
    assert (summary.first_grid.nodata, summary.second_grid.nodata, summary.target_grid.nodata) == ("nan", -0.5, "nan")
    assert tuple(map(fingerprint_file, (first, second))) == before


def test_same_dimensions_with_shifted_origin_use_real_nearest_and_gdal_mask(tmp_path):
    values = np.arange(12, dtype="float64").reshape(3, 4) / 16
    first = _write_ndvi(tmp_path / "first.tif", np.full(values.shape, 0.25))
    mask = np.ones(values.shape, dtype=bool)
    mask[1, 1] = False
    shifted = GRID * Affine.translation(1, 0)
    second = _write_ndvi(tmp_path / "second.tif", values, transform=shifted, mask=mask)
    target = tmp_path / "aligned.tif"

    result = align_ndvi(first, second, target, resampling="nearest")

    expected = np.full(values.shape, np.nan)
    expected[:, 1:] = values[:, :3]
    expected[1, 2] = np.nan
    _assert_aligned(target, first, result, np.where(np.isfinite(expected), 0.25, np.nan), expected)
    assert result.summary.warped and result.summary.second_grid.transform == tuple(shifted)[:6]
    assert (result.summary.first_valid_pixels, result.summary.second_valid_pixels, result.summary.common_valid_pixels) == (12, 8, 8)


def test_cross_crs_and_resolution_match_independent_target_center_lookup(tmp_path):
    first = _write_ndvi(tmp_path / "first.tif", np.full((3, 4), -0.25))
    second_crs = "EPSG:32631"
    project = Transformer.from_crs(CRS, second_crs, always_xy=True)
    x0, y0 = project.transform(*(GRID * (0, 0)))
    second_transform = from_origin(x0 - 4, y0 + 4, 8, 8)
    values = np.arange(36, dtype="float64").reshape(6, 6) / 36
    values[2, 2] = np.nan
    second = _write_ndvi(tmp_path / "second.tif", values, crs=second_crs, transform=second_transform)
    target = tmp_path / "aligned.tif"

    result = align_ndvi(first, second, target, resampling="nearest")

    expected = np.empty((3, 4), dtype="float64")
    for row, column in np.ndindex(expected.shape):
        x, y = project.transform(*(GRID * (column + 0.5, row + 0.5)))
        source_col, source_row = (~second_transform) * (x, y)
        expected[row, column] = values[floor(source_row), floor(source_col)]
    _assert_aligned(target, first, result, np.where(np.isfinite(expected), -0.25, np.nan), expected)
    summary = result.summary
    assert (summary.first_grid.crs, summary.second_grid.crs, summary.target_grid.crs) == (CRS, second_crs, CRS)
    assert (summary.first_grid.resolution, summary.second_grid.resolution, summary.target_grid.resolution) == ((10, 10), (8, 8), (10, 10))
    assert summary.common_valid_pixels == 11 and summary.warped


def test_bilinear_removes_outlier_before_it_contaminates_valid_neighbor(tmp_path):
    raw = [[0, 0.5, 1, -0.5], [0, 0.5, 2, -0.5], [0, 0.5, 1, -0.5], [0, 0.5, 1, -0.5]]
    first = _write_ndvi(tmp_path / "first.tif", np.full((2, 3), 0.25), transform=GRID * Affine.translation(0.75, 1))
    second = _write_ndvi(tmp_path / "second.tif", raw)
    target = tmp_path / "aligned.tif"

    result = align_ndvi(first, second, target, resampling="bilinear")

    # A 3:1 / 1:3 weighted mix of valid samples. The bad center is invalid, and
    # the next center uses only -0.5. Mixing the raw 2 would instead yield 0.125.
    expected = np.array([[0.375, np.nan, -0.5], [0.375, 0.875, -0.125]])
    _assert_aligned(target, first, result, np.where(np.isfinite(expected), 0.25, np.nan), expected, tolerance=1e-12)
    assert result.summary.common_valid_pixels == 5 and result.summary.warped
    assert result.summary.second_range == pytest.approx((-0.5, 0.875), abs=1e-12)


def test_selected_polygon_layer_includes_boundaries_and_excludes_hole_interior(tmp_path):
    first = _write_ndvi(tmp_path / "first.tif", np.full((4, 4), 0.25))
    second = _write_ndvi(tmp_path / "second.tif", np.full((4, 4), 0.75))
    shell = box(500005, 5099975, 500035, 5099995)
    hole = box(500011, 5099981, 500025, 5099989)
    polygon = Polygon(shell.exterior.coords, [hole.exterior.coords])
    extent = _write_area(tmp_path / "extent.gpkg", box(501000, 5101000, 501010, 5101010), layer="other")
    _write_area(extent, polygon)
    before = fingerprint_file(extent)
    target = tmp_path / "aligned.tif"

    result = align_ndvi(first, second, target, resampling="nearest", analysis_extent=extent, analysis_layer="study_area")

    covered = np.array([[True] * 4, [True, False, True, True], [True] * 4, [False] * 4])
    _assert_aligned(target, first, result, np.where(covered, 0.25, np.nan), np.where(covered, 0.75, np.nan))
    assert result.summary.analysis_pixels == result.summary.common_valid_pixels == 11
    assert result.summary.analysis_bounds == polygon.bounds
    assert fingerprint_file(extent) == before


def test_reprojects_actual_polygon_to_first_crs(tmp_path):
    first = _write_ndvi(tmp_path / "first.tif", np.full((3, 4), -0.5))
    second = _write_ndvi(tmp_path / "second.tif", np.full((3, 4), 0.5))
    polygon = box(500010, 5099980, 500030, 5100000)
    geographic = gpd.GeoSeries([polygon], crs=CRS).to_crs("EPSG:4326").iloc[0]
    extent = _write_area(tmp_path / "extent.gpkg", geographic, crs="EPSG:4326")
    target = tmp_path / "aligned.tif"

    result = align_ndvi(first, second, target, resampling="nearest", analysis_extent=str(extent))

    covered = np.array([[False, True, True, False], [False, True, True, False], [False] * 4])
    _assert_aligned(target, first, result, np.where(covered, -0.5, np.nan), np.where(covered, 0.5, np.nan))
    assert result.summary.analysis_pixels == result.summary.common_valid_pixels == 4
    assert result.summary.analysis_bounds == pytest.approx(polygon.bounds, rel=0, abs=1e-8)


def test_uses_both_rotation_terms_for_center_coverage(tmp_path):
    rotated = Affine(10, 2, 500000, 1, -10, 5100000)
    first = _write_ndvi(tmp_path / "first.tif", [[0.25, 0.25], [0.25, 0.25]], transform=rotated)
    second = _write_ndvi(tmp_path / "second.tif", [[0.75, 0.75], [0.75, 0.75]], transform=rotated)
    extent = _write_area(tmp_path / "extent.gpkg", box(500007.5, 5099985, 500008.5, 5099986))
    target = tmp_path / "aligned.tif"

    result = align_ndvi(first, second, target, resampling="nearest", analysis_extent=extent)

    _assert_aligned(target, first, result, [[np.nan, np.nan], [0.25, np.nan]], [[np.nan, np.nan], [0.75, np.nan]])
    assert result.summary.analysis_pixels == 1


def test_disjoint_inputs_emit_full_first_grid_with_no_common_pixels(tmp_path):
    first = _write_ndvi(tmp_path / "first.tif", np.full((2, 2), -0.25))
    second = _write_ndvi(tmp_path / "second.tif", np.full((2, 2), 0.75), transform=GRID * Affine.translation(100, 0))
    target = tmp_path / "aligned.tif"

    result = align_ndvi(first, second, target, resampling="bilinear")

    nodata = np.full((2, 2), np.nan)
    _assert_aligned(target, first, result, nodata, nodata)
    summary = result.summary
    assert (summary.analysis_pixels, summary.first_valid_pixels, summary.second_valid_pixels, summary.common_valid_pixels) == (4, 4, 0, 0)
    assert (summary.first_range, summary.second_range) == (None, None)


def test_aligns_actual_landsat_and_sentinel_ndvi_outputs_and_quality(tmp_path, sensor_registry, landsat_product_xml,
                                                            sentinel_product_xml, write_product_stack):
    landsat = write_product_stack(
        tmp_path / "landsat.tif", profile=LANDSAT, xml=landsat_product_xml(), codes=LANDSAT_CODES, pixel_m=30,
        values=[[[0, 8], [0, 0]], [[20000] * 2] * 2, [[0] * 2] * 2, [[10000] * 2] * 2],
    )
    scl = np.full((6, 6), 4, dtype="uint16")
    scl[4, 1] = 9
    sentinel = write_product_stack(
        tmp_path / "sentinel.tif", profile=SENTINEL, xml=sentinel_product_xml(), codes=SENTINEL_CODES, pixel_m=10,
        values=[scl, np.full(scl.shape, 7000), np.full(scl.shape, 3000)], scl_resampling="nearest",
    )
    ndvi_files = []
    for product in (landsat, sentinel):
        reflectance, quality, ndvi = (tmp_path / f"{product.stem}-{part}.tif" for part in ("reflectance", "quality", "ndvi"))
        scale_product_reflectance(product, reflectance, registry=sensor_registry)
        write_quality_mask(product, quality, registry=sensor_registry)
        write_ndvi(reflectance, quality, ndvi)
        ndvi_files.append(ndvi)
    first, second = ndvi_files
    target = tmp_path / "aligned.tif"

    result = align_ndvi(first, second, target, resampling="nearest")

    common = np.array([[True, False], [False, True]])
    _assert_aligned(target, first, result, np.where(common, 11 / 17, np.nan), np.where(common, 0.5, np.nan), tolerance=1e-15)
    summary = result.summary
    assert (summary.first_valid_pixels, summary.second_valid_pixels, summary.common_valid_pixels) == (3, 3, 2)
    assert summary.first_grid.resolution == (30, 30) and summary.second_grid.resolution == (10, 10)


def test_reopens_real_source_tiles_and_has_no_resampling_seam(tmp_path, monkeypatch):
    rows, columns = np.indices((259, 261))
    values = ((rows + columns) % 17 - 8).astype("float64") / 8
    first = _write_ndvi(tmp_path / "first.tif", np.zeros(values.shape))
    second = _write_ndvi(tmp_path / "second.tif", values, transform=GRID * Affine.translation(1, 0))
    target = tmp_path / "aligned.tif"
    actual_open = rasterio.open
    windows = {first: [], second: []}

    def track_reads(path, *args, **kwargs):
        dataset = actual_open(path, *args, **kwargs)
        if path in windows:
            read = dataset.read

            def record_read(*args, **kwargs):
                windows[path].append(kwargs.get("window"))
                return read(*args, **kwargs)

            dataset.read = record_read
        return dataset

    with monkeypatch.context() as patch:
        patch.setattr(rasterio, "open", track_reads)
        result = align_ndvi(first, second, target, resampling="nearest")

    for observed in windows.values():
        assert len(observed) == 4 and all(window is not None and max(window.width, window.height) <= 256 for window in observed)
    expected = np.full(values.shape, np.nan)
    expected[:, 1:] = values[:, :-1]
    _assert_aligned(target, first, result, np.where(np.isfinite(expected), 0, np.nan), expected)


@pytest.mark.parametrize("field", ["first.crs", "second", "resampling", "analysis_layer", "analysis_extent"])
def test_rejects_unknown_grid_wrong_stage_and_invalid_options(tmp_path, field):
    first = _write_ndvi(tmp_path / "first.tif", [[0.25]], crs=None if field == "first.crs" else CRS)
    second = _write_ndvi(tmp_path / "second.tif", [[0.75]], band="nir" if field == "second" else "ndvi")
    target = tmp_path / "aligned.tif"
    options = {"resampling": "nearest"}
    if field == "resampling":
        options["resampling"] = "cubic"
    elif field == "analysis_layer":
        options["analysis_layer"] = "study_area"
    elif field == "analysis_extent":
        options["analysis_extent"] = _write_area(tmp_path / "extent.gpkg", Point(500005, 5099995))

    with pytest.raises(AlignmentInputError) as failure:
        align_ndvi(first, second, target, **options)
    assert failure.value.field == field and not target.exists()


def test_rejects_default_identity_when_geotiff_contains_no_affine(tmp_path):
    with pytest.warns(NotGeoreferencedWarning):
        first = _write_ndvi(tmp_path / "first.tif", [[0.25]], transform=None)
    second = _write_ndvi(tmp_path / "second.tif", [[0.75]])
    target = tmp_path / "aligned.tif"

    with pytest.raises(AlignmentInputError) as failure:
        align_ndvi(first, second, target, resampling="nearest")
    assert failure.value.field == "first.transform" and not target.exists()


@pytest.mark.parametrize("existing_name", ["aligned.tif", "aligned.tif.aux.xml"])
def test_preserves_existing_destination_and_sidecar(tmp_path, existing_name):
    first = _write_ndvi(tmp_path / "first.tif", [[0.25]])
    second = _write_ndvi(tmp_path / "second.tif", [[0.75]])
    existing = tmp_path / existing_name
    existing.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        align_ndvi(first, second, tmp_path / "aligned.tif", resampling="nearest")
    assert existing.read_bytes() == b"preserve" and set(tmp_path.iterdir()) == {first, second, existing}
