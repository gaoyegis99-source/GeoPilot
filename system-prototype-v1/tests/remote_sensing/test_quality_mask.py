import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from product_fixtures import LANDSAT, LANDSAT_CODES, SENTINEL, SENTINEL_CODES
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject

from geopilot.data.fingerprint import fingerprint_file
from geopilot.remote_sensing.band_mapper import BandMappingError
from geopilot.remote_sensing.config_models import SensorConfigRef, SensorProductConfig
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.remote_sensing.quality_mask import QualityMaskError, write_quality_mask


QUALITY_DOMAIN = "GEOPILOT_QUALITY_MASK"
CRS = "EPSG:32632"


def test_landsat_mask_obeys_configured_bits_raw_zero_and_shared_gdal_mask(
    tmp_path, sensor_registry, landsat_product_xml, write_product_stack,
):
    quality = np.array([[0, 1, 2, 4, 8], [16, 32, 64, 128, 256], [0, 0, 0, 0, 0], [0, 0, 0, 0, 0]], dtype="uint16")
    saturation = np.zeros(quality.shape, dtype="uint16")
    saturation[2] = [8, 16, 2048, 2, 0]
    red = np.full(quality.shape, 10000, dtype="uint16")
    nir = np.full(quality.shape, 20000, dtype="uint16")
    red[3, 0] = nir[3, 1] = 0
    shared_mask = np.ones(quality.shape, dtype=bool)
    shared_mask[3, 2] = False
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        source = write_product_stack(
            tmp_path / "landsat.tif", profile=LANDSAT, xml=landsat_product_xml(), codes=LANDSAT_CODES, pixel_m=30,
            values=np.stack((quality, nir, saturation, red)), shared_mask=shared_mask,
        )
    before = fingerprint_file(source)
    target = tmp_path / "quality.tif"
    expected = np.array([[1, 0, 0, 0, 0], [0, 0, 1, 1, 1], [0, 0, 0, 1, 1], [0, 0, 0, 1, 1]], dtype="uint8")

    result = write_quality_mask(source, target, registry=sensor_registry)

    assert result.configuration_ref == SensorConfigRef(id="landsat-8-c2-l2sp", version=1)
    assert (result.valid_pixels, result.invalid_pixels, result.unknown_class_pixels) == (8, 12, {})
    assert result.metadata.nodata == (None,) and result.metadata.band_names == ("quality_mask",)
    with rasterio.open(source) as original, rasterio.open(target) as mask:
        assert original.crs == mask.crs and original.transform == mask.transform and original.shape == mask.shape
        assert mask.count == 1 and mask.dtypes == ("uint8",) and mask.nodata is None
        np.testing.assert_array_equal(mask.read(1), expected)
        np.testing.assert_array_equal(mask.read_masks(1), np.full(expected.shape, 255, dtype="uint8"))
        tags = mask.tags(ns=QUALITY_DOMAIN)
        assert (tags["CONFIG_ID"], tags["CONFIG_VERSION"]) == ("landsat-8-c2-l2sp", "1")
        assert tags["SOURCE_PRODUCT_ID"] == "LC08_L2SP_043035_20241101_20241103_02_T1"
        assert (tags["VALID_PIXELS"], tags["INVALID_PIXELS"]) == ("8", "12")
        assert json.loads(tags["UNKNOWN_CLASS_PIXELS"]) == {}
    assert fingerprint_file(source) == before
    assert set(tmp_path.iterdir()) == {source, target}


