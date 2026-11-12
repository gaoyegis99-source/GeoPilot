import sqlite3
from contextlib import closing

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
import shapely
from shapely.errors import GEOSException
from shapely.geometry import LineString, MultiLineString, Polygon, box

from geopilot.gis.tools import clip_vector_to_extent as clip


CRS = "EPSG:32650"
INPUTS = {"targets": {}, "area": {}}


def _example_scope():
    shell, hole = box(0, 0, 4, 4), box(1, 1, 2, 2)
    area = [Polygon(shell.exterior.coords, [hole.exterior.coords]), box(4, 0, 8, 4), box(10, 0, 12, 4)]
    return shapely.union_all(area)


@pytest.mark.parametrize(("kind", "shapes", "retained", "types"), [
    ("LineString", [
        LineString([(0.5, 1.5), (3, 1.5)]), LineString([(1.2, 1.5), (1.7, 1.5)]),
        LineString([(3, 3), (6, 3)]), LineString([(7, 2), (11, 2)]),
        LineString([(-1, -1), (0, 0)]), LineString([(20, 0), (21, 1)]),
    ], [True, False, True, True, True, False], ["MultiLineString", "LineString", "MultiLineString", "Point"]),
    ("Polygon", [
        box(0.5, 0.5, 3, 3), box(1.2, 1.2, 1.8, 1.8), box(3, 2, 5, 3), box(7, 1, 11, 3),
        box(-1, 2, 0, 3), box(20, 0, 21, 1),
    ], [True, False, True, True, True, False], ["Polygon", "Polygon", "MultiPolygon", "LineString"]),
])
def test_only_one_bbox_candidate_uses_dissolved_component(
    monkeypatch, kind, shapes, retained, types,
):
    scope = _example_scope()
    geometry = np.asarray(shapes)
    whole = shapely.intersection
    old = whole(geometry, scope)
    observed = []

    def observe(left, right):
        observed.append((left.tolist(), isinstance(right, np.ndarray)))
        return whole(left, right)

    monkeypatch.setattr(clip, "_declared_xy", lambda reference: True)
    monkeypatch.setattr(clip, "_COMPONENT_QUERY_PAIR_BUDGET", 2)  # One target per query.
    monkeypatch.setattr(shapely, "intersection", observe)
    keep, clipped = clip._clip_geometries(geometry, scope, kind, "Polygon", INPUTS)

    assert keep.tolist() == retained
    assert shapely.to_wkb(clipped).tolist() == shapely.to_wkb(old[keep]).tolist()
    assert [shape.geom_type for shape in clipped] == types
    local, fallback = observed
    assert local == ([shapes[index] for index in (0, 1, 2, 4)], True)
    assert fallback == ([shapes[index] for index in (3, 5)], False)


def test_multipart_targets_with_only_one_bbox_candidate_match_whole_scope(monkeypatch):
    scope = _example_scope()
    geometry = np.asarray([MultiLineString([[(2, 2), (4, 2)], [(5, 5), (5, 6)]])])
    expected = shapely.intersection(geometry, scope)
    monkeypatch.setattr(clip, "_declared_xy", lambda reference: True)
    keep, clipped = clip._clip_geometries(geometry, scope, "MultiLineString", "Polygon", INPUTS)
    assert keep.tolist() == [True]
    assert shapely.to_wkb(clipped).tolist() == shapely.to_wkb(expected).tolist()


@pytest.mark.parametrize("wkt", ["MULTIPOLYGON EMPTY", "MULTIPOLYGON (((0 0, 4 0, 4 4, 0 4, 0 0)))"])
def test_scope_with_at_most_one_component_uses_original_overlay(monkeypatch, wkt):
    scope = shapely.from_wkt(wkt)
    geometry = np.asarray([LineString([(-1, 2), (3, 2)])])
    expected = shapely.intersection(geometry, scope)
    monkeypatch.setattr(clip, "_declared_xy", lambda reference: True)
    monkeypatch.setattr(clip, "_component_intersections", lambda *args: pytest.fail("at most one component"))
    keep, clipped = clip._clip_geometries(geometry, scope, "LineString", "MultiPolygon", INPUTS)
    assert keep.tolist() == (~shapely.is_empty(expected)).tolist()
    assert shapely.to_wkb(clipped).tolist() == shapely.to_wkb(expected[keep]).tolist()


