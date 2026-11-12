from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from geopilot.remote_sensing.config_models import SensorConfigRef, SensorProductConfig, SensorProductKey


ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "config/sensors"
USGS = "https://www.usgs.gov/media/files/landsat-8-9-collection-2-level-2-science-product-guide"
LANDSAT_GROUP = "LEVEL2_SURFACE_REFLECTANCE_PARAMETERS"
SENTINEL_IMAGE = ("General_Info", "Product_Image_Characteristics")
QA_PIXEL = {"fill": 0, "dilated_cloud": 1, "cirrus": 2, "cloud": 3, "cloud_shadow": 4, "snow": 5}
QA_RADSAT = {"red_saturated": 3, "nir_saturated": 4, "terrain_occlusion": 11}


def _band(code, resolution, nodata):
    return {"code": code, "resolution_m": resolution, "nodata_dn": nodata}


def _landsat_scale(number):
    return {
        "kind": "multiply_add",
        "scale_field": {"path": [LANDSAT_GROUP, f"REFLECTANCE_MULT_BAND_{number}"]},
        "offset_field": {"path": [LANDSAT_GROUP, f"REFLECTANCE_ADD_BAND_{number}"]},
    }


def _sentinel_scale(band_id):
    return {
        "kind": "quantification",
        "quantification_field": {"path": [*SENTINEL_IMAGE, "QUANTIFICATION_VALUES_LIST", "BOA_QUANTIFICATION_VALUE"]},
        "dn_offset_field": {"path": [*SENTINEL_IMAGE, "BOA_ADD_OFFSET_VALUES_LIST", "BOA_ADD_OFFSET"], "band_id": band_id},
    }


@pytest.fixture
def landsat():
    return {
        "id": "landsat-8-c2-l2sp", "version": 1, "sensor": "landsat-8-oli-tirs",
        "product_level": "L2SP", "processing_version": "02",
        "band_roles": {"red": _band("SR_B4", 30, 0), "nir": _band("SR_B5", 30, 0),
                       "quality": _band("QA_PIXEL", 30, None), "saturation": _band("QA_RADSAT", 30, None)},
        "scale_offset": {"red": _landsat_scale(4), "nir": _landsat_scale(5)},
        "quality_rules": {
            "quality": {"kind": "bitmask", "bit_width": 16, "invalid_bits": QA_PIXEL.copy()},
            "saturation": {"kind": "bitmask", "bit_width": 16, "invalid_bits": QA_RADSAT.copy()},
        },
        "source_reference": {"title": "Landsat 8-9 Collection 2 Level 2 Science Product Guide",
                             "url": USGS, "updated_at": "2024-05-23"},
    }


@pytest.fixture
def sentinel(landsat):
    return {
        **landsat, "id": "sentinel-2-l2a-pb05-11", "sensor": "sentinel-2-msi",
        "product_level": "L2A", "processing_version": "05.11",
        "band_roles": {"red": _band("B04", 10, 0), "nir": _band("B08", 10, 0), "quality": _band("SCL", 20, None)},
        "scale_offset": {"red": _sentinel_scale(3), "nir": _sentinel_scale(7)},
        "quality_rules": {"quality": {
            "kind": "classification", "classes": {"nodata": 0, "cast_shadow": 2, "vegetation": 4, "water": 6},
            "valid_values": [4, 6],
        }},
    }


def test_exact_key_and_reference_round_trip_without_losing_collection_zero(landsat):
    configuration = SensorProductConfig.model_validate(landsat)
    assert configuration.key == SensorProductKey(sensor="landsat-8-oli-tirs", product_level="L2SP", processing_version="02")
    assert configuration.ref == SensorConfigRef(id="landsat-8-c2-l2sp", version=1)
    assert configuration.source_reference.updated_at == date(2024, 5, 23)
    assert str(configuration.source_reference.url) == USGS
    assert SensorProductConfig.model_validate_json(configuration.model_dump_json()) == configuration
    assert configuration.model_dump(mode="json") == landsat
    with pytest.raises(ValidationError):
        configuration.version = 2


