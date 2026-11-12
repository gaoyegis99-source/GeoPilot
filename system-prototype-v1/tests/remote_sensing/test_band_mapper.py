import numpy as np
import pytest
import rasterio

from geopilot.application.run_inputs import stage_run_inputs
from geopilot.data.assets import register_asset
from geopilot.persistence.schema import initialize_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.remote_sensing.band_mapper import BandMappingError, map_product_bands
from geopilot.remote_sensing.config_models import SensorConfigRef, SensorProductKey
from geopilot.remote_sensing.config_registry import (
    SensorConfigLookupError,
    SensorConfigRegistry,
)
from geopilot.remote_sensing.product_metadata import (
    ProductMetadataError,
    read_product_metadata,
    resolve_metadata_field,
)
from product_fixtures import (
    GRANULE,
    LANDSAT,
    LANDSAT_CODES,
    OTHER_GRANULE,
    SENTINEL,
    SENTINEL_CODES,
    build_landsat_xml as _landsat_xml,
    build_sentinel_granule_xml as _granule_xml,
    build_sentinel_xml as _sentinel_xml,
    write_stack as _write_stack,
)


@pytest.fixture
def registry(sensor_registry):
    return sensor_registry


def test_landsat_mapping_reads_reopened_versioned_stack_and_retains_zero_qa(
    tmp_path, sensor_registry, landsat_product_xml, write_product_stack,
):
    xml = landsat_product_xml()
    values = np.arange(24, dtype="uint16").reshape(4, 2, 3)
    source = write_product_stack(tmp_path / "landsat.tif", profile=LANDSAT, xml=xml, codes=LANDSAT_CODES,
                                 pixel_m=30, values=values)
    database = tmp_path / "local.sqlite"
    initialize_database(database)
    asset = register_asset(database, source)
    workspace = create_run_workspace(tmp_path / "runs", "p8-task", "p8-run")
    reference = stage_run_inputs(database, {"product": asset.ref}, workspace.path)["product"]
    staged = workspace.path / reference.path
    assert reference.path == "inputs/0001/source.tif"
    assert staged.read_bytes() == source.read_bytes()

    result = map_product_bands(staged, registry=sensor_registry)
    assert result.metadata == read_product_metadata(staged)
    assert result.metadata.profile == LANDSAT and result.metadata.xml == xml
    assert result.metadata.spacecraft == "LANDSAT_8" and result.metadata.processing_level == "L2SP"
    assert result.metadata.key == SensorProductKey(sensor="landsat-8-oli-tirs", product_level="L2SP", processing_version="02")
    assert result.configuration.ref == SensorConfigRef(id="landsat-8-c2-l2sp", version=1)
    assert {role: band.index for role, band in result.bands.items()} == {"red": 4, "nir": 2, "quality": 1, "saturation": 3}
    assert result.bands["red"].code == "SR_B4" and result.bands["red"].nodata_dn == 0
    assert result.bands["quality"].nodata_dn is None and result.bands["quality"].dtype == "uint16"
    assert result.bands["red"].native_resolution_m == 30
    assert result.bands["red"].source_filename == f"{result.metadata.product_id}_SR_B4.TIF"
    assert result.metadata.band_sources["SR_B4"].source_filename == result.bands["red"].source_filename
    assert result.bands["saturation"].source_filename == f"{result.metadata.product_id}_QA_RADSAT.TIF"
    scale = result.configuration.scale_offset["red"]
    assert resolve_metadata_field(result.metadata, scale.scale_field) == "2.75E-05"
    assert resolve_metadata_field(result.metadata, scale.offset_field) == "-0.2"
    with rasterio.open(staged) as dataset:
        quality = dataset.read(result.bands["quality"].index, masked=True)
        assert quality[0, 0] == 0 and not np.ma.getmaskarray(quality)[0, 0]


def test_landsat_9_l2sr_matches_the_separate_configuration(tmp_path, registry):
    source = _write_stack(tmp_path / "landsat-9.tif", profile=LANDSAT,
                          xml=_landsat_xml(spacecraft="LANDSAT_9", level="L2SR"), codes=LANDSAT_CODES, pixel_m=30)
    result = map_product_bands(source, registry=registry)
    assert result.metadata.spacecraft == "LANDSAT_9" and result.metadata.processing_level == "L2SR"
    assert result.configuration.ref == SensorConfigRef(id="landsat-9-c2-l2sr", version=1)


