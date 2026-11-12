import math

import geopandas as gpd
import pyogrio
import pytest
from shapely import MultiPolygon, Polygon, box
from shapely.affinity import translate

from geopilot.gis.measurement.coverage import MeasurementPolicyError
from geopilot.gis.validation.crs_validator import CRSValidationError
from geopilot.gis.vector.boundary_assignment import split_area_by_zone


ID = "source_feature_id"
X, Y = 1000000, 2000000


def _frame(features):
    return gpd.GeoDataFrame(
        {ID: list(features)}, geometry=[translate(geometry, X, Y) for geometry in features.values()], crs=6933,
    )


def _package(path, targets, zones):
    pyogrio.write_dataframe(_frame(targets), path, layer="targets")
    pyogrio.write_dataframe(_frame(zones), path, layer="zones")
    return pyogrio.read_dataframe(path, layer="targets"), pyogrio.read_dataframe(path, layer="zones")


@pytest.fixture
def parcels(tmp_path):
    package = tmp_path / "parcels.gpkg"
    targets, zones = _package(package, {
        "clipped-crossing": box(-2, 2, 14, 8).intersection(box(2, 0, 9, 10)),
        "partial": box(8, 2, 12, 8),
        "outside": box(15, 2, 17, 4),
        "multipart": MultiPolygon([box(0, 0, 2, 2), box(7, 1, 9, 4)]),
        "target-hole": Polygon(box(2, 2, 9, 8).exterior.coords, [box(4, 3, 8, 5).exterior.coords]),
        "zone-hole": box(22, 2, 28, 8),
    }, {
        "west": box(0, 0, 6, 10), "east": box(6, 0, 10, 10), "touch-only": box(12, 0, 14, 10),
        "ring": Polygon(box(20, 0, 30, 10).exterior.coords, [box(23, 3, 27, 7).exterior.coords]),
    })
    return package, targets, zones


def test_equal_area_geopackage_allocations_match_independent_rectangle_areas(parcels):
    package, targets, zones = parcels
    original = package.read_bytes()
    assert targets.crs.to_epsg() == zones.crs.to_epsg() == 6933

    expected = {
        "clipped-crossing": {"west": 24.0, "east": 18.0},
        "partial": {"east": 12.0, None: 12.0},
        "outside": {None: 4.0},
        "multipart": {"west": 4.0, "east": 6.0},
        "target-hole": {"west": 20.0, "east": 14.0},
        "zone-hole": {"ring": 20.0, None: 16.0},
    }
    actual = split_area_by_zone(targets, zones)

    assert actual == expected and list(actual) == targets[ID].tolist()
    assert split_area_by_zone(targets, zones.iloc[::-1]) == expected
    assert [math.fsum(allocations.values()) for allocations in actual.values()] == [42, 24, 4, 10, 34, 36]
    assert package.read_bytes() == original


def test_overlap_is_rejected_only_for_positive_area_on_the_target(tmp_path):
    targets, zones = _package(tmp_path / "overlap.gpkg", {
        "separate-parts": MultiPolygon([box(1, 1, 3, 2), box(6, 1, 8, 2)]),
        "overlap-wall": box(0, 1, 4, 2),
        "inside-overlap": box(3, 1, 6, 5),
    }, {"west": box(0, 0, 5, 10), "east": box(4, 0, 9, 10)})
    subset = targets.loc[~targets[ID].eq("inside-overlap")]

    assert split_area_by_zone(subset, zones) == {
        "separate-parts": {"west": 2.0, "east": 2.0}, "overlap-wall": {"west": 4.0},
    }
    with pytest.raises(ValueError, match="overlap.*inside-overlap") as caught:
        split_area_by_zone(targets, zones)
    assert "west" in str(caught.value) and "east" in str(caught.value)


def test_numeric_area_tolerance_does_not_hide_positive_overlaps_or_gaps(tmp_path):
    width = 1e-8
    targets, zones = _package(tmp_path / "narrow.gpkg", {"parcel": box(0, 0, 10, 1)}, {
        "west": box(0, 0, 5, 1), "east-with-gap": box(5 + width, 0, 10, 1),
        "east-with-overlap": box(5 - width, 0, 10, 1),
    })

    gap = split_area_by_zone(targets, zones.loc[~zones[ID].eq("east-with-overlap")])["parcel"]
    assert 0 < gap[None] < 1e-6
    assert gap[None] == pytest.approx(width, abs=1e-10, rel=0)
    assert math.fsum(gap.values()) == pytest.approx(10, abs=1e-10)
    with pytest.raises(ValueError, match="overlap.*parcel"):
        split_area_by_zone(targets, zones.loc[~zones[ID].eq("east-with-gap")])


def test_oblique_shared_boundary_conserves_area_with_fractional_intersection(tmp_path):
    targets, zones = _package(tmp_path / "oblique.gpkg", {"rectangle": box(0, 0, 2, 1)}, {
        "below": Polygon([(0, 0), (3, 0), (3, 1)]),
        "above": Polygon([(0, 0), (3, 1), (3, 2), (0, 2)]),
    })

    split = split_area_by_zone(targets, zones)["rectangle"]

    # The split line is y = x / 3. Integrating from x = 0 to 2 gives 2/3 square metres below it.
    assert split == pytest.approx({"below": 2 / 3, "above": 4 / 3}, abs=1e-8, rel=0)
    assert math.fsum(split.values()) == pytest.approx(2, abs=1e-8, rel=0)


@pytest.mark.parametrize("epsg", [4326, 3857, 32632])
def test_split_requires_actual_equal_area_projection(parcels, epsg):
    _, targets, zones = parcels
    with pytest.raises(MeasurementPolicyError):
        split_area_by_zone(targets.to_crs(epsg), zones.to_crs(epsg))


def test_split_rejects_mismatched_and_missing_crs(parcels):
    _, targets, zones = parcels
    with pytest.raises(ValueError, match="same CRS"):
        split_area_by_zone(targets, zones.to_crs(8857))
    with pytest.raises(CRSValidationError) as caught:
        split_area_by_zone(targets.set_crs(None, allow_override=True), zones)
    assert caught.value.code == "CRS_UNKNOWN"


def test_rejects_equal_area_crs_outside_its_documented_coverage():
    polar = gpd.GeoDataFrame({ID: ["polar"]}, geometry=[box(0, 87, 0.1, 87.1)], crs=4326).to_crs(6933)
    with pytest.raises(MeasurementPolicyError, match="outside the area of use"):
        split_area_by_zone(polar, polar)


def test_points_do_not_support_area_split(parcels):
    _, targets, zones = parcels
    points = targets.set_geometry(targets.geometry.centroid)
    with pytest.raises(ValueError, match="polygon"):
        split_area_by_zone(points, zones)


def test_no_targets_or_no_covering_zones_have_explicit_results(parcels):
    _, targets, zones = parcels
    assert split_area_by_zone(targets.iloc[:0], zones) == {}
    assert split_area_by_zone(targets, zones.iloc[:0]) == {
        key: {None: area} for key, area in zip(targets[ID], (42, 24, 4, 10, 34, 36), strict=True)
    }
    assert split_area_by_zone(targets.iloc[:0], zones.iloc[:0]) == {}
    with pytest.raises(MeasurementPolicyError, match="equal-area"):
        split_area_by_zone(targets.iloc[:0].to_crs(32632), zones.iloc[:0].to_crs(32632))