def test_measured_scope_can_change_z_of_local_intersection_and_must_fall_back(monkeypatch):
    near = box(0, 0, 4, 4)
    far = shapely.from_wkt("POLYGON M ((20 0 7, 24 0 7, 24 4 7, 20 4 7, 20 0 7))")
    scope = shapely.union_all([near, far])
    geometry = np.asarray([
        shapely.from_wkt("POLYGON ZM ((-1 -1 2 3, 2 -1 3 5, 2 2 7 8, -1 2 9 5, -1 -1 2 3))"),
    ])
    expected = shapely.intersection(geometry, scope)
    unsafe = clip._component_intersections(geometry, scope)
    assert shapely.equals(expected, unsafe).all()
    assert shapely.to_wkb(expected).tolist() != shapely.to_wkb(unsafe).tolist()
    monkeypatch.setattr(clip, "_declared_xy", lambda reference: True)
    monkeypatch.setattr(clip, "_component_intersections", lambda *args: pytest.fail("non-XY used component overlay"))
    keep, clipped = clip._clip_geometries(geometry, scope, "Polygon", "MultiPolygon", INPUTS)
    assert keep.tolist() == [True]
    assert shapely.to_wkb(clipped).tolist() == shapely.to_wkb(expected).tolist()


@pytest.mark.parametrize("case", ["Z", "M", "fixed-precision", "undeclared"])
def test_non_xy_or_explicit_precision_targets_keep_original_overlay(monkeypatch, case):
    scope = _example_scope()
    target = {
        "Z": shapely.from_wkt("LINESTRING Z (-1 2 7, 3 2 8)"),
        "M": shapely.from_wkt("LINESTRING M (-1 2 7, 3 2 8)"),
        "fixed-precision": shapely.set_precision(LineString([(-1, 2), (3, 2)]), 0.25),
        "undeclared": LineString([(-1, 2), (3, 2)]),
    }[case]
    geometry = np.asarray([target])
    expected = shapely.intersection(geometry, scope)
    monkeypatch.setattr(clip, "_declared_xy", lambda reference: case != "undeclared")
    monkeypatch.setattr(clip, "_component_intersections", lambda *args: pytest.fail("ineligible used component overlay"))
    keep, clipped = clip._clip_geometries(geometry, scope, "LineString", "Polygon", INPUTS)
    assert keep.tolist() == [True]
    assert shapely.to_wkb(clipped).tolist() == shapely.to_wkb(expected).tolist()


def test_gdal_can_erase_m_from_line_but_metadata_still_forces_whole_overlay(tmp_path, monkeypatch):
    line = LineString([(-1, 3), (3, 3)])
    source = tmp_path / "lines.gpkg"
    area = tmp_path / "area.gpkg"
    pyogrio.write_dataframe(
        gpd.GeoDataFrame({"source_feature_id": ["line"]}, geometry=[line], crs=CRS),
        source, layer="lines", layer_options={"SPATIAL_INDEX": "NO"},
    )
    pyogrio.write_dataframe(gpd.GeoDataFrame(geometry=[_example_scope()], crs=CRS), area, layer="area")
    with closing(sqlite3.connect(source)) as database, database:
        stored = database.execute("SELECT geom FROM lines").fetchone()[0]
        assert stored[3] & 14 == 2  # XY GeoPackage envelope before WKB.
        measured = shapely.from_wkt("LINESTRING M (-1 3 7, 3 3 8)")
        database.execute("UPDATE lines SET geom = ?", (stored[:40] + measured.wkb,))
        database.execute("UPDATE gpkg_geometry_columns SET m = 1")
    assert not clip._declared_xy({"path": str(source), "layer": "lines"})
    monkeypatch.setattr(clip, "_component_intersections", lambda *args: pytest.fail("measured input used components"))
    request = {
        "inputs": {"targets": {"path": str(source), "layer": "lines"}, "area": {"path": str(area), "layer": "area"}},
        "output_targets": {role: {"path": str(tmp_path / f"{role}.gpkg"), "layer": role}
                           for role in ("targets", "original_targets")},
    }
    with pytest.warns(UserWarning, match="Measured .* geometry types are not supported"):
        clip.clip_vector_to_extent(request)
    assert pyogrio.read_dataframe(tmp_path / "original_targets.gpkg").geometry.iloc[0].wkb == line.wkb
    assert pyogrio.read_dataframe(tmp_path / "targets.gpkg").geometry.iloc[0].wkb == (
        shapely.intersection(line, _example_scope()).wkb
    )


def test_component_geos_error_retries_whole_overlay_and_keeps_its_error(monkeypatch):
    geometry = np.asarray([LineString([(-1, 2), (3, 2)])])
    scope = _example_scope()
    monkeypatch.setattr(clip, "_declared_xy", lambda reference: True)

    def bad_component(*args):
        raise GEOSException("component error")

    monkeypatch.setattr(clip, "_component_intersections", bad_component)
    keep, clipped = clip._clip_geometries(geometry, scope, "LineString", "Polygon", INPUTS)
    assert keep.tolist() == [True]
    assert shapely.to_wkb(clipped).tolist() == shapely.to_wkb(shapely.intersection(geometry, scope)).tolist()

    def bad_whole(*args):
        raise GEOSException("legacy error")

    monkeypatch.setattr(shapely, "intersection", bad_whole)
    with pytest.raises(ValueError, match=r"Could not clip targets.geometry to area.geometry. legacy error"):
        clip._clip_geometries(geometry, scope, "LineString", "Polygon", INPUTS)
