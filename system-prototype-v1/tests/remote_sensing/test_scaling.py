import json
import math

import numpy as np
import pytest
import rasterio

from geopilot.data.fingerprint import fingerprint_file
from geopilot.remote_sensing.band_mapper import map_product_bands
from geopilot.remote_sensing.config_models import SensorProductConfig
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.remote_sensing.scaling import (
    ReflectanceScalingError,
    resolve_reflectance_coefficients,
    scale_product_reflectance,
)
from product_fixtures import LANDSAT, LANDSAT_CODES, SENTINEL, SENTINEL_CODES


def test_landsat_scale_reads_spectral_roles_and_masks_independently(
    tmp_path, sensor_registry, landsat_product_xml, write_product_stack,
):
    source = tmp_path / "landsat.tif"
    target = tmp_path / "reflectance.tif"
    write_product_stack(
        source, profile=LANDSAT, xml=landsat_product_xml(), codes=LANDSAT_CODES, pixel_m=30,
        values=[
            [[8, 0, 0], [64, 0, 0]],  # QA cloud flag does not participate in scaling.
            [[20000, 20000, 0], [20000, 20000, 20000]],
            [[24, 0, 0], [0, 0, 0]],
            [[10000, 0, 18639], [10000, 10000, 10000]],
        ],
        shared_mask=np.array([[255, 255, 255], [0, 255, 255]], dtype="uint8"),
    )
    before = fingerprint_file(source)

    result = scale_product_reflectance(source, target, registry=sensor_registry)

    with rasterio.open(source) as raw, rasterio.open(target) as scaled:
        assert (scaled.crs, scaled.transform, scaled.shape) == (raw.crs, raw.transform, raw.shape)
        assert scaled.descriptions == ("red", "nir")
        assert scaled.dtypes == ("float64", "float64")
        assert scaled.units == ("reflectance", "reflectance")
        assert scaled.scales == (1.0, 1.0)
        assert scaled.offsets == (0.0, 0.0)
        assert all(math.isnan(value) for value in scaled.nodatavals)
        assert json.loads(scaled.tags(ns="GEOPILOT_REFLECTANCE")["CALIBRATION"]) == result.evidence
        data = scaled.read(masked=True)
        np.testing.assert_array_equal(data.mask, [
            [[False, True, False], [True, False, False]],
            [[False, False, True], [True, False, False]],
        ])
        np.testing.assert_allclose(data.filled(np.nan), [
            [[0.075, np.nan, 0.3125725], [np.nan, 0.075, 0.075]],
            [[0.35, 0.35, np.nan], [np.nan, 0.35, 0.35]],
        ], rtol=0, atol=1e-15)

    assert result.metadata.band_count == 2
    assert result.metadata.nodata == ("nan", "nan")
    assert result.evidence["product_id"].startswith("LC08_L2SP_")
    assert result.evidence["configuration"] == {"id": "landsat-8-c2-l2sp", "version": 1}
    assert result.evidence["bands"]["red"]["source_index"] == 4
    assert result.evidence["bands"]["nir"]["source_index"] == 2
    assert fingerprint_file(source) == before


def test_landsat_uses_product_specific_xml_coefficients_across_tile_boundaries(
    tmp_path, sensor_registry, landsat_product_xml, write_product_stack,
):
    source = tmp_path / "different-calibration.tif"
    target = tmp_path / "reflectance.tif"
    xml = (landsat_product_xml()
           .replace("2.75E-05</REFLECTANCE_MULT_BAND_4>", "5E-05</REFLECTANCE_MULT_BAND_4>")
           .replace("-0.2</REFLECTANCE_ADD_BAND_4>", "-0.1</REFLECTANCE_ADD_BAND_4>")
           .replace("2.75E-05</REFLECTANCE_MULT_BAND_5>", "1E-05</REFLECTANCE_MULT_BAND_5>")
           .replace("-0.2</REFLECTANCE_ADD_BAND_5>", "0.03</REFLECTANCE_ADD_BAND_5>"))
    values = np.zeros((4, 257, 259), dtype="uint16")
    values[1].fill(20000)
    values[3].fill(10000)
    write_product_stack(source, profile=LANDSAT, xml=xml, codes=LANDSAT_CODES, pixel_m=30, values=values)

    coefficients = resolve_reflectance_coefficients(map_product_bands(source, registry=sensor_registry))
    assert coefficients["red"].scale == 0.00005
    assert coefficients["red"].offset == -0.1
    assert coefficients["nir"].scale == 0.00001
    assert coefficients["nir"].offset == 0.03

    result = scale_product_reflectance(source, target, registry=sensor_registry)
    with rasterio.open(target) as scaled:
        assert scaled.block_shapes == [(256, 256), (256, 256)]
        np.testing.assert_allclose(scaled.read(1), 0.4, rtol=0, atol=1e-15)
        np.testing.assert_allclose(scaled.read(2), 0.23, rtol=0, atol=1e-15)
    assert result.evidence["bands"]["red"]["scale_field"] == {
        "path": ["LEVEL2_SURFACE_REFLECTANCE_PARAMETERS", "REFLECTANCE_MULT_BAND_4"],
    }
    assert result.evidence["bands"]["nir"]["offset"] == 0.03


