from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from geopilot.remote_sensing.config_models import SensorConfigRef, SensorProductConfig, SensorProductKey
from geopilot.remote_sensing.config_registry import (
    SensorConfigLoadError,
    SensorConfigLookupError,
    SensorConfigRegistry,
    load_sensor_registry,
)


CONFIGURATION_DIRECTORY = Path(__file__).resolve().parents[2] / "config" / "sensors"
LANDSAT = SensorProductKey(sensor="landsat-8-oli-tirs", product_level="L2SP", processing_version="02")
SENTINEL = SensorProductKey(sensor="sentinel-2-msi", product_level="L2A", processing_version="05.11")


def _configuration(name):
    document = (CONFIGURATION_DIRECTORY / name).read_text(encoding="utf-8")
    return SensorProductConfig.model_validate(yaml.safe_load(document))


def test_real_catalog_matches_exact_product_and_configuration_versions():
    registry = load_sensor_registry(CONFIGURATION_DIRECTORY)
    cases = [
        (LANDSAT, "landsat-8-c2-l2sp", "SR_B5"),
        (LANDSAT.model_copy(update={"product_level": "L2SR"}), "landsat-8-c2-l2sr", "SR_B5"),
        (LANDSAT.model_copy(update={"sensor": "landsat-9-oli-tirs"}), "landsat-9-c2-l2sp", "SR_B5"),
        (SensorProductKey(sensor="landsat-9-oli-tirs", product_level="L2SR", processing_version="02"),
         "landsat-9-c2-l2sr", "SR_B5"),
        (SENTINEL, "sentinel-2-l2a-pb05-11", "B08"),
    ]
    for key, identifier, near_infrared in cases:
        configuration = registry.lookup(key)
        assert configuration.key == key
        assert configuration.ref == SensorConfigRef(id=identifier, version=1)
        assert configuration.band_roles["nir"].code == near_infrared
        assert registry.get(configuration.ref) == configuration
        with pytest.raises(KeyError):
            registry.get(SensorConfigRef(id=identifier, version=2))


@pytest.mark.parametrize(("base", "changes"), [
    (LANDSAT, {"sensor": "landsat-7-etm"}), (LANDSAT, {"product_level": "L2"}),
    (LANDSAT, {"processing_version": "2"}), (LANDSAT, {"processing_version": "03"}),
    (LANDSAT, {"product_level": "l2sp"}), (SENTINEL, {"processing_version": "5.11"}),
    (SENTINEL, {"processing_version": "05.12"}),
])
def test_unknown_key_is_reported_without_approximation(base, changes):
    key = base.model_copy(update=changes)
    registry = load_sensor_registry(CONFIGURATION_DIRECTORY)
    with pytest.raises(SensorConfigLookupError) as error:
        registry.lookup(key)
    assert error.value.code == "SENSOR_CONFIG_NOT_FOUND"
    assert error.value.key == key
    assert error.value.matches == ()


def test_lookup_rejects_multiple_versions_for_the_same_product_key(tmp_path):
    configuration = _configuration("landsat-8-c2-l2sp.v1.yaml")
    other = configuration.model_copy(update={"version": 2})
    for name, item in (("a-latest.yml", other), ("b-earlier.yaml", configuration)):
        (tmp_path / name).write_text(yaml.safe_dump(item.model_dump(mode="json")), encoding="utf-8")
    registry = load_sensor_registry(tmp_path)
    with pytest.raises(SensorConfigLookupError) as error:
        registry.lookup(configuration.key)
    assert error.value.code == "SENSOR_CONFIG_AMBIGUOUS"
    assert error.value.key == configuration.key
    assert error.value.matches == (configuration.ref, other.ref)
    assert registry.get(configuration.ref) == configuration
    assert registry.get(other.ref) == other


def test_registry_snapshots_isolate_nested_configuration_state():
    configuration = _configuration("landsat-8-c2-l2sp.v1.yaml")
    registry = SensorConfigRegistry([configuration])
    configuration.band_roles.pop("nir")
    matched = registry.lookup(LANDSAT)
    matched.scale_offset.clear()
    actual = registry.get(matched.ref)
    assert set(actual.scale_offset) == {"red", "nir"} and actual.band_roles["nir"].code == "SR_B5"


def test_registry_revalidates_mutated_in_memory_configuration():
    configuration = _configuration("landsat-8-c2-l2sp.v1.yaml")
    configuration.band_roles.pop("nir")
    with pytest.raises(ValidationError, match="near-infrared"):
        SensorConfigRegistry([configuration])


@pytest.mark.parametrize("version", [True, 1.0])
def test_exact_get_revalidates_a_constructed_reference(version):
    registry = load_sensor_registry(CONFIGURATION_DIRECTORY)
    reference = registry.lookup(LANDSAT).ref.model_copy(update={"version": version})
    with pytest.raises(ValidationError):
        registry.get(reference)


def test_lookup_revalidates_a_constructed_product_key():
    registry = load_sensor_registry(CONFIGURATION_DIRECTORY)
    key = LANDSAT.model_copy(update={"processing_version": 2})
    with pytest.raises(ValidationError):
        registry.lookup(key)


@pytest.mark.parametrize("document", ["version: 1\nversion: 2\n", "sensor: landsat-8-oli-tirs\n"])
def test_bad_yaml_is_rejected_with_source_location(tmp_path, document):
    source = tmp_path / "invalid.yaml"
    source.write_text(document, encoding="utf-8")
    with pytest.raises(SensorConfigLoadError) as error:
        load_sensor_registry(tmp_path)
    assert error.value.path == source


def test_exact_reference_cannot_be_defined_twice(tmp_path):
    configuration = _configuration("landsat-8-c2-l2sp.v1.yaml")
    document = yaml.safe_dump(configuration.model_dump(mode="json"))
    (tmp_path / "a.yaml").write_text(document, encoding="utf-8")
    duplicate = tmp_path / "b.yaml"
    duplicate.write_text(document, encoding="utf-8")
    with pytest.raises(SensorConfigLoadError) as error:
        load_sensor_registry(tmp_path)
    assert error.value.path == duplicate and configuration.id in str(error.value)


def test_empty_catalog_provides_a_precise_missing_key(tmp_path):
    (tmp_path / "README.md").write_text("Sensor configuration directory", encoding="utf-8")
    (tmp_path / "samples.yaml").mkdir()
    with pytest.raises(SensorConfigLookupError) as error:
        load_sensor_registry(tmp_path).lookup(SENTINEL)
    assert error.value.code == "SENSOR_CONFIG_NOT_FOUND" and error.value.key == SENTINEL
