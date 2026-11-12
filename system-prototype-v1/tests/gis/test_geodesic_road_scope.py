"""Verify the geodesic candidate scope and the actual preparation workers."""

import json
import math
from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest
import shapely
import yaml
from pyproj import CRS, Geod, Transformer
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import nearest_points

from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepRequest
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.gis.measurement.geodesic_road_scope import (
    CANDIDATE_PADDING_M,
    ROAD_STATION_SPACING_M,
    expand_wgs84_analysis_extent,
    prepare_wgs84_geometry,
    road_intersects_influence,
)
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
EXPAND = "python-expand-wgs84-analysis-extent"
SELECT = "python-select-wgs84-roads"
PREPARE = "python-prepare-wgs84-measurement-lines"
BUFFER = "python-wgs84-geodesic-buffer"
RADIUS_M = 6_378_137.0
REFERENCE = json.loads((ROOT / "tests/fixtures/geodesic/reference.json").read_text(encoding="utf-8"))
ARC = next(case for case in REFERENCE["cases"] if case["id"] == "high-latitude-arc")


def _degrees_at_equator(metres):
    return math.degrees(metres / RADIUS_M)


def test_equatorial_influence_envelope_covers_distance_boundary_and_preserves_large_hole():
    outside = box(116.97, -0.04, 117.03, 0.04)
    hole = box(116.99, -0.015, 117.01, 0.015)
    study_area = MultiPolygon([Polygon(outside.exterior.coords, [hole.exterior.coords]), box(117.07, -0.01, 117.08, 0.01)])
    assert (CANDIDATE_PADDING_M, ROAD_STATION_SPACING_M) == (2.51, 5.0)

    influence = expand_wgs84_analysis_extent(study_area, 500)

    assert influence.is_valid and influence.covers(study_area)
    assert influence.covers(Point(117.03 + _degrees_at_equator(500), 0))
    assert influence.covers(Point(117.03 + _degrees_at_equator(502.49), 0))
    assert not influence.covers(Point(117.03 + _degrees_at_equator(505), 0))
    near_lon = 117.03 + _degrees_at_equator(500)
    far_lon = 117.03 + _degrees_at_equator(505)
    assert road_intersects_influence(LineString([(near_lon, -0.0005), (near_lon, 0.0005)]), influence)
    assert not road_intersects_influence(LineString([(far_lon, -0.0005), (far_lon, 0.0005)]), influence)
    assert influence.covers(Point(117.01 - _degrees_at_equator(500), 0))
    assert not influence.covers(Point(117, 0))
    assert not influence.covers(Point(117.05, 0))


def test_geographiclib_high_latitude_fixture_needs_the_arc_instead_of_the_coordinate_chord():
    lon, lat = ARC["probes"][0]["point"]
    aoi = box(lon - 0.00002, lat - 0.00002, lon + 0.00002, lat + 0.00002)
    arc = LineString(ARC["line"])
    influence = expand_wgs84_analysis_extent(aoi, ARC["distance_m"])

    assert ARC["probes"][0]["distance_bracket_m"][1] < ARC["distance_m"]
    assert not influence.intersects(arc)
    assert road_intersects_influence(arc, influence)
    assert not road_intersects_influence(LineString([(178, 64), (-178, 64)]), influence)
    assert not road_intersects_influence(LineString([(-160, -40), (-40, -40)]), influence)


def test_projection_samples_source_polygon_edges_before_inverse_conversion():
    source = box(440000, 7200000, 560000, 7210000)
    transformer = Transformer.from_crs(32633, 4326, always_xy=True)
    reference = Point(transformer.transform(500037, 7200000))
    sparse = shapely.transform(source, transformer.transform, interleaved=False)
    converted = prepare_wgs84_geometry(source, CRS.from_epsg(32633))
    geod = Geod(ellps="WGS84")

    def distance_to_edge(geometry):
        nearest, _ = nearest_points(geometry.boundary, reference)
        return geod.inv(nearest.x, nearest.y, reference.x, reference.y)[2]

    assert distance_to_edge(sparse) > 100
    assert distance_to_edge(converted) < 0.001
    assert converted.is_valid and len(converted.exterior.coords) > len(sparse.exterior.coords)
    assert prepare_wgs84_geometry(sparse, "EPSG:4326").equals_exact(sparse, tolerance=0)


@pytest.mark.parametrize(("bounds", "diagnosis"), [
    ((179.990, 0, 179.999, 0.01), "antimeridian"),
    ((10, 74.995, 10.01, 74.999), "latitude"),
    ((10, 0, 15.999, 0.01), "longitude"),
])
def test_unrepresentable_candidate_scope_has_a_clear_domain_error(bounds, diagnosis):
    with pytest.raises(ValueError, match=diagnosis):
        expand_wgs84_analysis_extent(box(*bounds), 500)


def _write_frame(workdir, name, geometries, ids, *, epsg, geometry_type=None):
    destination = workdir / "inputs" / f"{name}.gpkg"
    frame = gpd.GeoDataFrame({"source_feature_id": ids}, geometry=geometries, crs=4326).to_crs(epsg)
    pyogrio.write_dataframe(frame, destination, layer=name, geometry_type=geometry_type)
    return {"path": destination.relative_to(workdir).as_posix(), "layer": name}