@pytest.mark.parametrize("satellite", (8, 9))
@pytest.mark.parametrize("level", ("L2SP", "L2SR"))
def test_formal_landsat_profiles_bind_level2_fields_and_qa_for_each_exact_key(satellite, level):
    identifier = f"landsat-{satellite}-c2-{level.lower()}"
    config = SensorProductConfig.model_validate(yaml.safe_load((CONFIGS / f"{identifier}.v1.yaml").read_text(encoding="utf-8")))
    assert config.key == SensorProductKey(sensor=f"landsat-{satellite}-oli-tirs", product_level=level, processing_version="02")
    assert config.ref == SensorConfigRef(id=identifier, version=1)
    assert {role: band.model_dump() for role, band in config.band_roles.items()} == {
        "red": _band("SR_B4", 30, 0), "nir": _band("SR_B5", 30, 0),
        "quality": _band("QA_PIXEL", 30, None), "saturation": _band("QA_RADSAT", 30, None),
    }
    for role, number in (("red", 4), ("nir", 5)):
        assert config.scale_offset[role].model_dump(mode="json") == _landsat_scale(number)
    assert config.quality_rules["quality"].bit_width == config.quality_rules["saturation"].bit_width == 16
    assert config.quality_rules["quality"].invalid_bits == QA_PIXEL
    assert config.quality_rules["saturation"].invalid_bits == QA_RADSAT
    assert config.source_reference.url.host == "www.usgs.gov"


def test_formal_sentinel_profile_keeps_b08_distinct_and_offsets_by_zero_based_band_id():
    identifier = "sentinel-2-l2a-pb05-11"
    config = SensorProductConfig.model_validate(yaml.safe_load((CONFIGS / f"{identifier}.v1.yaml").read_text(encoding="utf-8")))
    assert config.key == SensorProductKey(sensor="sentinel-2-msi", product_level="L2A", processing_version="05.11")
    assert config.ref == SensorConfigRef(id=identifier, version=1)
    assert {role: band.model_dump() for role, band in config.band_roles.items()} == {
        "red": _band("B04", 10, 0), "nir": _band("B08", 10, 0), "quality": _band("SCL", 20, None),
    }
    assert config.scale_offset["red"].model_dump(mode="json") == _sentinel_scale(3)
    assert config.scale_offset["nir"].model_dump(mode="json") == _sentinel_scale(7)
    quality = config.quality_rules["quality"]
    assert quality.kind == "classification" and set(quality.classes.values()) == set(range(12))
    assert quality.classes["cast_shadow"] == 2 and quality.classes["cloud_shadow"] == 3
    assert quality.valid_values == (4, 5, 6)
    assert config.source_reference.url.host == "sentiwiki.copernicus.eu"
    assert config.source_reference.updated_at == date(2024, 4, 30)


@pytest.mark.parametrize("missing", [
    "id", "version", "sensor", "product_level", "processing_version", "band_roles", "scale_offset", "quality_rules",
    "source_reference",
])
def test_missing_configuration_fields_are_rejected(landsat, missing):
    landsat.pop(missing)
    with pytest.raises(ValidationError):
        SensorProductConfig.model_validate(landsat)


def _set(document, path, value):
    for component in path[:-1]:
        document = document[component]
    document[path[-1]] = value


