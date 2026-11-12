from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
import json
import math

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from geopilot.data.fingerprint import fingerprint_file
from geopilot.remote_sensing.indices import NdviInputError, write_ndvi
from geopilot.remote_sensing.quality_mask import write_quality_mask
from geopilot.remote_sensing.scaling import scale_product_reflectance
from product_fixtures import LANDSAT, LANDSAT_CODES, SENTINEL, SENTINEL_CODES


CRS = "EPSG:32632"
TRANSFORM = from_origin(500000, 5100000, 10, 10)
RAW_RASTER = Path(__file__).resolve().parents[1] / "fixtures" / "raster" / "two-band.tif"


def _prepare_inputs(source, directory, registry):
    reflectance = directory / "reflectance.tif"
    mask = directory / "quality-mask.tif"
    scale_product_reflectance(source, reflectance, registry=registry)
    write_quality_mask(source, mask, registry=registry)
    return reflectance, mask


def _write_reflectance(path, values, *, crs=CRS, transform=TRANSFORM, shared_mask=None, units="reflectance"):
    values = np.asarray(values, dtype=np.float64)
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        path, "w", driver="GTiff", height=values.shape[1], width=values.shape[2], count=2, dtype="float64",
        crs=crs, transform=transform, nodata=np.nan,
    ) as dataset:
        dataset.descriptions = ("red", "nir")
        dataset.units = (units, units)
        dataset.write(values)
        if shared_mask is not None:
            dataset.write_mask(np.asarray(shared_mask, dtype=bool))
    return path


def _write_mask(path, values, *, crs=CRS, transform=TRANSFORM, nodata=None, shared_mask=None):
    values = np.asarray(values, dtype="uint8")
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1], count=1, dtype="uint8",
        crs=crs, transform=transform, nodata=nodata,
    ) as dataset:
        dataset.set_band_description(1, "quality_mask")
        dataset.write(values, 1)
        if shared_mask is not None:
            dataset.write_mask(np.asarray(shared_mask, dtype=bool))
    return path


def _assert_raster(output, reflectance, result, expected, *, tolerance=0):
    with rasterio.open(output) as dataset, rasterio.open(reflectance) as source:
        assert (dataset.crs, dataset.transform, dataset.shape) == (source.crs, source.transform, source.shape)
        assert dataset.descriptions == ("ndvi",) and dataset.dtypes == ("float64",) and dataset.units == ("1",)
        assert math.isnan(dataset.nodata) and dataset.scales == (1.0,) and dataset.offsets == (0.0,)
        actual = dataset.read(1, masked=True)
        np.testing.assert_array_equal(np.ma.getmaskarray(actual), np.isnan(expected))
        np.testing.assert_allclose(actual.filled(np.nan), expected, rtol=0, atol=tolerance, equal_nan=True)
        assert json.loads(dataset.tags(ns="GEOPILOT_NDVI")["SUMMARY"]) == asdict(result.summary)
    assert result.metadata.band_count == 1 and result.metadata.nodata == ("nan",)


def test_sentinel_real_dn_chain_strict_zero_denominator_bounds_and_mask_propagation(
    tmp_path, sensor_registry, sentinel_product_xml, write_product_stack,
):
    red = [[800, 800, 1300, 1000], [1300, 1500, 2500, 1000], [0, 1500, 1500, 1500]]
    nir = [[1200, 1300, 800, 1300], [1000, 2500, 1500, 1000], [2500, 0, 2500, 2500]]
    scl = [[4, 4, 5, 6], [4, 5, 6, 4], [4, 4, 9, 4]]
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        source = write_product_stack(
            tmp_path / "sentinel.tif", profile=SENTINEL, xml=sentinel_product_xml(), codes=SENTINEL_CODES,
            pixel_m=10, scl_resampling="nearest", values=[scl, nir, red],
            shared_mask=np.array([[True] * 4, [True] * 4, [True, True, True, False]]),
        )
    raw_fingerprint = fingerprint_file(source)
    reflectance, quality = _prepare_inputs(source, tmp_path, sensor_registry)
    fingerprints = tuple(map(fingerprint_file, (reflectance, quality)))
    with rasterio.open(reflectance) as dataset, rasterio.open(quality) as mask:
        scaled_red, scaled_nir = dataset.read()[:, 0, 0]
        assert (scaled_red, scaled_nir) == (-0.02, 0.02) and scaled_red + scaled_nir == 0
        np.testing.assert_array_equal(mask.read(1), [[1] * 4, [1] * 4, [0] * 4])

    target = tmp_path / "ndvi.tif"
    result = write_ndvi(reflectance, quality, target)

    expected = np.array([[np.nan, 5, -5, 1], [-1, 0.5, -0.5, np.nan], [np.nan] * 4])
    _assert_raster(target, reflectance, result, expected, tolerance=2e-15)
    summary = result.summary
    assert (summary.total_pixels, summary.quality_invalid_pixels, summary.reflectance_invalid_pixels) == (12, 4, 0)
    assert (summary.denominator_zero_pixels, summary.arithmetic_invalid_pixels) == (2, 0)
    assert (summary.valid_pixels, summary.valid_fraction, summary.out_of_range_pixels) == (6, 0.5, 2)
    assert (summary.minimum, summary.maximum) == pytest.approx((-5, 5))
    assert (summary.out_of_range_minimum, summary.out_of_range_maximum) == pytest.approx((-5, 5))
    first = summary.first_out_of_range
    assert (first.row, first.column, first.value) == pytest.approx((0, 1, 5))
    assert fingerprint_file(source) == raw_fingerprint
    assert tuple(map(fingerprint_file, (reflectance, quality))) == fingerprints


