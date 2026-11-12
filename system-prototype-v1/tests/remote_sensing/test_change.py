import json
import math
from dataclasses import asdict

import numpy as np
import pytest
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.transform import from_origin

from geopilot.data.fingerprint import fingerprint_file
from geopilot.remote_sensing.alignment import align_ndvi
from geopilot.remote_sensing.change import NdviChangeInputError, write_ndvi_change


CRS = "EPSG:32632"
GRID = from_origin(500000, 5100000, 10, 10)
ALIGNED_BANDS = ("first_ndvi", "second_ndvi")


def _write_bands(path, values, *, descriptions, transform=GRID, mask=None, nodata=np.nan, units="1", crs=CRS):
    values = np.asarray(values, dtype="float64")
    count, height, width = values.shape
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        path, "w", driver="GTiff", width=width, height=height, count=count, dtype="float64",
        crs=crs, transform=transform, nodata=nodata,
    ) as raster:
        raster.descriptions = descriptions
        raster.units = (units,) * count
        raster.write(values)
        if mask is not None:
            raster.write_mask(np.asarray(mask, dtype=bool))
    return path


def _write_ndvi(path, values, **options):
    return _write_bands(path, [values], descriptions=("ndvi",), **options)


def _assert_change(path, aligned, expected, result):
    expected = np.asarray(expected, dtype="float64")
    with rasterio.open(path) as raster, rasterio.open(aligned) as source:
        assert (raster.crs, raster.transform, raster.shape, raster.res) == (
            source.crs, source.transform, source.shape, source.res,
        )
        assert raster.descriptions == ("ndvi_change",) and raster.units == ("1",)
        assert raster.dtypes == ("float64",) and math.isnan(raster.nodata)
        assert raster.scales == (1.0,) and raster.offsets == (0.0,)
        np.testing.assert_array_equal(raster.read(1), expected)
        np.testing.assert_array_equal(raster.read_masks(1), 255 * ~np.isnan(expected))
        np.testing.assert_array_equal(raster.read_masks(1), source.read_masks(1))
        assert json.loads(raster.tags(ns="GEOPILOT_NDVI_CHANGE")["SUMMARY"]) == asdict(result.summary)
        assert raster.files == [str(path)]
    assert result.metadata.band_names == ("ndvi_change",) and result.metadata.nodata == ("nan",)


def test_real_shifted_alignment_yields_second_minus_first_and_valid_zero(tmp_path):
    first = _write_ndvi(
        tmp_path / "first.tif", [[0.125, 0.25, -0.5, 0.75], [-0.125, 0.375, np.nan, -0.5], [0, -1, 1, 0]],
    )
    second = _write_ndvi(
        tmp_path / "second.tif", [[0.75, -0.75, 0.75, 0.875], [np.nan, 0.125, 0.25, 0.875], [1, -1, -0.0, 0.875]],
        transform=from_origin(500010, 5100000, 10, 10),
    )
    original_fingerprints = tuple(map(fingerprint_file, (first, second)))
    aligned = tmp_path / "aligned.tif"
    alignment = align_ndvi(first, second, aligned, resampling="nearest")
    aligned_fingerprint = fingerprint_file(aligned)
    target = tmp_path / "change.tif"

    result = write_ndvi_change(aligned, target)

    expected = [[np.nan, 0.5, -0.25, 0], [np.nan, np.nan, np.nan, 0.75], [np.nan, 2, -2, -0.0]]
    _assert_change(target, aligned, expected, result)
    assert alignment.summary.warped and alignment.summary.common_valid_pixels == 7
    assert asdict(result.summary) == {
        "direction": "second_minus_first", "total_pixels": 12, "common_valid_pixels": 7, "nodata_pixels": 5,
        "positive_pixels": 3, "negative_pixels": 2, "zero_pixels": 2, "minimum": -2.0, "maximum": 2.0,
    }
    with rasterio.open(target) as raster:
        values = raster.read(1, masked=True)
        assert values[0, 3] == 0.0 and values[2, 3] == 0.0
    assert tuple(map(fingerprint_file, (first, second))) == original_fingerprints
    assert fingerprint_file(aligned) == aligned_fingerprint


def test_disjoint_real_alignment_preserves_grid_and_all_nodata(tmp_path):
    first = _write_ndvi(tmp_path / "first.tif", [[0.0, 0.5]])
    second = _write_ndvi(tmp_path / "second.tif", [[0.5, 0.75]], transform=from_origin(501000, 5100000, 10, 10))
    aligned = tmp_path / "aligned.tif"
    alignment = align_ndvi(first, second, aligned, resampling="nearest")
    target = tmp_path / "change.tif"

    result = write_ndvi_change(aligned, target)

    _assert_change(target, aligned, [[np.nan, np.nan]], result)
    assert alignment.summary.common_valid_pixels == result.summary.common_valid_pixels == 0
    assert result.summary.nodata_pixels == result.summary.total_pixels == 2
    assert (result.summary.positive_pixels, result.summary.negative_pixels, result.summary.zero_pixels) == (0, 0, 0)
    assert (result.summary.minimum, result.summary.maximum) == (None, None)