def _run(workdir, name, tool, inputs, parameters):
    contract_path = ROOT / "config" / "tools" / f"{tool}.yaml"
    contract = ToolContract.model_validate(yaml.safe_load(contract_path.read_text()))
    registry = ToolRegistry(python_implementations=(contract.implementation,))
    registry.register(contract)
    worker = ROOT / "src/geopilot/gis/tools" / f"{contract.implementation.removeprefix('geopilot.')}.py"
    (workdir / "steps" / name).mkdir(parents=True)
    request = StepRequest(
        run_id="test-geodesic-road", step_key=name, tool=contract.ref, workdir_ref="test-geodesic-road",
        inputs=inputs, parameters=parameters,
        output_targets={role: {"path": f"outputs/{name}-{role}.gpkg", "layer": role} for role in contract.outputs},
        log_path=f"steps/{name}/process.log", cancel_path="cancel.flag",
    )
    return run_step(
        request, registry=registry, workdir=workdir, python_implementations={contract.implementation: worker},
        input_preflight=preflight_step_inputs,
    )


def _frame(workdir, result, role):
    output = result.outputs[role]
    return pyogrio.read_dataframe(workdir / output.path, layer=output.layer)


def _reference(result, role):
    return result.outputs[role].model_dump(include={"path", "layer"})


def _measurement(workdir, result):
    log = (workdir / result.log_path).read_text()
    return next(json.loads(line.removeprefix("geodesic_measurement: ")) for line in log.splitlines()
                if line.startswith("geodesic_measurement: "))


@pytest.mark.parametrize(("near_road", "empty_source"), [
    (False, True), (False, False), (True, False),
], ids=("empty-source", "empty-candidates", "outside-near-road"))
def test_actual_four_children_prepare_mixed_crs_and_create_the_formal_geodesic_buffer(tmp_path, near_road, empty_source):
    for directory in ("inputs", "outputs"):
        (tmp_path / directory).mkdir()
    lon, lat = ARC["probes"][0]["point"]
    extent = box(lon - 0.00002, lat - 0.00002, lon + 0.00002, lat + 0.00002)
    source = _write_frame(tmp_path, "source", [extent], ["area"], epsg=32633)
    targets = _write_frame(tmp_path, "targets", [Point(lon, lat)], ["building"], epsg=4326)
    zones = _write_frame(tmp_path, "zones", [extent.buffer(0.0001)], ["zone"], epsg=3857)
    geometries = [] if empty_source else [LineString([(-160, -40), (-40, -40)])]
    ids = [] if empty_source else ["far"]
    if near_road:
        geometries.append(LineString(ARC["line"]))
        ids.append("near")
    roads = _write_frame(tmp_path, "roads", geometries, ids, epsg=3857, geometry_type="LineString")
    input_paths = list((tmp_path / "inputs").glob("*.gpkg"))
    before = [fingerprint_file(path) for path in input_paths]

    expanded = _run(tmp_path, "expand", EXPAND, {"source": source, "targets": targets, "zones": zones}, {"distance": 1000})
    assert expanded.status == "SUCCEEDED", expanded
    assert all(_frame(tmp_path, expanded, role).crs.to_epsg() == 4326 for role in expanded.outputs)
    assert _measurement(tmp_path, expanded) == {
        "algorithm": "wgs84_ellipsoid_influence_envelope", "ellipsoid": "WGS84", "distance_m": 1000,
        "candidate_padding_m": 2.51, "road_station_spacing_m": 5.0,
    }

    selected = _run(
        tmp_path, "select", SELECT, {"source": roads, "area": _reference(expanded, "expanded_extent")},
        {"predicate": "intersects"},
    )
    assert selected.status == "SUCCEEDED", selected
    selected_frame = _frame(tmp_path, selected, "roads")
    assert selected_frame.crs.to_epsg() == 3857
    assert selected_frame.source_feature_id.tolist() == (["near"] if near_road else [])
    if near_road:
        original = pyogrio.read_dataframe(tmp_path / roads["path"], layer=roads["layer"])
        assert selected_frame.geometry.item().equals_exact(original.geometry.iloc[1], tolerance=0)

    prepared = _run(
        tmp_path, "prepare", PREPARE, {"source": _reference(selected, "roads")},
        {"method": "geodesic", "target_crs": "EPSG:4326"},
    )
    assert prepared.status == "SUCCEEDED", prepared
    assert _frame(tmp_path, prepared, "roads").crs.to_epsg() == 4326

    buffered = _run(tmp_path, "buffer", BUFFER, {"source": _reference(prepared, "roads")}, {"distance": 1000, "dissolve": True})
    assert buffered.status == "SUCCEEDED", buffered
    buffer_frame = _frame(tmp_path, buffered, "buffer")
    assert buffer_frame.crs.to_epsg() == 4326 and len(buffer_frame) == int(near_road)
    if near_road:
        assert buffer_frame.geometry.item().covers(Point(*ARC["probes"][0]["point"]))
        assert not buffer_frame.geometry.item().covers(Point(*ARC["probes"][1]["point"]))
    audit = _measurement(tmp_path, buffered)
    assert audit["algorithm"] == "wgs84_geodesic_line_buffer" and audit["distance_m"] == 1000
    assert audit["road_count"] == int(near_road)
    assert [fingerprint_file(path) for path in input_paths] == before


def test_candidate_beyond_t04_segment_limit_is_reported_during_preparation(tmp_path):
    for directory in ("inputs", "outputs"):
        (tmp_path / directory).mkdir()
    roads = _write_frame(tmp_path, "roads", [LineString([(12, 65), (12, 67)])], ["long"], epsg=4326)
    aoi = box(12.0001, 65.499, 12.001, 65.501)
    assert road_intersects_influence(LineString([(12, 65), (12, 67)]), expand_wgs84_analysis_extent(aoi, 500))

    result = _run(tmp_path, "prepare", PREPARE, {"source": roads}, {"method": "geodesic", "target_crs": "EPSG:4326"})

    assert result.status == "FAILED" and result.exit_code == 1
    log = (tmp_path / result.log_path).read_text()
    assert "segment length" in log and "Road row 1" in log
    assert not list((tmp_path / "outputs").iterdir())