def test_landsat_real_offset_chain_differs_from_raw_dn_and_preserves_valid_ndvi_zero(
    tmp_path, sensor_registry, landsat_product_xml, write_product_stack,
):
    red = [[8000, 10000, 12000, 8000], [8000, 8000, 8000, 0]]
    nir = [[12000, 10000, 8000, 12000], [12000, 12000, 0, 12000]]
    qa = [[0, 0, 0, 8], [0, 0, 0, 0]]
    saturation = [[0, 0, 0, 0], [16, 0, 0, 0]]
    source = write_product_stack(
        tmp_path / "landsat.tif", profile=LANDSAT, xml=landsat_product_xml(), codes=LANDSAT_CODES, pixel_m=30,
        values=[qa, nir, saturation, red], shared_mask=np.array([[True] * 4, [True, False, True, True]]),
    )
    reflectance, quality = _prepare_inputs(source, tmp_path, sensor_registry)
    target = tmp_path / "ndvi.tif"

    result = write_ndvi(reflectance, quality, target)

    hand_value = float(Fraction(11, 15))
    expected = np.array([[hand_value, 0, -hand_value, np.nan], [np.nan] * 4])
    _assert_raster(target, reflectance, result, expected, tolerance=1e-15)
    summary = result.summary
    assert (summary.quality_invalid_pixels, summary.denominator_zero_pixels, summary.valid_pixels) == (5, 0, 3)
    assert summary.valid_fraction == 3 / 8 and summary.out_of_range_pixels == 0
    assert (summary.out_of_range_minimum, summary.out_of_range_maximum, summary.first_out_of_range) == (None, None, None)


def test_distinguishes_input_invalidity_exact_zero_and_arithmetic_overflow(tmp_path):
    maximum = np.finfo(np.float64).max
    near_zero = np.nextafter(-0.02, 0)
    red = [[np.nan, 0.2, 0.1, maximum, -maximum, 0.2, 0.0, near_zero, 0.25]]
    nir = [[0.2, np.inf, 0.3, maximum, maximum / 2, 0.4, -0.0, 0.02, 0.75]]
    reflectance = _write_reflectance(
        tmp_path / "reflectance.tif", [red, nir], shared_mask=[[True, True, False, True, True, True, True, True, True]],
    )
    quality = _write_mask(
        tmp_path / "quality.tif", [[1] * 9], shared_mask=[[True, True, True, True, True, False, True, True, True]],
    )
    target = tmp_path / "ndvi.tif"

    result = write_ndvi(reflectance, quality, target)

    r, n = Fraction.from_float(float(near_zero)), Fraction.from_float(0.02)
    expected_outlier = float((n - r) / (n + r))
    with rasterio.open(target) as dataset:
        values = dataset.read(1, masked=True)
        assert values.mask.tolist() == [[True] * 7 + [False, False]]
        assert values[0, 7] == pytest.approx(expected_outlier, rel=1e-15)
        assert values[0, 8] == 0.5
    summary = result.summary
    assert (summary.quality_invalid_pixels, summary.reflectance_invalid_pixels) == (1, 3)
    assert (summary.denominator_zero_pixels, summary.arithmetic_invalid_pixels, summary.valid_pixels) == (1, 2, 2)
    assert summary.out_of_range_pixels == 1 and summary.valid_fraction == 2 / 9
    assert (summary.first_out_of_range.row, summary.first_out_of_range.column) == (0, 7)