def test_quality_bits_follow_the_resolved_versioned_configuration(
    tmp_path, sensor_registry, landsat_product_xml, write_product_stack,
):
    profile = sensor_registry.get(SensorConfigRef(id="landsat-8-c2-l2sp", version=1)).model_dump(mode="json")
    profile["version"] = 2
    profile["quality_rules"]["quality"]["invalid_bits"] = {"water": 7}
    registry = SensorConfigRegistry((SensorProductConfig.model_validate(profile),))
    source = write_product_stack(
        tmp_path / "landsat.tif", profile=LANDSAT, xml=landsat_product_xml(), codes=LANDSAT_CODES, pixel_m=30,
        values=np.array([[[0, 8, 128]], [[20000, 20000, 20000]], [[0, 0, 0]], [[10000, 10000, 10000]]], dtype="uint16"),
    )
    target = tmp_path / "mask.tif"
    result = write_quality_mask(source, target, registry=registry)
    with rasterio.open(target) as raster:
        assert raster.read(1).tolist() == [[1, 1, 0]]
    assert result.configuration_ref.version == 2


def test_sentinel_uses_real_nearest_aligned_scl_and_counts_unknown_values(
    tmp_path, sensor_registry, sentinel_product_xml, write_product_stack,
):
    native = np.array([[4, 5, 6, 0], [1, 2, 3, 7], [8, 9, 10, 11], [12, 4, 5, 6]], dtype="uint8")
    native_path = tmp_path / "native-scl.tif"
    origin = (500000, 5100000)
    with rasterio.open(native_path, "w", driver="GTiff", width=4, height=4, count=1, dtype="uint8", crs=CRS,
                       transform=from_origin(*origin, 20, 20)) as raster:
        raster.write(native, 1)
    aligned = np.zeros((8, 8), dtype="uint16")
    with rasterio.open(native_path) as raster:
        reproject(
            rasterio.band(raster, 1), aligned, src_transform=raster.transform, src_crs=raster.crs,
            dst_transform=from_origin(*origin, 10, 10), dst_crs=CRS, resampling=Resampling.nearest,
        )
    hand_aligned = native.repeat(2, axis=0).repeat(2, axis=1)
    np.testing.assert_array_equal(aligned, hand_aligned)
    red = np.full(aligned.shape, 3000, dtype="uint16")
    nir = np.full(aligned.shape, 7000, dtype="uint16")
    red[0, 0] = 0
    shared_mask = np.ones(aligned.shape, dtype=bool)
    shared_mask[0, 1] = False
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        source = write_product_stack(
            tmp_path / "sentinel.tif", profile=SENTINEL, xml=sentinel_product_xml(), codes=SENTINEL_CODES, pixel_m=10,
            values=np.stack((aligned, nir, red)), scl_resampling="nearest", shared_mask=shared_mask,
        )
    target = tmp_path / "quality.tif"

    result = write_quality_mask(source, target, registry=sensor_registry)

    expected = np.isin(hand_aligned, [4, 5, 6]).astype("uint8")
    expected[0, 0] = expected[0, 1] = 0
    assert result.configuration_ref == SensorConfigRef(id="sentinel-2-l2a-pb05-11", version=1)
    assert (result.valid_pixels, result.invalid_pixels, result.unknown_class_pixels) == (22, 42, {"quality": 4})
    with rasterio.open(target) as raster:
        assert raster.crs.to_string() == CRS and raster.transform == from_origin(*origin, 10, 10)
        assert raster.nodata is None and raster.dtypes == ("uint8",)
        np.testing.assert_array_equal(raster.read(1), expected)
        assert (raster.read(1)[6:8, 0:2] == 0).all()
        tags = raster.tags(ns=QUALITY_DOMAIN)
        bands = json.loads(tags["QUALITY_BANDS"])
        assert bands["quality"]["native_resolution_m"] == 20 and bands["quality"]["resampling"] == "nearest"
        assert json.loads(tags["UNKNOWN_CLASS_PIXELS"]) == {"quality": 4}


def test_rejects_shared_nodata_zero_that_would_hide_landsat_qa_zero(
    tmp_path, sensor_registry, landsat_product_xml, write_product_stack,
):
    source = write_product_stack(
        tmp_path / "landsat.tif", profile=LANDSAT, xml=landsat_product_xml(), codes=LANDSAT_CODES, pixel_m=30, nodata=0,
    )
    target = tmp_path / "quality.tif"
    with pytest.raises(BandMappingError) as failure:
        write_quality_mask(source, target, registry=sensor_registry)
    assert failure.value.field == "source.nodata" and not target.exists()


