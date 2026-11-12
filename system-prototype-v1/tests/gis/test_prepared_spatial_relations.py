import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import shapely
from geopandas.testing import assert_geodataframe_equal
from shapely.errors import GEOSException
from shapely.geometry import LineString, Point, Polygon, box

from geopilot.gis.tools import select_spatial_relation as worker
from geopilot.gis.vector import spatial_relations as relations


CRS = "EPSG:32650"


@pytest.fixture
def scope_and_targets():
    shell = box(0, 0, 4, 4)
    hole = box(1, 1, 2, 2)
    area = gpd.GeoDataFrame(
        geometry=[Polygon(shell.exterior.coords, [hole.exterior.coords]), box(4, 0, 8, 4), box(10, 0, 12, 4)],
        crs=CRS,
    )
    cases = [
        ("crosses-seam", LineString([(3, 3), (6, 3)])),
        ("hole-interior", Point(1.5, 1.5)),
        ("seam-point", Point(4, 2)),
        ("crosses-gap", LineString([(7, 2), (11, 2)])),
        ("along-hole-edge", LineString([(1, 1.25), (1, 1.75)])),
        ("crosses-seam", box(3, 2, 5, 3)),
        ("touches-shell-from-outside", box(-1, 2, 0, 3)),
        ("across-hole", box(0.5, 0.5, 2.5, 2.5)),
        ("disjoint", Point(9, 2)),
    ]
    targets = gpd.GeoDataFrame(
        {"source_feature_id": [name for name, _ in cases], "payload": pd.array([7, 6, 5, 4, None, 3, 2, 1, 0],
                                                                                 dtype="Int64")},
        geometry=[shape for _, shape in cases], crs=CRS,
        index=pd.Index([9, 5, 5, 4, 0, 9, 2, 3, 2], name="source_index"),
    ).rename_geometry("original_geometry")
    return area, targets


@pytest.mark.parametrize(("relation", "prepared_name", "positions"), [
    ("intersecting", "intersects", [0, 2, 3, 4, 5, 6, 7]),
    ("within", "contains", [0, 2, 5]),
])
def test_prepares_one_union_and_preserves_de9im_and_original_rows(
    scope_and_targets, monkeypatch, relation, prepared_name, positions,
):
    area, targets = scope_and_targets
    predicate = getattr(shapely, prepared_name)
    calls = []

    def observe(scope, geometry):
        assert shapely.is_prepared(scope)
        assert scope.geom_type == "MultiPolygon" and len(scope.geoms) == 2
        assert isinstance(geometry, np.ndarray)
        calls.append(len(geometry))
        return predicate(scope, geometry)

    expected = targets.iloc[positions]
    monkeypatch.setattr(shapely, prepared_name, observe)
    actual = getattr(relations, f"select_{relation}")(targets, area)

    assert calls == [len(targets)]
    assert_geodataframe_equal(actual, expected)
    assert actual.geometry.to_wkb().tolist() == expected.geometry.to_wkb().tolist()
    assert not shapely.is_prepared(area.geometry.to_numpy()).any()
    assert not shapely.is_prepared(targets.geometry.to_numpy()).any()


@pytest.mark.parametrize("failure_at", ["prepare", "contains"])
def test_geos_failure_retries_original_predicate_in_its_original_order(scope_and_targets, monkeypatch, failure_at):
    area, targets = scope_and_targets
    calls = []
    legacy_within = shapely.within

    def fail(*args):
        calls.append(failure_at)
        raise GEOSException("synthetic prepared-path failure")

    def legacy(geometry, scope):
        assert isinstance(geometry, np.ndarray)
        calls.append("within")
        return legacy_within(geometry, scope)

    monkeypatch.setattr(shapely, failure_at, fail)
    monkeypatch.setattr(shapely, "within", legacy)
    assert_geodataframe_equal(relations.select_within(targets, area), targets.iloc[[0, 2, 5]])
    assert calls == [failure_at, "within"]


def test_empty_rows_and_invalid_area_preserve_validation_before_preparation(scope_and_targets, monkeypatch):
    area, targets = scope_and_targets

    def no_prepare(*args):
        pytest.fail("preparation called before validation or with no target rows")

    monkeypatch.setattr(shapely, "prepare", no_prepare)
    assert_geodataframe_equal(relations.select_within(targets.iloc[:0], area), targets.iloc[:0])
    with pytest.raises(ValueError, match=r"area.geometry requires at least one polygon"):
        relations.select_intersecting(targets, area.iloc[:0])
    with pytest.raises(ValueError, match=r"area.crs EPSG:4326 must match targets.crs EPSG:32650"):
        relations.select_within(targets.iloc[:0], area.set_crs("EPSG:4326", allow_override=True))


def test_union_geos_failure_retains_existing_error(scope_and_targets, monkeypatch):
    area, targets = scope_and_targets

    def fail_union(*args):
        raise GEOSException("synthetic union error")

    monkeypatch.setattr(shapely, "union_all", fail_union)
    with pytest.raises(ValueError, match=r"Could not union area.geometry polygons. synthetic union error"):
        relations.select_intersecting(targets, area)


def test_worker_retains_original_predicate_error_when_both_paths_fail(scope_and_targets, monkeypatch):
    area, targets = scope_and_targets
    calls = []

    def fail_prepared(*args):
        calls.append("contains")
        raise GEOSException("synthetic contains error")

    def fail_legacy(*args):
        calls.append("within")
        raise GEOSException("synthetic within error")

    def read(reference, *, role, **kwargs):
        return (targets if role == "targets" else area), "Unknown"

    monkeypatch.setattr(shapely, "contains", fail_prepared)
    monkeypatch.setattr(shapely, "within", fail_legacy)
    monkeypatch.setattr(worker, "read_vector", read)
    with pytest.raises(ValueError, match=r"Could not evaluate within for targets.geometry. synthetic within error"):
        worker.select_spatial_relation({"parameters": {"spatial_relation": "within"}, "inputs": {"targets": {}, "area": {}}})
    assert calls == ["contains", "within"]
