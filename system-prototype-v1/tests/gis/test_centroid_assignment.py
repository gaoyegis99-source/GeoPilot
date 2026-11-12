from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest
from shapely import MultiPolygon, Point, Polygon, box
from shapely.affinity import translate

from geopilot.gis.measurement.coverage import MeasurementPolicyError
from geopilot.gis.validation.crs_validator import CRSValidationError
from geopilot.gis.vector.boundary_assignment import assign_by_centroid


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/vector/t13/spatial-relations.gpkg"
ID = "source_feature_id"
X, Y = 500000, 4400000


@pytest.fixture
def golden():
    return pyogrio.read_dataframe(FIXTURE, layer="polygons"), pyogrio.read_dataframe(FIXTURE, layer="area")


def _frame(features):
    return gpd.GeoDataFrame(
        {ID: list(features)}, geometry=[translate(geometry, X, Y) for geometry in features.values()], crs=32650,
    )


def test_accepted_geopackage_polygon_centroids_match_manually_located_centres(golden):
    targets, zones = golden
    # The fixture's hole is (40, 40) to (60, 60) in local coordinates.
    # The first rectangle's centre is (40, 50), directly on the hole wall.
    expected_assigned = {
        "polygon-crosses-shell", "polygon-hole-edge-inside", "polygon-inside", "polygon-shell-edge-inside",
    }
    expected = {identifier: "area-with-hole" if identifier in expected_assigned else None for identifier in targets[ID]}

    assert assign_by_centroid(targets, zones) == expected
    assert len(expected) == 12


def test_real_geopackage_preserves_full_polygon_centroids_and_unassigned_ties(tmp_path):
    zones = _frame({
        "west": box(0, 0, 10, 10), "east": box(10, 0, 20, 10),
        "holed": Polygon(box(30, 0, 42, 12).exterior.coords, [box(34, 4, 38, 8).exterior.coords]),
        "overlap-a": box(50, 0, 62, 12), "overlap-b": box(56, 0, 68, 12),
        "concavity": box(104, 4, 105, 6),
    })
    targets = _frame({
        "west-interior": box(2, 2, 4, 4),
        "crossing": box(8, 4, 12, 6),
        "outer-border": box(-1, 4, 1, 6),
        "hole-centre": box(35, 5, 37, 7),
        "hole-wall": box(33, 5, 35, 7),
        "zone-ring": box(31, 5, 33, 7),
        "double-interior": box(58, 4, 60, 6),
        "overlap-wall": box(55, 4, 57, 6),
        "outside": box(75, 4, 77, 6),
        "concave": box(100, 0, 110, 10).difference(box(102, 2, 110, 8)),
        "multipart": MultiPolygon([box(7, 2, 9, 4), box(11, 1, 15, 5)]),
        "asymmetric-hole": Polygon(box(0, 0, 10, 10).exterior.coords, [box(6, 4, 8, 6).exterior.coords]),
        "centroid-in-own-hole": Polygon(box(12, 1, 18, 9).exterior.coords, [box(14, 4, 16, 6).exterior.coords]),
    })
    package = tmp_path / "centroids.gpkg"
    pyogrio.write_dataframe(targets, package, layer="targets")
    pyogrio.write_dataframe(zones, package, layer="zones")
    original = package.read_bytes()
    targets = pyogrio.read_dataframe(package, layer="targets")
    zones = pyogrio.read_dataframe(package, layer="zones")
    targets.index = [4] * len(targets)
    original_geometries = targets.geometry.to_wkb().tolist()

    expected = {
        "west-interior": "west", "crossing": None, "outer-border": None,
        "hole-centre": None, "hole-wall": None, "zone-ring": "holed",
        "double-interior": None, "overlap-wall": None, "outside": None, "concave": "concavity",
        "multipart": "east", "asymmetric-hole": "west", "centroid-in-own-hole": "east",
    }
    actual = assign_by_centroid(targets, zones, boundary_tie="unassigned")

    assert actual == expected and list(actual) == targets[ID].tolist()
    assert assign_by_centroid(targets, zones.iloc[::-1]) == expected
    assert targets.geometry.to_wkb().tolist() == original_geometries and package.read_bytes() == original

    by_id = targets.set_index(ID).geometry
    concave = by_id["concave"]
    expected_x = (100 * 105 - 48 * 106) / 52
    assert (concave.centroid.x, concave.centroid.y) == pytest.approx((X + expected_x, Y + 5), abs=1e-8, rel=0)
    assert not concave.covers(Point(X + expected_x, Y + 5))
    assert by_id["multipart"].centroid.x - X == pytest.approx((4 * 8 + 16 * 13) / 20, abs=1e-8)
    assert by_id["asymmetric-hole"].centroid.x - X == pytest.approx((100 * 5 - 4 * 7) / 96, abs=1e-8)

    # A western clip would shift the original crossing centroid off the shared boundary.
    crossing = targets.loc[targets[ID].eq("crossing")].copy()
    crossing.geometry = crossing.geometry.intersection(box(X, Y, X + 9, Y + 10))
    assert assign_by_centroid(crossing, zones) == {"crossing": "west"}
    assert actual["crossing"] is None