def test_writes_all_blocks_and_reports_global_first_range_anomaly(tmp_path):
    shape = (259, 261)
    red = np.full(shape, 0.25)
    nir = np.full(shape, 0.75)
    red[12, 258], nir[12, 258] = -0.25, 0.5
    red[255, 3], nir[255, 3] = 0.5, -0.25
    red[258, 260] = -0.25
    reflectance = _write_reflectance(tmp_path / "reflectance.tif", [red, nir])
    quality = _write_mask(tmp_path / "quality.tif", np.ones(shape, dtype="uint8"))
    target = tmp_path / "ndvi.tif"

    result = write_ndvi(reflectance, quality, target)

    expected = np.full(shape, 0.5)
    expected[12, 258] = 3
    expected[255, 3] = -3
    expected[258, 260] = 2
    _assert_raster(target, reflectance, result, expected)
    with rasterio.open(target) as dataset:
        assert dataset.block_shapes == [(256, 256)] and len(list(dataset.block_windows(1))) == 4
    summary = result.summary
    assert summary.valid_pixels == summary.total_pixels == shape[0] * shape[1]
    assert summary.out_of_range_pixels == 3
    assert (summary.out_of_range_minimum, summary.out_of_range_maximum) == (-3, 3)
    assert (summary.first_out_of_range.row, summary.first_out_of_range.column, summary.first_out_of_range.value) == (12, 258, 3)


@pytest.mark.parametrize(("values", "kwargs", "field"), [
    ([[1, 1]], {"transform": from_origin(500005, 5100000, 10, 10)}, "inputs.quality_mask.transform"),
    ([[1, 1]], {"crs": "EPSG:32633"}, "inputs.quality_mask.crs"),
    ([[1, 1]], {"crs": None}, "inputs.quality_mask.crs"),
    ([[1]], {}, "inputs.quality_mask.shape"),
    ([[0, 1]], {"nodata": 0}, "inputs.quality_mask"),
])
def test_rejects_shifted_grids_and_malformed_mask_contract(tmp_path, values, kwargs, field):
    reflectance = _write_reflectance(tmp_path / "reflectance.tif", [[[0.2, 0.4]], [[0.6, 0.8]]])
    quality = _write_mask(tmp_path / "quality.tif", values, **kwargs)
    target = tmp_path / "ndvi.tif"

    with pytest.raises(NdviInputError) as failure:
        write_ndvi(reflectance, quality, target)

    assert (failure.value.code, failure.value.field) == ("NDVI_INPUT_INVALID", field)
    assert not target.exists()


def test_late_non_binary_mask_value_never_publishes_partial_result(tmp_path):
    shape = (259, 261)
    reflectance = _write_reflectance(tmp_path / "reflectance.tif", [np.full(shape, 0.25), np.full(shape, 0.75)])
    values = np.ones(shape, dtype="uint8")
    values[-1, -1] = 2
    quality = _write_mask(tmp_path / "quality.tif", values)
    target = tmp_path / "ndvi.tif"

    with pytest.raises(NdviInputError) as failure:
        write_ndvi(reflectance, quality, target)

    assert failure.value.field == "inputs.quality_mask.values"
    assert set(tmp_path.iterdir()) == {reflectance, quality}


def test_refuses_real_raw_dn_raster_with_red_nir_descriptions(tmp_path):
    with rasterio.open(RAW_RASTER) as dataset:
        quality = _write_mask(tmp_path / "quality.tif", np.ones(dataset.shape, dtype="uint8"),
                              crs=dataset.crs, transform=dataset.transform)
    target = tmp_path / "ndvi.tif"

    with pytest.raises(NdviInputError) as failure:
        write_ndvi(RAW_RASTER, quality, target)

    assert failure.value.field == "inputs.reflectance" and not target.exists()


def test_rejects_float_samples_declared_in_wrong_units(tmp_path):
    reflectance = _write_reflectance(tmp_path / "mislabelled.tif", [[[0.2]], [[0.6]]], units="DN")
    quality = _write_mask(tmp_path / "quality.tif", [[1]])

    with pytest.raises(NdviInputError) as failure:
        write_ndvi(reflectance, quality, tmp_path / "ndvi.tif")

    assert failure.value.field == "inputs.reflectance"


def test_all_invalid_yields_nan_and_null_summary_extrema(tmp_path):
    reflectance = _write_reflectance(tmp_path / "reflectance.tif", [[[0.25]], [[0.75]]])
    quality = _write_mask(tmp_path / "quality.tif", [[0]])
    target = tmp_path / "ndvi.tif"

    result = write_ndvi(reflectance, quality, target)

    _assert_raster(target, reflectance, result, np.array([[np.nan]]))
    summary = result.summary
    assert (summary.total_pixels, summary.valid_pixels, summary.valid_fraction, summary.quality_invalid_pixels) == (1, 0, 0, 1)
    assert (summary.minimum, summary.maximum, summary.first_out_of_range) == (None, None, None)


def test_does_not_replace_destination_or_companion(tmp_path):
    reflectance = _write_reflectance(tmp_path / "reflectance.tif", [[[0.25]], [[0.75]]])
    quality = _write_mask(tmp_path / "quality.tif", [[1]])
    target = tmp_path / "ndvi.tif"
    companion = tmp_path / "ndvi.tif.aux.xml"
    companion.write_bytes(b"existing companion")

    with pytest.raises(FileExistsError):
        write_ndvi(reflectance, quality, target)

    assert companion.read_bytes() == b"existing companion" and not target.exists()