def test_sentinel_matches_exact_native_resolution_and_calibration_band_ids(
    tmp_path, sensor_registry, sentinel_product_xml, write_product_stack,
):
    xml = sentinel_product_xml()
    source = write_product_stack(tmp_path / "sentinel.tif", profile=SENTINEL, xml=xml, codes=SENTINEL_CODES,
                                 pixel_m=10, scl_resampling="nearest")
    result = map_product_bands(source, registry=sensor_registry)
    assert result.metadata.xml == xml and result.metadata.granule_id == GRANULE
    assert result.metadata.spacecraft == "Sentinel-2A" and result.metadata.processing_level == "Level-2A"
    assert result.metadata.key == SensorProductKey(sensor="sentinel-2-msi", product_level="L2A", processing_version="05.11")
    assert result.configuration.ref == SensorConfigRef(id="sentinel-2-l2a-pb05-11", version=1)
    assert {role: band.index for role, band in result.bands.items()} == {"red": 3, "nir": 2, "quality": 1}
    assert result.bands["red"].source_filename.endswith("/R10m/T32UMA_20240831T103021_B04_10m.jp2")
    assert result.bands["nir"].source_filename.endswith("/R10m/T32UMA_20240831T103021_B08_10m.jp2")
    assert result.bands["quality"].source_filename.endswith("/R20m/T32UMA_20240831T103021_SCL_20m.jp2")
    assert result.bands["quality"].native_resolution_m == 20 and result.bands["quality"].resampling == "nearest"
    assert result.bands["nir"].native_resolution_m == 10 and result.bands["nir"].resampling is None
    assert result.metadata.band_sources["SCL"].declared_filename == result.bands["quality"].source_filename[:-4]
    assert result.metadata.band_sources["B08"].metadata_field.endswith(".Granule.IMAGE_FILE")
    for role, band_id in (("red", 3), ("nir", 7)):
        calibration = result.configuration.scale_offset[role]
        assert result.bands[role].band_id == calibration.dn_offset_field.band_id == band_id
        assert resolve_metadata_field(result.metadata, calibration.quantification_field) == "10000"
        assert resolve_metadata_field(result.metadata, calibration.dn_offset_field) == "-1000"


def test_sentinel_selects_explicit_granule_with_its_own_file_paths(tmp_path, registry):
    xml = _sentinel_xml(granules=_granule_xml() + _granule_xml(OTHER_GRANULE, "T32UMB"))
    source = _write_stack(tmp_path / "sentinel.tif", profile=SENTINEL, xml=xml, codes=SENTINEL_CODES,
                          pixel_m=10, granule=OTHER_GRANULE, scl_resampling="nearest")
    result = map_product_bands(source, registry=registry)
    assert result.metadata.granule_id == OTHER_GRANULE
    assert all("/L2A_T32UMB_" in band.source_filename for band in result.bands.values())


@pytest.mark.parametrize("selector", [None, "unknown-granule", GRANULE])
def test_sentinel_rejects_ambiguous_missing_or_duplicated_granule(tmp_path, registry, selector):
    second_id = GRANULE if selector == GRANULE else OTHER_GRANULE
    xml = _sentinel_xml(granules=_granule_xml() + _granule_xml(second_id, "T32UMB"))
    source = _write_stack(tmp_path / "sentinel.tif", profile=SENTINEL, xml=xml, codes=SENTINEL_CODES,
                          pixel_m=10, granule=selector, scl_resampling="nearest")
    with pytest.raises(ProductMetadataError) as error:
        map_product_bands(source, registry=registry)
    assert error.value.code == "PRODUCT_METADATA_INVALID" and error.value.field == "product_metadata.GRANULE_ID"


@pytest.mark.parametrize(("profile", "xml"), [
    (None, _landsat_xml()),
    ("custom-product", _landsat_xml()),
    (LANDSAT, None),
    (LANDSAT, "<LANDSAT_METADATA_FILE>"),
    (LANDSAT, '<!DOCTYPE LANDSAT_METADATA_FILE SYSTEM "file:///tmp/unrelated.xml">\n<LANDSAT_METADATA_FILE/>'),
    (LANDSAT, _sentinel_xml()),
    (SENTINEL, _sentinel_xml().replace("psd-15.sentinel2", "psd-14.sentinel2")),
])
def test_product_reader_rejects_missing_unrecognized_or_mismatched_metadata(tmp_path, profile, xml):
    source = _write_stack(tmp_path / "source.tif", profile=profile, xml=xml, codes=LANDSAT_CODES, pixel_m=30)
    with pytest.raises(ProductMetadataError) as error:
        read_product_metadata(source)
    assert error.value.code == "PRODUCT_METADATA_INVALID"
    assert error.value.field.startswith("product_metadata.")