def test_rejects_sentinel_scl_without_nearest_provenance(
    tmp_path, sensor_registry, sentinel_product_xml, write_product_stack,
):
    source = write_product_stack(
        tmp_path / "sentinel.tif", profile=SENTINEL, xml=sentinel_product_xml(), codes=SENTINEL_CODES,
        pixel_m=10, scl_resampling="bilinear",
    )
    target = tmp_path / "quality.tif"
    with pytest.raises(BandMappingError) as failure:
        write_quality_mask(source, target, registry=sensor_registry)
    assert failure.value.field == "bands.quality.resampling" and not target.exists()


def test_rejects_observed_bit_codes_outside_declared_storage_width(
    tmp_path, sensor_registry, landsat_product_xml, write_product_stack,
):
    values = np.zeros((4, 1, 257), dtype="uint32")
    values[0, 0, -1] = 65536
    values[1] = 20000
    values[3] = 10000
    source = write_product_stack(
        tmp_path / "landsat.tif", profile=LANDSAT, xml=landsat_product_xml(), codes=LANDSAT_CODES, pixel_m=30,
        dtype="uint32", values=values,
    )
    target = tmp_path / "quality.tif"
    with pytest.raises(QualityMaskError) as failure:
        write_quality_mask(source, target, registry=sensor_registry)
    assert (failure.value.code, failure.value.field) == ("QUALITY_CODE_OUT_OF_RANGE", "bands.quality")
    assert set(tmp_path.iterdir()) == {source}


def test_reopens_real_tiles_and_matches_across_block_boundaries(
    tmp_path, monkeypatch, sensor_registry, landsat_product_xml, write_product_stack,
):
    rows, columns = np.indices((517, 519))
    cloud = (rows + columns) % 3 == 0
    quality = np.where(cloud, 8, 0).astype("uint16")
    nir = np.full(quality.shape, 20000, dtype="uint16")
    red = np.full(quality.shape, 10000, dtype="uint16")
    red[255, 256] = 0
    source = write_product_stack(
        tmp_path / "landsat.tif", profile=LANDSAT, xml=landsat_product_xml(), codes=LANDSAT_CODES, pixel_m=30,
        values=np.stack((quality, nir, np.zeros_like(quality), red)),
    )
    target = tmp_path / "quality.tif"
    actual_open = rasterio.open
    windows = []

    def track_reads(path, *args, **kwargs):
        dataset = actual_open(path, *args, **kwargs)
        if Path(path) == source:
            actual_read = dataset.read

            def read(*args, **kwargs):
                windows.append(kwargs.get("window"))
                return actual_read(*args, **kwargs)

            dataset.read = read
        return dataset

    with monkeypatch.context() as patch:
        patch.setattr(rasterio, "open", track_reads)
        result = write_quality_mask(source, target, registry=sensor_registry)

    assert len(windows) == 9 and all(window is not None and window.width <= 256 and window.height <= 256 for window in windows)
    expected = (~cloud & (red != 0)).astype("uint8")
    assert result.valid_pixels == int(expected.sum())
    with rasterio.open(target) as raster:
        np.testing.assert_array_equal(raster.read(1), expected)


@pytest.mark.parametrize("existing_name", ["quality.tif", "quality.tif.aux.xml"])
def test_rejects_overwriting_existing_outputs_or_companions(
    tmp_path, sensor_registry, landsat_product_xml, write_product_stack, existing_name,
):
    source = write_product_stack(
        tmp_path / "landsat.tif", profile=LANDSAT, xml=landsat_product_xml(), codes=LANDSAT_CODES, pixel_m=30,
    )
    existing = tmp_path / existing_name
    existing.write_bytes(b"preserve existing")
    with pytest.raises(FileExistsError):
        write_quality_mask(source, tmp_path / "quality.tif", registry=sensor_registry)
    assert existing.read_bytes() == b"preserve existing"
    assert set(tmp_path.iterdir()) == {source, existing}
