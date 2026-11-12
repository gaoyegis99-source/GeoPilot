import sqlite3
from contextlib import closing

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pytest
import shapely
from shapely.errors import GEOSException
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon, box

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


def test_rectangular_polygon_pairs_keep_exact_geopackage_rows(tmp_path, monkeypatch):
    scope_parts = [box(0, 3, 4, 5), box(30, 0, 34, 5), box(6, 0, 8, 5), box(0, 0, 4, 2)]
    crossing = box(1, 1, 5, 4)
    mixed_contact = box(1, 2, 3, 4)
    hole_shell = box(1, 0, 3, 5)
    holed = Polygon(hole_shell.exterior.coords, [box(2, 1, 2.5, 2).exterior.coords])
    shapes = [crossing, box(4, 0, 5, 1), box(20, 2, 21, 3), crossing,
              box(3, 1, 7, 4), mixed_contact, holed, box(1.25, 1, 3, 4)]
    identifiers = ["crossing", "edge-contact", "zero", "crossing",
                   "three-components", "mixed-contact", "hole", "fractional"]
    targets_path, area_path = tmp_path / "input.gpkg", tmp_path / "extent.gpkg"
    pyogrio.write_dataframe(
        gpd.GeoDataFrame({
            "source_feature_id": identifiers,
            "label": ["甲", "", "unused", "乙", None, "contact", "hole", "fraction"],
            "row_number": list(range(len(shapes))),
        }, geometry=shapes, crs="EPSG:6933"),
        targets_path, layer="input",
    )
    pyogrio.write_dataframe(
        gpd.GeoDataFrame(geometry=scope_parts, crs="EPSG:6933"),
        area_path, layer="extent",
    )
    targets = pyogrio.read_dataframe(targets_path, layer="input")
    area = pyogrio.read_dataframe(area_path, layer="extent")
    scope = shapely.union_all(area.geometry.to_numpy())
    original_overlay = shapely.intersection
    baseline = original_overlay(targets.geometry.to_numpy(), scope)
    retained = ~shapely.is_empty(baseline)
    assert retained.tolist() == [True, True, False, True, True, True, True, True]
    assert shapely.get_type_id(baseline[0]) == shapely.GeometryType.MULTIPOLYGON
    assert shapely.get_type_id(baseline[1]) == shapely.GeometryType.LINESTRING
    assert shapely.get_type_id(baseline[5]) == shapely.GeometryType.GEOMETRYCOLLECTION

    observed = {"pair": [], "single": [], "whole": []}

    def record(left, right):
        if len(left):
            if isinstance(right, np.ndarray):
                sizes = shapely.get_num_geometries(right).tolist()
                assert len(set(sizes)) == 1
                kind = "pair" if sizes[0] == 2 else "single"
                if kind == "pair":
                    assert (shapely.get_type_id(right) == shapely.GeometryType.MULTIPOLYGON).all()
                else:
                    assert sizes[0] == 1
            else:
                assert shapely.to_wkb(right) == shapely.to_wkb(scope)
                kind = "whole"
            observed[kind].extend(shapely.to_wkb(left))
        return original_overlay(left, right)

    monkeypatch.setattr(shapely, "intersection", record)
    monkeypatch.setattr(clip, "_COMPONENT_QUERY_PAIR_BUDGET", 3)  # One target per tree query.
    outputs = {role: {"path": str(tmp_path / f"{role}.gpkg"), "layer": role}
               for role in ("targets", "original_targets")}
    request = {
        "inputs": {"targets": {"path": str(targets_path), "layer": "input"},
                   "area": {"path": str(area_path), "layer": "extent"}},
        "output_targets": outputs,
    }
    assert clip.clip_vector_to_extent(request) == outputs
    assert observed == {
        "pair": targets.geometry.iloc[[0, 3]].to_wkb().tolist(),
        "single": targets.geometry.iloc[[1]].to_wkb().tolist(),
        "whole": targets.geometry.iloc[[4, 5, 6, 7]].to_wkb().tolist(),
    }
    originals = targets.iloc[retained].reset_index(drop=True)
    for role, wkb, layer_type in (
        ("targets", shapely.to_wkb(baseline[retained]).tolist(), "Unknown"),
        ("original_targets", originals.geometry.to_wkb().tolist(), "Polygon"),
    ):
        actual = pyogrio.read_dataframe(outputs[role]["path"], layer=role)
        info = pyogrio.read_info(outputs[role]["path"], layer=role)
        assert actual.crs == originals.crs
        assert actual.geometry.to_wkb().tolist() == wkb
        assert info["geometry_type"] == layer_type
        pd.testing.assert_frame_equal(
            actual.drop(columns=actual.geometry.name), originals.drop(columns=originals.geometry.name),
        )