@pytest.mark.parametrize("xml", [
    _landsat_xml().replace("<COLLECTION_NUMBER>02</COLLECTION_NUMBER>", "<COLLECTION_NUMBER>02</COLLECTION_NUMBER>" * 2),
    _landsat_xml().replace("_QA_RADSAT.TIF", "_QA_PIXEL.TIF"),
    _landsat_xml().replace("LC08_L2SP_043035_20241101_20241103_02_T1_SR_B4.TIF", "another_product_SR_B4.TIF"),
])
def test_landsat_rejects_ambiguous_identity_or_inconsistent_native_files(tmp_path, registry, xml):
    source = _write_stack(tmp_path / "landsat.tif", profile=LANDSAT, xml=xml, codes=LANDSAT_CODES, pixel_m=30)
    with pytest.raises(ProductMetadataError) as error:
        map_product_bands(source, registry=registry)
    assert error.value.code == "PRODUCT_METADATA_INVALID" and "PRODUCT_CONTENTS" in error.value.field


@pytest.mark.parametrize("xml", [
    _sentinel_xml().replace("_B08_10m", "_B8A_10m"),
    _sentinel_xml().replace("_SCL_20m", "_SCL_10m"),
    _sentinel_xml().replace("<IMAGE_FILE>GRANULE/L2A_T32UMA_A048010_20240831T103023/IMG_DATA/R10m/T32UMA_20240831T103021_B08_10m</IMAGE_FILE>",
                           "<IMAGE_FILE>GRANULE/another_granule/IMG_DATA/R10m/T32UMA_20240831T103021_B08_10m</IMAGE_FILE>"),
])
def test_sentinel_requires_native_files_for_the_same_granule(tmp_path, registry, xml):
    source = _write_stack(tmp_path / "sentinel.tif", profile=SENTINEL, xml=xml, codes=SENTINEL_CODES,
                          pixel_m=10, scl_resampling="nearest")
    with pytest.raises(ProductMetadataError) as error:
        map_product_bands(source, registry=registry)
    assert error.value.code == "PRODUCT_METADATA_INVALID" and "IMAGE_FILE" in error.value.field


@pytest.mark.parametrize(("codes", "expected_code", "field"), [
    (("SCL", "B8A", "B04"), "BAND_MISSING", "bands.nir"),
    (("SCL", "B08", "B08", "B04"), "BAND_AMBIGUOUS", "bands.nir"),
    (("B08", "B04"), "BAND_MISSING", "bands.quality"),
])
def test_stack_band_descriptions_must_uniquely_match_required_codes(tmp_path, registry, codes, expected_code, field):
    source = _write_stack(tmp_path / "sentinel.tif", profile=SENTINEL, xml=_sentinel_xml(), codes=codes,
                          pixel_m=10, scl_resampling="nearest")
    with pytest.raises(BandMappingError) as error:
        map_product_bands(source, registry=registry)
    assert (error.value.code, error.value.field) == (expected_code, field)


@pytest.mark.parametrize("resampling", [None, "bilinear"])
def test_sentinel_scl_resampling_is_explicit_nearest(tmp_path, registry, resampling):
    source = _write_stack(tmp_path / "sentinel.tif", profile=SENTINEL, xml=_sentinel_xml(), codes=SENTINEL_CODES,
                          pixel_m=10, scl_resampling=resampling)
    with pytest.raises(BandMappingError) as error:
        map_product_bands(source, registry=registry)
    assert (error.value.code, error.value.field) == ("BAND_MAPPING_INVALID", "bands.quality.resampling")


@pytest.mark.parametrize(("overrides", "field"), [
    ({"nodata": 0}, "source.nodata"),
    ({"pixel_m": 15}, "source.resolution"),
    ({"crs": "EPSG:4326"}, "source.crs"),
    ({"crs": None}, "source.crs"),
    ({"scales": (1, 0.0000275, 1, 1)}, "bands.nir"),
    ({"dtype": "float32"}, "bands.red"),
])
def test_raw_stack_rejects_misleading_raster_metadata(tmp_path, registry, overrides, field):
    stack = {"profile": LANDSAT, "xml": _landsat_xml(), "codes": LANDSAT_CODES, "pixel_m": 30}
    source = _write_stack(tmp_path / "landsat.tif", **(stack | overrides))
    with pytest.raises(BandMappingError) as error:
        map_product_bands(source, registry=registry)
    assert (error.value.code, error.value.field) == ("BAND_MAPPING_INVALID", field)