@pytest.mark.parametrize(("crs", "message"), [(4326, "Geographic"), (3857, "Web Mercator"), (2277, "metre")])
def test_rejects_unsuitable_crs_before_centroid(golden, crs, message):
    targets, zones = golden
    if crs == 2277:
        targets = targets.set_crs(crs, allow_override=True)
        zones = zones.set_crs(crs, allow_override=True)
    else:
        targets, zones = targets.to_crs(crs), zones.to_crs(crs)

    with pytest.raises(CRSValidationError, match=message):
        assign_by_centroid(targets, zones)


def test_requires_known_matching_crs_and_area_of_use(golden):
    targets, zones = golden
    missing = targets.set_crs(None, allow_override=True)
    with pytest.raises(CRSValidationError) as caught:
        assign_by_centroid(missing, zones)
    assert caught.value.code == "CRS_UNKNOWN"

    with pytest.raises(ValueError, match="same CRS"):
        assign_by_centroid(targets, zones.to_crs(6933))

    far = targets.set_geometry(targets.geometry.translate(xoff=600000))
    with pytest.raises(MeasurementPolicyError, match="outside the area of use"):
        assign_by_centroid(far, zones)


def test_empty_targets_and_zones_have_explicit_unassigned_semantics(golden):
    targets, zones = golden
    assert assign_by_centroid(targets.iloc[:0], zones) == {}
    assert assign_by_centroid(targets, zones.iloc[:0]) == dict.fromkeys(targets[ID])
    assert assign_by_centroid(targets.iloc[:0], zones.iloc[:0]) == {}


@pytest.mark.parametrize("invalid", [Point(X, Y), Polygon(), None,
                                     Polygon([(X, Y), (X + 10, Y + 10), (X, Y + 10), (X + 10, Y)])])
def test_requires_valid_polygonal_features(golden, invalid):
    targets, zones = golden
    wrong = targets.iloc[:1].set_geometry([invalid])
    with pytest.raises(ValueError, match="polygon"):
        assign_by_centroid(wrong, zones)


def test_rejects_missing_duplicate_and_blank_identifiers(golden):
    targets, zones = golden
    for frame in (targets.drop(columns=ID), targets.assign(source_feature_id="duplicate"),
                  targets.assign(source_feature_id=" "), targets.assign(source_feature_id=None)):
        with pytest.raises(ValueError, match=ID):
            assign_by_centroid(frame, zones)
    with pytest.raises(ValueError, match=ID):
        assign_by_centroid(targets, zones.drop(columns=ID))


def test_rejects_an_unsupported_boundary_tie_choice(golden):
    with pytest.raises(ValueError, match="boundary_tie"):
        assign_by_centroid(*golden, boundary_tie="first_zone")
