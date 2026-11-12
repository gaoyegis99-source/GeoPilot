import sqlite3
from contextlib import closing

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pytest
import shapely
from shapely.errors import GEOSException
from shapely.geometry import MultiPoint, Point, Polygon, box

from geopilot.gis.tools import clip_vector_to_extent as clip

CRS = "EPSG:32650"


def _request(tmp_path, points, scope, *, target_kind=None, area_crs=CRS, target_format="gpkg"):
    targets = tmp_path / f"input.{target_format}"
    area = tmp_path / "area.gpkg"
    ids = ["repeated-id" if index < 2 else f"id-{index}" for index in range(len(points))]
    frame = gpd.GeoDataFrame(
        {"source_feature_id": ids, "rank": list(range(len(points))), "geopilot_fid": ["field"] * len(points)},
        geometry=points, crs=CRS,
    )
    target_options = {}
    if target_format == "gpkg":
        target_options = {"layer": "points 'selected'", "layer_options": {"SPATIAL_INDEX": "NO"}}
    pyogrio.write_dataframe(frame, targets, geometry_type=target_kind, **target_options)
    pyogrio.write_dataframe(
        gpd.GeoDataFrame(geometry=[scope], crs=area_crs), area,
        layer="scope", layer_options={"SPATIAL_INDEX": "NO"},
    )
    return {
        "inputs": {
            "targets": {"path": str(targets), **({"layer": target_options["layer"]} if target_options else {})},
            "area": {"path": str(area)},  # Implicit layer with one declared geometry.
        },
        "output_targets": {
            role: {"path": str(tmp_path / f"{role}.gpkg"), "layer": role}
            for role in ("targets", "original_targets")
        },
    }


def _layer(request, role):
    output = request["output_targets"][role]
    path = output["path"]
    return pyogrio.list_layers(path).tolist(), pyogrio.read_dataframe(path, layer=output["layer"])


def _must_use_overlay(*args):
    pytest.fail("prepared covers called for an input requiring overlay")


def test_xy_points_preserve_order_fields_coordinates_and_both_layers(tmp_path, monkeypatch):
    shell = box(0, 0, 10, 10)
    hole = box(4, 4, 6, 6)
    scope = Polygon(shell.exterior.coords, [hole.exterior.coords])
    points = [Point(5, 5), Point(0, 0), Point(10, 7), Point(11, 1), Point(4, 5), Point(1, 1)]
    request = _request(tmp_path, points, scope, target_kind="Point")
    selected = [1, 2, 4, 5]
    covers = clip.points_covered_by_scope
    calls = []

    def record_covers(geometry, area):
        calls.append(len(geometry))
        return covers(geometry, area)

    with monkeypatch.context() as patch:
        patch.setattr(clip, "points_covered_by_scope", record_covers)
        patch.setattr(shapely, "intersection", lambda *args: pytest.fail("unexpected overlay"))
        assert clip.clip_vector_to_extent(request) == request["output_targets"]
    assert calls == [6]

    baseline = {**request, "output_targets": {
        role: {**ref, "path": str(tmp_path / f"overlay-{role}.gpkg")}
        for role, ref in request["output_targets"].items()
    }}
    monkeypatch.setattr(clip, "_declared_xy", lambda reference: False)
    monkeypatch.setattr(clip, "points_covered_by_scope", _must_use_overlay)
    clip.clip_vector_to_extent(baseline)
    for role, kind in (("targets", "Unknown"), ("original_targets", "Point")):
        layers, frame = _layer(request, role)
        baseline_layers, baseline_frame = _layer(baseline, role)
        assert layers == baseline_layers == [[role, kind]]
        assert frame.crs == baseline_frame.crs and frame.crs.to_string() == CRS
        pd.testing.assert_frame_equal(frame.drop(columns="geometry"), baseline_frame.drop(columns="geometry"))
        assert frame["rank"].tolist() == selected
        assert frame["geopilot_fid"].tolist() == ["field"] * len(selected)
        assert frame["source_feature_id"].tolist() == ["repeated-id", "id-2", "id-4", "id-5"]
        assert frame.geometry.to_wkb().tolist() == baseline_frame.geometry.to_wkb().tolist()
        assert frame.geometry.to_wkb().tolist() == [points[index].wkb for index in selected]


@pytest.mark.parametrize(("point_wkt", "scope_wkt", "target_format", "clipped_wkt"), [
    ("POINT Z (0 0 17)", "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))", "gpkg", "POINT Z (0 0 17)"),
    ("POINT (0 0)", "POLYGON Z ((0 0 8, 10 0 8, 10 10 8, 0 10 8, 0 0 8))", "gpkg", "POINT Z (0 0 8)"),
    ("MULTIPOINT ((0 0), (20 20))", "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))", "gpkg", "POINT (0 0)"),
    ("POINT (0 0)", "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))", "geojson", "POINT (0 0)"),
])
def test_geometry_outside_xy_point_geopackage_gate_uses_overlay(
    tmp_path, monkeypatch, point_wkt, scope_wkt, target_format, clipped_wkt,
):
    point = shapely.from_wkt(point_wkt)
    request = _request(tmp_path, [point], shapely.from_wkt(scope_wkt), target_format=target_format)
    monkeypatch.setattr(clip, "points_covered_by_scope", _must_use_overlay)
    clip.clip_vector_to_extent(request)
    assert _layer(request, "targets")[1].geometry.iloc[0].wkb == shapely.from_wkt(clipped_wkt).wkb
    assert _layer(request, "original_targets")[1].geometry.iloc[0].wkb == point.wkb


