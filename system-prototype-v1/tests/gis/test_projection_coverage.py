import pytest
from pyproj import CRS, Transformer

from geopilot.gis.measurement.coverage import (
    MeasurementPolicyError,
    verify_projection_coverage,
)


@pytest.mark.parametrize("longitude", [114.1, 117.0, 119.9])
def test_utm_coverage_uses_geographic_coordinates(longitude):
    crs = CRS.from_epsg(32650)
    easting, northing = Transformer.from_crs(4326, crs, always_xy=True).transform(longitude, 40)
    bounds = (easting, northing, easting, northing)

    assert verify_projection_coverage(crs, bounds, field="extent") == pytest.approx(
        (longitude, 40, longitude, 40)
    )


def test_utm_coverage_rejects_input_in_adjacent_zone():
    crs = CRS.from_epsg(32650)
    easting, northing = Transformer.from_crs(4326, crs, always_xy=True).transform(123, 40)

    with pytest.raises(MeasurementPolicyError, match="area of use") as caught:
        verify_projection_coverage(crs, (easting, northing, easting, northing), field="extent")

    assert caught.value.code == "MEASUREMENT_POLICY_REJECTED"
    assert caught.value.phase == "preflight"
    assert caught.value.field == "extent"
    assert caught.value.observed == pytest.approx((123, 40, 123, 40))


def test_coverage_rejects_missing_bounds():
    with pytest.raises(MeasurementPolicyError, match="bounds") as caught:
        verify_projection_coverage(CRS.from_epsg(32650), None, field="extent")

    assert caught.value.field == "extent"
    assert caught.value.observed is None


def test_coverage_rejects_crs_without_documented_area():
    crs = CRS.from_proj4("+proj=utm +zone=50 +datum=WGS84 +units=m +type=crs")
    assert crs.area_of_use is None

    with pytest.raises(MeasurementPolicyError, match="no documented area of use"):
        verify_projection_coverage(crs, (500000, 4400000, 500000, 4400000), field="extent")
