from pathlib import Path

import numpy as np
import pyogrio
import pytest
import rasterio
from pyproj import CRS
from rasterio.transform import from_origin

from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.data.vector_reader import read_vector_metadata
from geopilot.gis.validation.crs_validator import (
    CRSInfo,
    CRSValidationError,
    inspect_crs,
    validate_crs_operation,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ROADS = FIXTURES / "vector" / "road-buffer.gpkg"
DEM = FIXTURES / "raster" / "dem-plane.tif"


def _write_and_reopen_raster(path, crs):
    with rasterio.open(
        path, "w", driver="GTiff", width=2, height=2, count=1, dtype="uint8",
        transform=from_origin(500000, 4400000, 10, 10), crs=crs,
    ) as raster:
        raster.write(np.ones((2, 2), dtype="uint8"), 1)
    return read_raster_metadata(path)


def _assert_crs_error(error, code, field, observed):
    assert error.code == code
    assert error.phase == "preflight"
    assert error.field == field
    assert error.message.strip()
    assert str(observed) in str(error.observed)


@pytest.mark.parametrize("dataset", ["dem", "roads"])
def test_real_projected_metre_assets_are_inspected(dataset):
    if dataset == "dem":
        metadata = read_raster_metadata(DEM)
    else:
        _, metadata = read_geopackage_metadata(ROADS, layer="roads")

    info = inspect_crs(metadata, require_projected_metres=True)
    assert isinstance(info, CRSInfo)
    assert info.crs == CRS.from_epsg(32650)
    assert info.horizontal_unit.lower() in {"metre", "meter"}
    assert info.metres_per_unit == 1
    assert info.is_web_mercator is False


def test_real_geographic_road_is_readable_but_rejected_for_planar_metres(tmp_path):
    source = tmp_path / "roads.geojson"
    roads = pyogrio.read_dataframe(ROADS, layer="roads").to_crs(4326)
    pyogrio.write_dataframe(roads, source, driver="GeoJSON")
    metadata = read_vector_metadata(source)

    info = inspect_crs(metadata)
    assert info.crs.is_geographic
    assert "degree" in info.horizontal_unit.lower()
    assert info.metres_per_unit is None
    with pytest.raises(CRSValidationError) as error:
        inspect_crs(metadata, field="inputs.roads.crs", require_projected_metres=True)
    _assert_crs_error(error.value, "MEASUREMENT_POLICY_REJECTED", "inputs.roads.crs", metadata.crs)


def _web_mercator_without_authority():
    wkt = CRS.from_epsg(3857).to_wkt().replace("WGS 84 / Pseudo-Mercator", "GeoPilot display grid", 1)
    return wkt.removesuffix(',ID["EPSG",3857]]') + "]"


@pytest.mark.parametrize("variant", ["epsg", "equivalent-wkt"])
def test_web_mercator_cannot_be_used_for_planar_metres_even_without_epsg_id(tmp_path, variant):
    crs = "EPSG:3857" if variant == "epsg" else _web_mercator_without_authority()
    metadata = _write_and_reopen_raster(tmp_path / "display.tif", crs)
    if variant == "equivalent-wkt":
        source_crs = CRS.from_user_input(metadata.crs)
        assert source_crs.to_epsg(min_confidence=100) is None
        assert source_crs.equals(CRS.from_epsg(3857), ignore_axis_order=True)

    info = inspect_crs(metadata)
    assert info.is_web_mercator is True
    assert info.metres_per_unit == 1
    with pytest.raises(CRSValidationError) as error:
        inspect_crs(metadata, require_projected_metres=True)
    _assert_crs_error(error.value, "MEASUREMENT_POLICY_REJECTED", "inputs.source.crs", metadata.crs)


def test_deprecated_web_mercator_definition_is_recognized(tmp_path):
    with rasterio.Env(OSR_USE_NON_DEPRECATED="NO"):
        metadata = _write_and_reopen_raster(tmp_path / "legacy-display.tif", "EPSG:3785")
    assert inspect_crs(metadata).is_web_mercator is True

    # GDAL can normalize a deprecated definition. Exercise its explicit authority as well.
    legacy = metadata.model_copy(update={"crs": "EPSG:3785"})
    assert not CRS.from_epsg(3785).equals(CRS.from_epsg(3857))
    with pytest.raises(CRSValidationError) as error:
        inspect_crs(legacy, require_projected_metres=True)
    _assert_crs_error(error.value, "MEASUREMENT_POLICY_REJECTED", "inputs.source.crs", "EPSG:3785")


def test_us_survey_foot_unit_is_reported_and_rejected_for_metre_policy(tmp_path):
    metadata = _write_and_reopen_raster(tmp_path / "foot.tif", "EPSG:2277")
    info = inspect_crs(metadata)
    assert info.crs == CRS.from_epsg(2277)
    assert "foot" in info.horizontal_unit.lower()
    assert info.metres_per_unit == pytest.approx(1200 / 3937)
    assert info.is_web_mercator is False
    with pytest.raises(CRSValidationError) as error:
        inspect_crs(metadata, require_projected_metres=True)
    _assert_crs_error(error.value, "MEASUREMENT_POLICY_REJECTED", "inputs.source.crs", metadata.crs)


def test_a_radian_axis_with_unit_factor_one_does_not_pass_metre_policy():
    projected = CRS.from_epsg(32650).to_wkt()
    head, metre, tail = projected.rpartition('LENGTHUNIT["metre",1]')
    assert metre and "north,ORDER[2]" in head[-80:]
    observed = head + 'ANGLEUNIT["radian",1]' + tail
    mixed = CRS.from_wkt(observed)
    assert mixed.is_projected and mixed.axis_info[1].unit_name == "radian"
    assert all(axis.unit_conversion_factor == 1 for axis in mixed.axis_info)
    metadata = read_raster_metadata(DEM).model_copy(update={"crs": observed})

    with pytest.raises(CRSValidationError) as error:
        inspect_crs(metadata, require_projected_metres=True)
    _assert_crs_error(error.value, "MEASUREMENT_POLICY_REJECTED", "inputs.source.crs", observed)


def test_unknown_crs_is_not_inferred_from_projected_looking_coordinates(tmp_path):
    metadata = _write_and_reopen_raster(tmp_path / "unknown.tif", None)
    assert metadata.crs is None
    assert metadata.bounds == (500000, 4399980, 500020, 4400000)
    with pytest.raises(CRSValidationError) as error:
        inspect_crs(metadata, field="inputs.dem.crs")
    _assert_crs_error(error.value, "CRS_UNKNOWN", "inputs.dem.crs", None)


def test_unparseable_crs_returns_original_observation():
    metadata = read_raster_metadata(DEM).model_copy(update={"crs": "not-a-crs"})
    with pytest.raises(CRSValidationError) as error:
        inspect_crs(metadata)
    _assert_crs_error(error.value, "CRS_UNKNOWN", "inputs.source.crs", "not-a-crs")


def test_explicit_assignment_and_known_to_known_reprojection_are_distinct(tmp_path):
    source = tmp_path / "unknown.tif"
    unknown = _write_and_reopen_raster(source, None)
    original = source.read_bytes()
    validate_crs_operation(unknown.crs, "EPSG:32650", operation="assign")
    validate_crs_operation(CRS.from_epsg(4326), "EPSG:32650", operation="reproject")
    assert read_raster_metadata(source).crs is None
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "source,target,operation,code,field_part",
    [
        ("EPSG:4326", "EPSG:32650", "assign", "MEASUREMENT_POLICY_REJECTED", "source"),
        (None, None, "assign", "CRS_UNKNOWN", "target"),
        (None, "EPSG:32650", "reproject", "CRS_UNKNOWN", "source"),
        ("EPSG:4326", None, "reproject", "CRS_UNKNOWN", "target"),
    ],
)
def test_operation_validates_source_and_target_separately(source, target, operation, code, field_part):
    with pytest.raises(CRSValidationError) as error:
        validate_crs_operation(source, target, operation=operation)
    assert field_part in error.value.field
    observed = source if field_part == "source" else target
    _assert_crs_error(error.value, code, error.value.field, observed)