@pytest.mark.parametrize("reverse", [False, True])
def test_rectangular_pairs_keep_scope_order_in_exact_wkb(monkeypatch, reverse):
    parts = [box(0, 3, 4, 5), box(30, 0, 34, 5), box(6, 0, 8, 5), box(0, 0, 4, 2)]
    scope = MultiPolygon(parts[::-1] if reverse else parts)
    targets = np.asarray([box(1, 1, 5, 4), box(0, 0, 4, 5), box(20, 2, 21, 3)])
    expected = shapely.intersection(targets, scope)
    calls = []
    pair_overlay = clip._rectangular_pair_clips

    def observed(*args):
        result = pair_overlay(*args)
        calls.extend(result[0])
        return result

    monkeypatch.setattr(clip, "_declared_xy", lambda reference: True)
    monkeypatch.setattr(clip, "_rectangular_pair_clips", observed)
    keep, clipped = clip._clip_geometries(targets, scope, "Polygon", "MultiPolygon", INPUTS)
    assert calls == [0, 1]
    assert keep.tolist() == [True, True, False]
    assert shapely.to_wkb(clipped).tolist() == shapely.to_wkb(expected[keep]).tolist()


@pytest.mark.parametrize("case", ["Z-target", "M-target", "ZM-target", "M-scope", "fixed-target", "fixed-scope",
                                  "undeclared-target", "undeclared-area"])
def test_rectangular_polygon_pairs_keep_non_xy_precision_and_declaration_fallback(monkeypatch, case):
    target = box(1, 1, 5, 4)
    distant = box(30, 0, 34, 5)
    if case in {"Z-target", "M-target"}:
        dimension = case.split("-")[0]
        target = shapely.from_wkt(f"POLYGON {dimension} ((1 1 8, 5 1 9, 5 4 10, 1 4 11, 1 1 8))")
    elif case == "ZM-target":
        target = shapely.from_wkt("POLYGON ZM ((1 1 8 3, 5 1 9 4, 5 4 10 5, 1 4 11 6, 1 1 8 3))")
    elif case == "M-scope":
        distant = shapely.from_wkt("POLYGON M ((30 0 8, 34 0 8, 34 5 8, 30 5 8, 30 0 8))")
    elif case == "fixed-target":
        target = shapely.set_precision(target, 0.25)
    scope = MultiPolygon([box(0, 0, 4, 2), box(0, 3, 4, 5), distant])
    if case == "fixed-scope":
        scope = shapely.set_precision(scope, 0.25)
    targets = np.asarray([target])
    expected = shapely.intersection(targets, scope)
    inputs = {"targets": {"path": "targets"}, "area": {"path": "area"}}
    undeclared = {"undeclared-target": "targets", "undeclared-area": "area"}.get(case)
    monkeypatch.setattr(clip, "_declared_xy", lambda reference: reference["path"] != undeclared)
    monkeypatch.setattr(clip, "_component_intersections", lambda *args: pytest.fail("ineligible used components"))
    keep, clipped = clip._clip_geometries(targets, scope, "Polygon", "MultiPolygon", inputs)
    assert keep.tolist() == [True]
    assert shapely.to_wkb(clipped).tolist() == shapely.to_wkb(expected).tolist()


def test_rectangular_pair_touching_at_corner_uses_whole_scope(monkeypatch):
    scope = MultiPolygon([box(0, 0, 2, 2), box(2, 2, 4, 4), box(10, 10, 12, 12)])
    targets = np.asarray([box(1, 1, 3, 3)])
    original_overlay = shapely.intersection
    expected = original_overlay(targets, scope)
    observed = []

    def record(left, right):
        if len(left):
            observed.append(right)
        return original_overlay(left, right)

    monkeypatch.setattr(clip, "_declared_xy", lambda reference: True)
    monkeypatch.setattr(shapely, "intersection", record)
    keep, clipped = clip._clip_geometries(targets, scope, "Polygon", "MultiPolygon", INPUTS)
    assert observed == [scope]
    assert keep.tolist() == [True]
    assert shapely.to_wkb(clipped).tolist() == shapely.to_wkb(expected).tolist()
