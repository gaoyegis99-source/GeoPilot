import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
from geopandas.testing import assert_geodataframe_equal
from shapely.geometry import LineString, MultiPolygon, box

from geopilot.gis.vector.spatial_relations import select_intersecting, select_within


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/vector/t13"


@pytest.fixture(scope="module")
def golden():
    expected = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))
    package = FIXTURE / expected["input_file"]
    area = pyogrio.read_dataframe(package, layer=expected["reference"]["layer"])
    return package, area, expected


@pytest.mark.parametrize("layer", ["points", "lines", "polygons"])
def test_within_matches_hand_calculated_ids_and_preserves_the_original_rows(golden, layer):
    package, area, expected = golden
    targets = pyogrio.read_dataframe(package, layer=layer).iloc[::-1].rename_geometry("observed_geometry")
    targets.index = pd.Index([position // 2 for position in range(len(targets))], name="source_index")
    targets["attribute"] = range(len(targets))
    initial_targets, initial_area = targets.copy(), area.copy()
    within_ids = expected["relations"]["within"][layer]
    intersects_ids = expected["relations"]["intersects"][layer]
    retained = targets.iloc[targets.source_feature_id.isin(within_ids).to_numpy()]

    selected = select_within(targets, area)
    intersecting = select_intersecting(targets, area)

    assert sorted(selected.source_feature_id) == within_ids
    assert set(intersecting.source_feature_id) - set(selected.source_feature_id) == set(intersects_ids) - set(within_ids)
    assert set(selected.source_feature_id).isdisjoint(expected["boundary_only_ids"][layer])
    assert_geodataframe_equal(selected, retained)
    assert selected.geometry.name == "observed_geometry"
    assert selected.geometry.to_wkb().tolist() == retained.geometry.to_wkb().tolist()
    assert_geodataframe_equal(targets, initial_targets)
    assert_geodataframe_equal(area, initial_area)


def test_within_uses_the_polygon_union_across_seams_and_preserves_gaps():
    crs = "EPSG:32650"
    area = gpd.GeoDataFrame(
        geometry=[box(0, 0, 4, 4), MultiPolygon([box(4, 0, 8, 4), box(10, 0, 12, 4)])], crs=crs,
    )
    targets = gpd.GeoDataFrame(
        {"source_feature_id": ["spans-shared-edge", "on-shared-edge", "matches-component", "crosses-gap", "outer-edge"]},
        geometry=[
            LineString([(2, 2), (6, 2)]), LineString([(4, 1), (4, 3)]), box(0, 0, 8, 4),
            LineString([(7, 2), (11, 2)]), LineString([(0, 1), (0, 3)]),
        ],
        crs=crs,
    )

    selected = select_within(targets, area)

    assert_geodataframe_equal(selected, targets.iloc[:3])


def test_within_accepts_an_empty_target_layer_and_checks_its_crs(golden):
    package, area, _ = golden
    empty = pyogrio.read_dataframe(package, layer="points").iloc[:0]

    assert_geodataframe_equal(select_within(empty, area), empty)
    with pytest.raises(ValueError, match=r"targets\.crs.*missing"):
        select_within(empty.set_crs(None, allow_override=True), area)