def test_sentinel_adds_dn_offset_before_division_and_preserves_valid_zero(
    tmp_path, sensor_registry, sentinel_product_xml, write_product_stack,
):
    source = tmp_path / "sentinel.tif"
    target = tmp_path / "reflectance.tif"
    write_product_stack(
        source, profile=SENTINEL, xml=sentinel_product_xml(), codes=SENTINEL_CODES, pixel_m=10,
        scl_resampling="nearest",
        values=[[[4, 9], [4, 4]], [[1200, 1000], [7000, 7000]], [[800, 1000], [3000, 0]]],
    )

    result = scale_product_reflectance(source, target, registry=sensor_registry)

    with rasterio.open(target) as scaled:
        red, nir = scaled.read(masked=True)
        assert red[0, 0] == -0.02
        assert nir[0, 0] == 0.02
        assert nir[0, 0] + red[0, 0] == 0.0
        assert red[0, 1] == nir[0, 1] == 0.0
        assert red[1, 0] == 0.2
        assert nir[1, 0] == nir[1, 1] == 0.6
        assert red.mask.tolist() == [[False, False], [False, True]]
        assert not nir.mask.any()
    assert result.evidence["bands"]["red"]["quantification"] == 10000.0
    assert result.evidence["bands"]["red"]["dn_offset_field"]["band_id"] == 3
    assert result.evidence["bands"]["nir"]["dn_offset_field"]["band_id"] == 7


def test_sentinel_resolves_quantification_and_different_offsets_by_native_band_id(
    tmp_path, sensor_registry, sentinel_product_xml, write_product_stack,
):
    source = tmp_path / "sentinel.tif"
    target = tmp_path / "reflectance.tif"
    xml = (sentinel_product_xml()
           .replace('unit="none">10000', 'unit="none">20000')
           .replace('band_id="3">-1000', 'band_id="3">-1234')
           .replace('band_id="7">-1000', 'band_id="7">-2345'))
    write_product_stack(
        source, profile=SENTINEL, xml=xml, codes=SENTINEL_CODES, pixel_m=10,
        scl_resampling="nearest", values=[[[4]], [[7000]], [[3000]]],
    )
    scale_product_reflectance(source, target, registry=sensor_registry)
    with rasterio.open(target) as scaled:
        np.testing.assert_array_equal(scaled.read()[:, 0, 0], [0.0883, 0.23275])


@pytest.mark.parametrize("replacement", [None, "NaN", "0", "-0.00001"])
def test_landsat_invalid_scale_leaves_no_output(
    tmp_path, sensor_registry, landsat_product_xml, write_product_stack, replacement,
):
    source = tmp_path / "invalid.tif"
    target = tmp_path / "reflectance.tif"
    original = "<REFLECTANCE_MULT_BAND_4>2.75E-05</REFLECTANCE_MULT_BAND_4>"
    updated = "" if replacement is None else original.replace("2.75E-05", replacement)
    write_product_stack(
        source, profile=LANDSAT, xml=landsat_product_xml().replace(original, updated),
        codes=LANDSAT_CODES, pixel_m=30,
    )

    with pytest.raises(ReflectanceScalingError) as failure:
        scale_product_reflectance(source, target, registry=sensor_registry)

    assert failure.value.field == "product_metadata.LEVEL2_SURFACE_REFLECTANCE_PARAMETERS.REFLECTANCE_MULT_BAND_4"
    assert not target.exists()


def test_sentinel_rejects_zero_quantification(tmp_path, sensor_registry, sentinel_product_xml, write_product_stack):
    source = tmp_path / "invalid.tif"
    target = tmp_path / "reflectance.tif"
    write_product_stack(
        source, profile=SENTINEL, xml=sentinel_product_xml().replace('unit="none">10000', 'unit="none">0'),
        codes=SENTINEL_CODES, pixel_m=10, scl_resampling="nearest",
    )
    with pytest.raises(ReflectanceScalingError) as failure:
        scale_product_reflectance(source, target, registry=sensor_registry)
    assert failure.value.field.endswith(".BOA_QUANTIFICATION_VALUE")
    assert not target.exists()


def test_sentinel_rejects_configured_offset_for_a_different_native_band(
    tmp_path, sensor_registry, sentinel_product_xml, write_product_stack,
):
    source = tmp_path / "sentinel.tif"
    target = tmp_path / "reflectance.tif"
    write_product_stack(
        source, profile=SENTINEL, xml=sentinel_product_xml(), codes=SENTINEL_CODES, pixel_m=10,
        scl_resampling="nearest",
    )
    mapping = map_product_bands(source, registry=sensor_registry)
    configuration = mapping.configuration.model_dump()
    configuration["scale_offset"]["red"]["dn_offset_field"]["band_id"] = 8  # B8A is present in the XML.
    mismatched = SensorConfigRegistry([SensorProductConfig.model_validate(configuration)])

    with pytest.raises(ReflectanceScalingError) as failure:
        scale_product_reflectance(source, target, registry=mismatched)
    assert failure.value.field == "configuration.scale_offset.red.dn_offset_field.band_id"
    assert not target.exists()


def test_pixel_overflow_preserves_source_and_cleans_staged_output(
    tmp_path, sensor_registry, landsat_product_xml, write_product_stack,
):
    source = tmp_path / "overflow.tif"
    target = tmp_path / "reflectance.tif"
    xml = landsat_product_xml().replace("2.75E-05</REFLECTANCE_MULT_BAND_4>", "1E308</REFLECTANCE_MULT_BAND_4>")
    write_product_stack(source, profile=LANDSAT, xml=xml, codes=LANDSAT_CODES, pixel_m=30)
    before = fingerprint_file(source)
    original_files = set(tmp_path.iterdir())
    with pytest.raises(ReflectanceScalingError) as failure:
        scale_product_reflectance(source, target, registry=sensor_registry)
    assert failure.value.field == "bands.red"
    assert set(tmp_path.iterdir()) == original_files
    assert fingerprint_file(source) == before
