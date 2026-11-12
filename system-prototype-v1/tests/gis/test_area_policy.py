import geopandas as gpd
import pyogrio
import pytest
from pyproj import CRS
from shapely.geometry import box

from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.gis.measurement.area import validate_area
from geopilot.gis.measurement.coverage import MeasurementDecision, MeasurementPolicyError
from geopilot.gis.validation.crs_validator import CRSValidationError


def _write_and_reopen(path, epsg, geographic_bounds):
    frame = gpd.GeoDataFrame(
        {"parcel_id": ["p1"]}, geometry=[box(*geographic_bounds)], crs="EPSG:4326"
    ).to_crs(epsg=epsg)
    pyogrio.write_dataframe(frame, path, layer="parcels", driver="GPKG")
    selected, metadata = read_geopackage_metadata(path, "parcels")
    assert selected == "parcels"
    assert metadata.geometry_type == "Polygon" and metadata.feature_count == 1
    assert CRS.from_user_input(metadata.crs).to_epsg() == epsg
    return metadata


@pytest.mark.parametrize("epsg", [4326, 3857, 32650])
def test_real_parcels_reject_geographic_web_mercator_and_utm_area(tmp_path, epsg):
    metadata = _write_and_reopen(tmp_path / "parcels.gpkg", epsg, (116, 39, 116.1, 39.1))

    with pytest.raises(MeasurementPolicyError) as caught:
        validate_area(metadata, field="inputs.parcels.crs")
    error = caught.value
    assert error.code == "MEASUREMENT_POLICY_REJECTED"
    assert error.phase == "preflight" and error.field == "inputs.parcels.crs"
    assert metadata.crs in str(error.observed)
    assert error.message


@pytest.mark.parametrize(
    "epsg,method,geographic_bounds",
    [
        (6933, "9835", (116, 39, 116.1, 39.1)),
        (5070, "9822", (-102, 38, -101.9, 38.1)),
        (3035, "9820", (5, 51, 5.1, 51.1)),
        (8857, "1078", (116, 39, 116.1, 39.1)),
    ],
)
def test_real_equal_area_parcels_return_observed_crs_and_square_metres(
    tmp_path, epsg, method, geographic_bounds
):
    source = tmp_path / "parcels.gpkg"
    metadata = _write_and_reopen(source, epsg, geographic_bounds)
    before = source.read_bytes()
    operation = CRS.from_user_input(metadata.crs).coordinate_operation
    assert (operation.method_auth_name, operation.method_code) == ("EPSG", method)

    assert validate_area(metadata) == MeasurementDecision(
        method="equal_area", crs=metadata.crs, unit="square_meter"
    )
    assert source.read_bytes() == before


def test_real_equal_area_crs_outside_declared_area_is_blocked(tmp_path):
    metadata = _write_and_reopen(tmp_path / "outside.gpkg", 6933, (0, 87, 0.1, 87.1))
    assert CRS.from_epsg(6933).area_of_use.north == 86

    with pytest.raises(MeasurementPolicyError) as caught:
        validate_area(metadata, field="inputs.parcels.crs")
    assert caught.value.field == "inputs.parcels.crs"
    assert caught.value.observed is not None


def test_equal_area_crs_without_extent_evidence_is_blocked(tmp_path):
    metadata = _write_and_reopen(tmp_path / "parcels.gpkg", 6933, (116, 39, 116.1, 39.1))
    incomplete = metadata.model_copy(update={"bounds": None})

    with pytest.raises(MeasurementPolicyError) as caught:
        validate_area(incomplete, field="inputs.parcels.crs")
    assert caught.value.field == "inputs.parcels.crs"
    assert caught.value.observed is None


def test_crs_display_name_cannot_turn_a_transverse_mercator_into_equal_area(tmp_path):
    metadata = _write_and_reopen(tmp_path / "parcels.gpkg", 32650, (116, 39, 116.1, 39.1))
    crs = CRS.from_user_input(metadata.crs)
    renamed_wkt = crs.to_wkt().replace(crs.name, "GeoPilot Equal Area", 1)
    renamed = CRS.from_wkt(renamed_wkt)
    assert renamed.name == "GeoPilot Equal Area"
    assert renamed.coordinate_operation.method_code == "9807"

    with pytest.raises(MeasurementPolicyError):
        validate_area(metadata.model_copy(update={"crs": renamed_wkt}))


def test_equal_area_method_code_must_match_the_projection_compiled_by_proj(tmp_path):
    metadata = _write_and_reopen(tmp_path / "parcels.gpkg", 32650, (116, 39, 116.1, 39.1))
    actual_wkt = CRS.from_user_input(metadata.crs).to_wkt()
    conflicting_wkt = actual_wkt.replace('ID["EPSG",9807]', 'ID["EPSG",9835]')
    operation = CRS.from_wkt(conflicting_wkt).coordinate_operation
    assert (operation.method_auth_name, operation.method_code) == ("EPSG", "9835")
    assert "+proj=tmerc" in operation.to_proj4().split()

    with pytest.raises(MeasurementPolicyError) as caught:
        validate_area(metadata.model_copy(update={"crs": conflicting_wkt}))
    assert caught.value.observed == conflicting_wkt


def test_unknown_crs_remains_unknown_for_area(tmp_path):
    metadata = _write_and_reopen(tmp_path / "parcels.gpkg", 6933, (116, 39, 116.1, 39.1))
    unknown = metadata.model_copy(update={"crs": None, "issues": ("CRS_UNKNOWN",)})

    with pytest.raises(CRSValidationError) as caught:
        validate_area(unknown, field="inputs.parcels.crs")
    assert caught.value.code == "CRS_UNKNOWN"
    assert caught.value.field == "inputs.parcels.crs" and caught.value.observed is None
