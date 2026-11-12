import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
from geopandas.testing import assert_geodataframe_equal
from shapely.geometry import MultiPolygon, Point, Polygon, box

from geopilot.gis.vector.spatial_relations import select_intersecting


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/vector/t13"
CRS = "EPSG:32650"


@pytest.fixture(scope="module")
def golden():
    expected = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))
    package = FIXTURE / expected["input_file"]
    area = pyogrio.read_dataframe(package, layer=expected["reference"]["layer"])
    return package, area, expected


@pytest.mark.parametrize("layer", ["points", "lines", "polygons"])
def test_intersects_selects_exact_golden_ids_and_preserves_original_rows(golden, layer):
    package, area, expected = golden
    targets = pyogrio.read_dataframe(package, layer=layer).iloc[::-1].rename_geometry("original_geometry")
    targets.index = pd.Index([f"row-{position // 2}" for position in range(len(targets))], name="source_index")
    targets["payload"] = range(len(targets))
    original_targets, original_area = targets.copy(), area.copy()
    expected_ids = expected["relations"]["intersects"][layer]
    retained = targets.iloc[targets.source_feature_id.isin(expected_ids).to_numpy()]

    selected = select_intersecting(targets, area)

    assert sorted(selected.source_feature_id) == expected_ids
    assert set(expected["boundary_only_ids"][layer]).issubset(selected.source_feature_id)
    assert_geodataframe_equal(selected, retained)
    assert selected.geometry.name == "original_geometry"
    assert selected.geometry.to_wkb().tolist() == retained.geometry.to_wkb().tolist()
    assert_geodataframe_equal(targets, original_targets)
    assert_geodataframe_equal(area, original_area)


def test_multipart_area_uses_the_union_without_filling_its_gap():
    area = gpd.GeoDataFrame(
        geometry=[box(0, 0, 4, 4), MultiPolygon([box(4, 0, 6, 4), box(8, 0, 12, 4)])], crs=CRS,
    )
    targets = gpd.GeoDataFrame(
        {"source_feature_id": ["second-part", "first-part", "outer-edge", "gap"]},
        geometry=[Point(9, 2), Point(2, 2), Point(4, 4), Point(7, 2)], crs=CRS,
    )

    selected = select_intersecting(targets, area)

    assert selected.source_feature_id.tolist() == ["second-part", "first-part", "outer-edge"]


def test_equivalent_known_crs_uses_original_coordinates():
    area = gpd.GeoDataFrame(geometry=[box(116, 39, 118, 41)], crs="OGC:CRS84")
    targets = gpd.GeoDataFrame(geometry=[Point(117, 40), Point(119, 40)], crs="EPSG:4326")

    selected = select_intersecting(targets, area)

    assert_geodataframe_equal(selected, targets.iloc[[0]])
    assert selected.crs.to_string() == "EPSG:4326"


@pytest.mark.parametrize("role", ["targets", "area"])
@pytest.mark.parametrize("problem", ["missing", "empty", "invalid"])
def test_rejects_a_bad_individual_geometry_with_its_identity(golden, role, problem):
    package, area, _ = golden
    targets = pyogrio.read_dataframe(package, layer="polygons")
    frame = targets if role == "targets" else area
    geometries = {
        "missing": None,
        "empty": Polygon(),
        "invalid": Polygon([(500010, 4400010), (500020, 4400020), (500020, 4400010), (500010, 4400020)]),
    }
    broken = gpd.GeoDataFrame(
        {"source_feature_id": ["first-valid", "bad-feature"]},
        geometry=[frame.geometry.iloc[0], geometries[problem]], crs=frame.crs, index=["first-index", "bad-index"],
    )

    with pytest.raises(ValueError) as raised:
        select_intersecting(broken if role == "targets" else targets, broken if role == "area" else area)

    message = str(raised.value)
    assert f"{role}.geometry" in message and problem in message
    assert "bad-index" in message and "bad-feature" in message


def test_rejects_a_nonpolygon_scope_and_an_empty_scope(golden):
    package, area, _ = golden
    targets = pyogrio.read_dataframe(package, layer="points")
    nonpolygon = gpd.GeoDataFrame(
        {"source_feature_id": ["point-scope"]}, geometry=[Point(500020, 4400020)], crs=CRS,
    )

    with pytest.raises(ValueError, match=r"area\.geometry.*at least one"):
        select_intersecting(targets, area.iloc[:0])
    with pytest.raises(ValueError) as raised:
        select_intersecting(targets, nonpolygon)
    assert "area.geometry" in str(raised.value) and "point-scope" in str(raised.value)


@pytest.mark.parametrize("role", ["targets", "area"])
def test_rejects_a_missing_active_geometry_column(golden, role):
    package, area, _ = golden
    targets = pyogrio.read_dataframe(package, layer="points")
    broken = gpd.GeoDataFrame({"source_feature_id": ["no-geometry"]})

    with pytest.raises(ValueError, match=rf"{role}\.geometry"):
        select_intersecting(broken if role == "targets" else targets, broken if role == "area" else area)


def test_unknown_and_mismatched_crs_are_rejected_without_reprojection(golden):
    package, area, _ = golden
    targets = pyogrio.read_dataframe(package, layer="points")
    placeholder = (
        'LOCAL_CS["Undefined Cartesian SRS",LOCAL_DATUM["unknown",0],UNIT["metre",1],'
        'AXIS["Easting",EAST],AXIS["Northing",NORTH]]'
    )

    with pytest.raises(ValueError, match=r"targets\.crs.*missing"):
        select_intersecting(targets.set_crs(None, allow_override=True), area)
    with pytest.raises(ValueError, match=r"area\.crs.*undefined"):
        select_intersecting(targets, area.set_crs(placeholder, allow_override=True))
    with pytest.raises(ValueError, match=r"area\.crs.*match.*targets\.crs"):
        select_intersecting(targets.to_crs("EPSG:4326"), area)


@pytest.mark.parametrize("empty_input", [True, False], ids=["empty-input", "no-matches"])
def test_empty_selection_preserves_target_schema_and_crs(golden, empty_input):
    package, area, _ = golden
    targets = pyogrio.read_dataframe(package, layer="points").rename_geometry("observed_geometry")
    targets = targets.iloc[:0] if empty_input else targets.loc[targets.source_feature_id.eq("point-outside")]

    selected = select_intersecting(targets, area)

    assert_geodataframe_equal(selected, targets.iloc[:0])
    assert selected.geometry.name == "observed_geometry"