def test_raw_stack_can_retain_a_shared_validity_mask(tmp_path, registry):
    shared = np.array([[255, 255], [0, 255]], dtype="uint8")
    source = _write_stack(tmp_path / "landsat.tif", profile=LANDSAT, xml=_landsat_xml(), codes=LANDSAT_CODES,
                          pixel_m=30, shared_mask=shared)
    result = map_product_bands(source, registry=registry)
    with rasterio.open(source) as dataset:
        np.testing.assert_array_equal(dataset.read_masks(result.bands["quality"].index), shared)


def test_lookup_uses_exact_observed_collection_before_mapping(tmp_path, registry):
    source = _write_stack(tmp_path / "unknown-collection.tif", profile=LANDSAT, xml=_landsat_xml(collection="03"),
                          codes=LANDSAT_CODES, pixel_m=30)
    with pytest.raises(SensorConfigLookupError) as error:
        map_product_bands(source, registry=registry)
    assert error.value.code == "SENSOR_CONFIG_NOT_FOUND" and error.value.key.processing_version == "03"


def test_lookup_refuses_an_ambiguous_configuration(tmp_path, registry):
    source = _write_stack(tmp_path / "landsat.tif", profile=LANDSAT, xml=_landsat_xml(), codes=LANDSAT_CODES, pixel_m=30)
    configuration = registry.lookup(read_product_metadata(source).key)
    conflicting = SensorConfigRegistry([configuration, configuration.model_copy(update={"version": 2})])
    with pytest.raises(SensorConfigLookupError) as error:
        map_product_bands(source, registry=conflicting)
    assert error.value.code == "SENSOR_CONFIG_AMBIGUOUS" and len(error.value.matches) == 2


def test_profile_does_not_accept_configured_b8a_in_place_of_b08(tmp_path, registry):
    source = _write_stack(tmp_path / "sentinel.tif", profile=SENTINEL, xml=_sentinel_xml(),
                          codes=("B04", "B8A", "SCL"), pixel_m=10, scl_resampling="nearest")
    configuration = registry.lookup(read_product_metadata(source).key)
    configuration.band_roles["nir"] = configuration.band_roles["nir"].model_copy(update={"code": "B8A", "resolution_m": 20})
    with pytest.raises(BandMappingError) as error:
        map_product_bands(source, registry=SensorConfigRegistry([configuration]))
    assert (error.value.code, error.value.field) == ("BAND_MAPPING_INVALID", "bands.nir")


def test_calibration_field_resolver_rejects_duplicate_band_selector(tmp_path, registry):
    xml = _sentinel_xml().replace('<BOA_ADD_OFFSET band_id="3">-1000</BOA_ADD_OFFSET>',
                                  '<BOA_ADD_OFFSET band_id="3">-1000</BOA_ADD_OFFSET>' * 2)
    source = _write_stack(tmp_path / "sentinel.tif", profile=SENTINEL, xml=xml, codes=SENTINEL_CODES,
                          pixel_m=10, scl_resampling="nearest")
    metadata = read_product_metadata(source)
    red = registry.lookup(metadata.key).scale_offset["red"]
    with pytest.raises(ProductMetadataError) as error:
        resolve_metadata_field(metadata, red.dn_offset_field)
    assert error.value.field.endswith("BOA_ADD_OFFSET[band_id=3]")


def test_sentinel_rejects_duplicated_native_spectral_resolution(tmp_path):
    xml = _sentinel_xml().replace("<RESOLUTION>10</RESOLUTION>",
                                  "<RESOLUTION>10</RESOLUTION><RESOLUTION>20</RESOLUTION>", 1)
    source = _write_stack(tmp_path / "sentinel.tif", profile=SENTINEL, xml=xml, codes=SENTINEL_CODES,
                          pixel_m=10, scl_resampling="nearest")
    with pytest.raises(ProductMetadataError) as error:
        read_product_metadata(source)
    assert error.value.field.endswith(".Spectral_Information.RESOLUTION")