@pytest.mark.parametrize(("point_wkt", "z", "loaded"), [
    ("POINT M (0 0 13)", 0, Point(0, 0)),
    ("POINT ZM (0 0 7 13)", 1, Point(0, 0, 7)),
])
def test_measured_geopackage_is_excluded_before_gdal_can_hide_m(tmp_path, monkeypatch, point_wkt, z, loaded):
    request = _request(tmp_path, [loaded], box(0, 0, 10, 10))
    reference = request["inputs"]["targets"]
    # Preserve the GeoPackage binary header while replacing the geometry with measured WKB.
    with closing(sqlite3.connect(reference["path"])) as db, db:
        stored = db.execute('SELECT geom FROM "points \'selected\'"').fetchone()[0]
        assert (stored[3] & 14) == 0  # No envelope in a GPKG Point geometry.
        db.execute('UPDATE "points \'selected\'" SET geom = ?', (stored[:8] + shapely.from_wkt(point_wkt).wkb,))
        db.execute("UPDATE gpkg_geometry_columns SET z = ?, m = 1", (z,))
    assert not clip._declared_xy(reference)
    monkeypatch.setattr(clip, "points_covered_by_scope", _must_use_overlay)
    with pytest.warns(UserWarning, match="Measured .* geometry types are not supported"):
        clip.clip_vector_to_extent(request)
    assert _layer(request, "original_targets")[1].geometry.iloc[0].wkb == loaded.wkb
    assert _layer(request, "targets")[1].geometry.iloc[0].wkb == loaded.wkb


def test_measured_scope_is_excluded_when_gdal_reports_an_xy_polygon(tmp_path, monkeypatch):
    point = Point(0, 0)
    scope = box(0, 0, 10, 10)
    request = _request(tmp_path, [point], scope)
    measured = shapely.from_wkt("POLYGON M ((0 0 13, 10 0 13, 10 10 13, 0 10 13, 0 0 13))")
    reference = request["inputs"]["area"]
    with closing(sqlite3.connect(reference["path"])) as db, db:
        stored = db.execute("SELECT geom FROM scope").fetchone()[0]
        assert (stored[3] & 14) == 2  # A 32-byte XY envelope follows the 8-byte header.
        db.execute("UPDATE scope SET geom = ?", (stored[:40] + measured.wkb,))
        db.execute("UPDATE gpkg_geometry_columns SET m = 1")
    assert clip._declared_xy(request["inputs"]["targets"]) and not clip._declared_xy(reference)
    monkeypatch.setattr(clip, "points_covered_by_scope", _must_use_overlay)
    with pytest.warns(UserWarning, match="Measured .* geometry types are not supported"):
        clip.clip_vector_to_extent(request)
    for role in ("targets", "original_targets"):
        assert _layer(request, role)[1].geometry.iloc[0].wkb == point.wkb


@pytest.mark.parametrize("wkt", ["POINT M (0 0 7)", "POINT ZM (0 0 8 7)"])
def test_in_memory_non_xy_point_remains_overlay_even_with_xy_metadata(monkeypatch, wkt):
    monkeypatch.setattr(clip, "_declared_xy", lambda reference: True)
    monkeypatch.setattr(clip, "points_covered_by_scope", _must_use_overlay)
    points = np.asarray([shapely.from_wkt(wkt)])
    scope = box(0, 0, 10, 10)
    keep, actual = clip._clip_geometries(points, scope, "Point", "Polygon", {"targets": {}, "area": {}})
    assert keep.tolist() == [True]
    assert actual[0].wkb == shapely.intersection(points[0], scope).wkb


def test_empty_rows_keep_original_error_before_writing(tmp_path, monkeypatch):
    request = _request(tmp_path, [Point()], box(0, 0, 10, 10), target_kind="Point")
    monkeypatch.setattr(clip, "points_covered_by_scope", _must_use_overlay)
    with pytest.raises(ValueError, match=r"targets.geometry has empty geometry at row 0.*repeated-id"):
        clip.clip_vector_to_extent(request)
    assert not list(tmp_path.glob("*targets.gpkg"))


def test_empty_layer_keeps_two_empty_outputs(tmp_path, monkeypatch):
    request = _request(tmp_path, [], box(0, 0, 10, 10), target_kind="Point")
    monkeypatch.setattr(clip, "points_covered_by_scope", _must_use_overlay)
    clip.clip_vector_to_extent(request)
    for role in ("targets", "original_targets"):
        assert _layer(request, role)[1].empty


def test_crs_error_precedes_prepared_predicate(tmp_path, monkeypatch):
    request = _request(tmp_path, [Point(0, 0)], box(0, 0, 10, 10), area_crs="EPSG:4326")
    monkeypatch.setattr(clip, "points_covered_by_scope", _must_use_overlay)
    with pytest.raises(ValueError, match=r"area.crs EPSG:4326 must match targets.crs EPSG:32650"):
        clip.clip_vector_to_extent(request)
    assert not list(tmp_path.glob("*targets.gpkg"))


def test_predicate_geos_failure_uses_established_overlay_error(tmp_path, monkeypatch):
    request = _request(tmp_path, [Point(0, 0)], box(0, 0, 10, 10))
    calls = []

    def fail_predicate(*args):
        calls.append("covers")
        raise GEOSException("synthetic predicate error")

    def fail_overlay(*args):
        calls.append("intersection")
        raise GEOSException("synthetic overlay error")

    monkeypatch.setattr(clip, "points_covered_by_scope", fail_predicate)
    monkeypatch.setattr(shapely, "intersection", fail_overlay)
    with pytest.raises(ValueError, match=r"Could not clip targets.geometry to area.geometry. synthetic overlay error"):
        clip.clip_vector_to_extent(request)
    assert calls == ["covers", "intersection"]
    assert not list(tmp_path.glob("*targets.gpkg"))
