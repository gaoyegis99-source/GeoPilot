import geopandas as gpd
import pyogrio
import pytest
from shapely.geometry import MultiPolygon, Polygon, box

from geopilot.gis.measurement.coverage import MeasurementPolicyError
from geopilot.gis.validation.crs_validator import CRSValidationError
from geopilot.gis.vector.boundary_assignment import assign_by_largest_overlap


CRS = "EPSG:6933"
X, Y = 1_000_000, 2_000_000


def _box(left, bottom, right, top):
    return box(X + left, Y + bottom, X + right, Y + top)


def _persist(package, layer, features):
    frame = gpd.GeoDataFrame({"source_feature_id": list(features)}, geometry=list(features.values()), crs=CRS)
    pyogrio.write_dataframe(frame, package, driver="GPKG", layer=layer, promote_to_multi=False)
    return pyogrio.read_dataframe(package, layer=layer)


@pytest.fixture
def adjacent_zones(tmp_path):
    package = tmp_path / "largest-overlap.gpkg"
    zones = _persist(package, "zones", {"west": _box(0, 0, 2_000, 4_000), "east": _box(2_000, 0, 4_000, 4_000)})
    return package, zones


def test_largest_overlap_uses_complete_geometry_and_clipping_can_change_the_winner(adjacent_zones):
    package, zones = adjacent_zones
    original = _persist(package, "original_targets", {"crossing": _box(1_960, 10, 2_060, 30)})
    extent = _persist(package, "analysis_extent", {"extent": _box(1_950, 0, 2_020, 100)})
    clipped_geometry = original.geometry.item().intersection(extent.geometry.item())
    clipped = _persist(package, "clipped_targets", {"crossing": clipped_geometry})
    assert original.geometry.item().area == 2_000 and clipped.geometry.item().area == 1_200
    assert clipped.geometry.item().equals(_box(1_960, 10, 2_020, 30))

    assert assign_by_largest_overlap(original, zones) == {"crossing": "east"}
    assert assign_by_largest_overlap(clipped, zones) == {"crossing": "west"}


def test_equal_and_nearly_equal_positive_areas_are_unassigned_in_both_zone_orders(adjacent_zones):
    package, zones = adjacent_zones
    targets = _persist(package, "targets", {
        "equal": _box(1_990, 10, 2_010, 20),
        "absolute-tie": _box(1_990, 30, 2_010 + 2e-8, 40),
        "absolute-winner": _box(1_990, 50, 2_010 + 1e-6, 60),
        "relative-tie": _box(1_000, 100, 3_000 + 5e-7, 1_100),
        "relative-winner": _box(1_000, 1_200, 3_000 + 3e-6, 2_200),
    })
    expected = {
        "equal": None, "absolute-tie": None, "absolute-winner": "east",
        "relative-tie": None, "relative-winner": "east",
    }

    assert assign_by_largest_overlap(targets, zones) == expected
    assert assign_by_largest_overlap(targets, zones.iloc[::-1]) == expected


def test_zero_area_contacts_are_ignored_and_a_unique_tiny_overlap_is_retained(adjacent_zones):
    package, zones = adjacent_zones
    targets = _persist(package, "targets", {
        "touch-only": _box(4_000, 10, 4_010, 20),
        "outside": _box(4_020, 10, 4_030, 20),
        "tiny-west": _box(2_000 - 5e-8, 100, 2_000, 101),
    })
    tiny = targets.loc[targets.source_feature_id.eq("tiny-west"), "geometry"].item()
    assert 0 < tiny.area < 1e-6

    assert assign_by_largest_overlap(targets, zones) == {"touch-only": None, "outside": None, "tiny-west": "west"}


def test_holes_and_multipart_shapes_use_their_actual_intersection_area(tmp_path):
    package = tmp_path / "multipart.gpkg"
    ring = Polygon(_box(0, 0, 80, 80).exterior.coords, [_box(20, 20, 60, 60).exterior.coords])
    islands = MultiPolygon([_box(25, 25, 35, 35), _box(45, 45, 55, 55)])
    zones = _persist(package, "zones", {"ring": ring, "islands": islands})
    targets = _persist(package, "targets", {
        "crosses-ring-and-island": _box(15, 25, 55, 35),
        "in-uncovered-hole": _box(37, 37, 43, 43),
        "multipart-target": MultiPolygon([_box(2, 2, 6, 6), _box(25, 25, 30, 30)]),
    })

    assert assign_by_largest_overlap(targets, zones) == {
        "crosses-ring-and-island": "islands", "in-uncovered-hole": None, "multipart-target": "islands",
    }


def test_overlapping_zones_compare_individual_areas_and_require_a_unique_winner(tmp_path):
    package = tmp_path / "overlap.gpkg"
    zones = _persist(package, "zones", {"west": _box(0, 0, 20, 20), "east": _box(5, 0, 25, 20)})
    targets = _persist(package, "targets", {"west-larger": _box(0, 0, 15, 10), "inside-both": _box(6, 0, 14, 10)})

    assert assign_by_largest_overlap(targets, zones) == {"west-larger": "west", "inside-both": None}


@pytest.mark.parametrize("role", ["targets", "zones"])
def test_invalid_geometry_fails_with_its_layer(adjacent_zones, role):
    package, zones = adjacent_zones
    targets = _persist(package, "targets", {"valid": _box(10, 10, 20, 20)})
    bad = Polygon([(X, Y), (X + 10, Y + 10), (X + 10, Y), (X, Y + 10)])
    invalid = _persist(package, "invalid", {"bow-tie": bad})
    assert not invalid.geometry.item().is_valid

    with pytest.raises(ValueError, match=rf"{role}.*invalid"):
        assign_by_largest_overlap(invalid if role == "targets" else targets, invalid if role == "zones" else zones)


def test_requires_a_matching_equal_area_crs_and_unassigned_tie_policy(adjacent_zones):
    package, zones = adjacent_zones
    targets = _persist(package, "targets", {"inside": _box(10, 10, 20, 20)})

    with pytest.raises(CRSValidationError, match="same CRS") as mismatch:
        assign_by_largest_overlap(targets, zones.to_crs("EPSG:4326"))
    assert mismatch.value.field == "inputs.zones.crs"
    with pytest.raises(MeasurementPolicyError, match="equal-area"):
        assign_by_largest_overlap(targets.to_crs("EPSG:32632"), zones.to_crs("EPSG:32632"))
    with pytest.raises(ValueError, match="boundary_tie"):
        assign_by_largest_overlap(targets, zones, boundary_tie="first")


def test_empty_polygon_layers_keep_deterministic_results(adjacent_zones):
    package, zones = adjacent_zones
    targets = _persist(package, "targets", {"inside": _box(10, 10, 20, 20)})

    assert assign_by_largest_overlap(targets, zones.iloc[:0]) == {"inside": None}
    assert assign_by_largest_overlap(targets.iloc[:0], zones) == {}
    assert assign_by_largest_overlap(targets.iloc[:0], zones.iloc[:0]) == {}