def test_reopened_change_matches_reference_across_four_gdal_blocks(tmp_path):
    shape = (259, 261)
    first_values = np.full(shape, 0.25)
    second_values = np.full(shape, 0.75)
    first_values[255, 255], second_values[255, 255] = 0.75, 0.25
    first_values[256, 256], second_values[256, 256] = 0.0, -0.0
    first_values[258, 260] = np.nan
    second_values[12, 258] = np.nan
    first = _write_ndvi(tmp_path / "first.tif", first_values)
    second = _write_ndvi(tmp_path / "second.tif", second_values)
    aligned = tmp_path / "aligned.tif"
    align_ndvi(first, second, aligned, resampling="nearest")
    target = tmp_path / "change.tif"

    result = write_ndvi_change(aligned, target)

    expected = np.full(shape, 0.5)
    expected[255, 255] = -0.5
    expected[256, 256] = -0.0
    expected[258, 260] = expected[12, 258] = np.nan
    _assert_change(target, aligned, expected, result)
    with rasterio.open(target) as raster:
        assert raster.block_shapes == [(256, 256)] and len(list(raster.block_windows(1))) == 4
        assert np.signbit(raster.read(1)[256, 256])
    total = shape[0] * shape[1]
    summary = result.summary
    assert (summary.common_valid_pixels, summary.nodata_pixels) == (total - 2, 2)
    assert (summary.positive_pixels, summary.negative_pixels, summary.zero_pixels) == (total - 4, 1, 1)
    assert (summary.minimum, summary.maximum) == (-0.5, 0.5)


@pytest.mark.parametrize(("values", "mask"), [
    ([[[np.nan]], [[0.5]]], [[True]]),
    ([[[0.25]], [[0.5]]], [[False]]),
    ([[[np.nan]], [[0.5]]], None),
])
def test_rejects_inconsistent_common_mask_or_raw_nodata(tmp_path, values, mask):
    source = _write_bands(tmp_path / "broken.tif", values, descriptions=ALIGNED_BANDS, mask=mask)
    before = fingerprint_file(source)
    target = tmp_path / "change.tif"

    with pytest.raises(NdviChangeInputError) as failure:
        write_ndvi_change(source, target)

    assert (failure.value.code, failure.value.field) == ("NDVI_CHANGE_INPUT_INVALID", "inputs.aligned.mask")
    assert "row 0, column 0" in str(failure.value)
    assert fingerprint_file(source) == before and set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize("value", [-1.00001, 1.00001, np.inf])
def test_rejects_visible_out_of_range_values_in_aligned_input(tmp_path, value):
    source = _write_bands(tmp_path / "broken.tif", [[[value]], [[0.5]]], descriptions=ALIGNED_BANDS, mask=[[True]])
    target = tmp_path / "change.tif"

    with pytest.raises(NdviChangeInputError) as failure:
        write_ndvi_change(source, target)

    assert failure.value.field == "inputs.aligned.values"
    assert not target.exists()


def test_late_mask_mismatch_cleans_staged_output(tmp_path):
    shape = (259, 261)
    values = np.full((2, *shape), 0.5)
    values[1, -1, -1] = np.nan
    source = _write_bands(tmp_path / "broken.tif", values, descriptions=ALIGNED_BANDS, mask=np.ones(shape, dtype=bool))
    target = tmp_path / "change.tif"

    with pytest.raises(NdviChangeInputError, match="row 258, column 260"):
        write_ndvi_change(source, target)

    assert set(tmp_path.iterdir()) == {source}


@pytest.mark.parametrize(("options", "field"), [
    ({"descriptions": ("second_ndvi", "first_ndvi")}, "inputs.aligned"),
    ({"nodata": -9999}, "inputs.aligned"),
    ({"units": "reflectance"}, "inputs.aligned"),
    ({"crs": None}, "inputs.aligned.crs"),
])
def test_rejects_invalid_aligned_schema(tmp_path, options, field):
    settings = {"descriptions": ALIGNED_BANDS, "mask": [[True]]} | options
    source = _write_bands(tmp_path / "broken.tif", [[[0.25]], [[0.75]]], **settings)

    with pytest.raises(NdviChangeInputError) as failure:
        write_ndvi_change(source, tmp_path / "change.tif")

    assert failure.value.field == field


def test_rejects_known_crs_without_stored_affine(tmp_path):
    with pytest.warns(NotGeoreferencedWarning):
        source = _write_bands(
            tmp_path / "broken.tif", [[[0.25]], [[0.75]]], descriptions=ALIGNED_BANDS, transform=None,
        )

    with pytest.raises(NdviChangeInputError) as failure:
        write_ndvi_change(source, tmp_path / "change.tif")

    assert failure.value.field == "inputs.aligned.transform"


def test_source_and_existing_destination_companions_are_not_replaced(tmp_path):
    source = _write_bands(tmp_path / "aligned.tif", [[[0.25]], [[0.75]]], descriptions=ALIGNED_BANDS, mask=[[True]])
    source_fingerprint = fingerprint_file(source)
    target = tmp_path / "change.tif"
    companion = tmp_path / "change.tif.aux.xml"
    companion.write_bytes(b"existing companion")

    for collision in (source, target):
        with pytest.raises(FileExistsError):
            write_ndvi_change(source, collision)

    assert fingerprint_file(source) == source_fingerprint and not target.exists()
    assert companion.read_bytes() == b"existing companion"
