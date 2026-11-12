"""Real GeoTIFF and standard-shape metadata factories shared by P8 tests."""

from pathlib import Path

import pytest

from geopilot.remote_sensing.config_registry import load_sensor_registry
from product_fixtures import build_landsat_xml, build_sentinel_xml, write_stack


CONFIGURATION_DIRECTORY = Path(__file__).resolve().parents[2] / "config" / "sensors"


@pytest.fixture
def sensor_registry():
    return load_sensor_registry(CONFIGURATION_DIRECTORY)


@pytest.fixture
def landsat_product_xml():
    return build_landsat_xml


@pytest.fixture
def sentinel_product_xml():
    return build_sentinel_xml


@pytest.fixture
def write_product_stack():
    return write_stack