@pytest.mark.parametrize(("path", "value"), [
    (("version",), True), (("version",), 1.0), (("version",), 0), (("version",), "1"),
    (("sensor",), " "), (("processing_version",), 2), (("processing_version",), 5.11),
    (("band_roles", "red", "code"), ""), (("band_roles", "red", "resolution_m"), "30"),
    (("band_roles", "red", "resolution_m"), 0), (("band_roles", "red", "resolution_m"), float("nan")),
    (("band_roles", "red", "nodata_dn"), False), (("band_roles", "red", "nodata_dn"), -1),
    (("band_roles", "nir"), _band("SR_B4", 30, 0)),
    (("scale_offset", "red", "scale_field", "path"), []),
    (("scale_offset", "red", "offset_field", "path"), ["../metadata"]),
    (("scale_offset", "red", "scale_field", "path"), [LANDSAT_GROUP, "REFLECTANCE_ADD_BAND_4"]),
    (("scale_offset", "red", "scale"), 0.0000275),
    (("quality_rules", "quality", "bit_width"), 0),
    (("quality_rules", "quality", "invalid_bits"), {}),
    (("quality_rules", "quality", "invalid_bits"), {"cloud": 16}),
    (("quality_rules", "quality", "invalid_bits"), {"cloud": -1}),
    (("quality_rules", "quality", "invalid_bits"), {"cloud": True}),
    (("quality_rules", "quality", "invalid_bits"), {"cloud": 3, "shadow": 3}),
    (("source_reference", "url"), "../manual.pdf"),
    (("source_reference", "updated_at"), "2024-13-30"),
    (("source_reference", "updated_at"), "2024-05-23T12:00:00Z"),
    (("source_reference", "title"), ""),
])
def test_strict_identity_radiometry_qa_and_provenance_validation(landsat, path, value):
    _set(landsat, path, value)
    with pytest.raises(ValidationError):
        SensorProductConfig.model_validate(landsat)


@pytest.mark.parametrize(("mapping", "key"), [
    ("band_roles", "nir"), ("scale_offset", "red"), ("quality_rules", "saturation"), ("source_reference", "updated_at"),
])
def test_missing_linked_band_scale_quality_or_source_values_fail(landsat, mapping, key):
    landsat[mapping].pop(key)
    with pytest.raises(ValidationError):
        SensorProductConfig.model_validate(landsat)


def test_unknown_quality_role_and_quality_on_a_spectral_band_fail(landsat):
    landsat["quality_rules"]["undeclared"] = landsat["quality_rules"]["quality"]
    with pytest.raises(ValidationError):
        SensorProductConfig.model_validate(landsat)
    landsat["quality_rules"]["red"] = landsat["quality_rules"].pop("undeclared")
    with pytest.raises(ValidationError):
        SensorProductConfig.model_validate(landsat)


@pytest.mark.parametrize(("path", "value"), [
    (("scale_offset", "nir", "dn_offset_field", "band_id"), True),
    (("scale_offset", "nir", "dn_offset_field", "band_id"), -1),
    (("scale_offset", "nir", "dn_offset_field", "band_id"), 3),
    (("scale_offset", "nir", "quantification_field", "band_id"), 7),
    (("quality_rules", "quality", "classes"), {"water": 6, "duplicate": 6}),
    (("quality_rules", "quality", "valid_values"), [5]),
    (("quality_rules", "quality", "valid_values"), [4, 4]),
    (("quality_rules", "quality", "valid_values"), []),
    (("quality_rules", "quality", "valid_values"), [True]),
])
def test_sentinel_metadata_selection_and_scl_membership_are_strict(sentinel, path, value):
    _set(sentinel, path, value)
    with pytest.raises(ValidationError):
        SensorProductConfig.model_validate(sentinel)


def test_json_schema_exposes_the_two_scaling_and_quality_encodings(landsat):
    schema = SensorProductConfig.model_json_schema()
    assert set(schema["required"]) == set(landsat) and schema["additionalProperties"] is False
    for name in ("band_roles", "scale_offset", "quality_rules"):
        assert schema["properties"][name]["additionalProperties"] is False
    for name in ("scale_offset", "quality_rules"):
        entry, = schema["properties"][name]["patternProperties"].values()
        assert entry["discriminator"]["propertyName"] == "kind"
    assert schema["$defs"]["BitmaskQualityRule"]["properties"]["invalid_bits"]["additionalProperties"] is False
    assert schema["$defs"]["ClassificationQualityRule"]["properties"]["classes"]["additionalProperties"] is False
