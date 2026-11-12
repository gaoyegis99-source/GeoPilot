import json
import math
import subprocess
import sys
from pathlib import Path

import pyogrio
import pytest
from shapely import from_wkt
from shapely.geometry import box


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "vector"
PACKAGE_NAME = "road-buffer.gpkg"
LAYER_TYPES = {
    "analysis_extent": "Polygon",
    "roads": "LineString",
    "targets": "Point",
    "zones": "Polygon",
}


def _read_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _build_package(output):
    return subprocess.run(
        [sys.executable, str(FIXTURES / "build_fixtures.py"), str(output)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _read_layer(package, layer):
    return pyogrio.read_dataframe(package, layer=layer).set_index("source_feature_id")


@pytest.mark.parametrize("rebuild", [False, True], ids=["shipped", "rebuilt"])
def test_four_layers_reopen_with_exact_features(tmp_path, rebuild):
    package = FIXTURES / PACKAGE_NAME
    if rebuild:
        package = tmp_path / PACKAGE_NAME
        result = _build_package(package)
        assert result.returncode == 0, result.stderr

    source = _read_json("source.json")
    expected = _read_json("expected.json")
    assert dict(pyogrio.list_layers(package)) == LAYER_TYPES
    for layer, records in source["layers"].items():
        features = _read_layer(package, layer)
        assert features.crs.to_authority() == ("EPSG", "32650")
        assert features.crs.axis_info[0].unit_name == "metre"
        assert features.index.is_unique
        assert set(features.index) == set(expected["input_feature_ids"][layer])
        assert features.geometry.is_valid.all()
        for record in records:
            actual = features.geometry.loc[record["source_feature_id"]]
            assert actual.equals_exact(from_wkt(record["wkt"]), tolerance=0)


def test_builder_does_not_overwrite_existing_output(tmp_path):
    output = tmp_path / PACKAGE_NAME
    output.write_bytes(b"existing file")
    result = _build_package(output)
    assert result.returncode == 2
    assert "already exists" in result.stderr
    assert output.read_bytes() == b"existing file"


def test_reference_capsule_uses_straight_road_and_locked_discretization():
    expected = _read_json("expected.json")
    roads = _read_layer(FIXTURES / PACKAGE_NAME, "roads")
    reference = expected["main_road_buffer"]
    line = roads.geometry.loc[reference["road_id"]]
    endpoints = tuple(map(tuple, reference["endpoints_xy_m"]))
    assert tuple(line.coords) == endpoints
    (x1, y), (x2, y2) = endpoints
    assert y == y2
    length = x2 - x1
    assert length == reference["length_m"] == 1_000

    radius = expected["distance_m"]
    assert radius == 500
    assert expected["buffer_options"] == {
        "segments_per_quarter": 16,
        "end_cap_style": "round",
        "join_style": "round",
        "dissolve": True,
    }
    chord_count = 4 * expected["buffer_options"]["segments_per_quarter"]
    discrete_area = 2 * radius * length + chord_count * radius**2 * math.sin(math.tau / chord_count) / 2
    max_sagitta = radius * (1 - math.cos(math.pi / chord_count))
    ideal_area = 2 * radius * length + math.pi * radius**2
    assert ideal_area == pytest.approx(reference["ideal_area_m2"], rel=0, abs=1e-8)
    assert discrete_area == pytest.approx(reference["discrete_area_m2"], rel=0, abs=1e-8)
    assert max_sagitta == pytest.approx(reference["max_sagitta_m"], rel=0, abs=1e-10)
    assert [x1 - radius, y - radius, x2 + radius, y + radius] == reference["bounds_xy_m"]


def _distance_to_horizontal_segment(point, line):
    (left, y), (right, end_y) = line.coords
    assert left < right and y == end_y
    return math.hypot(max(left - point.x, 0, point.x - right), point.y - y)


def test_reference_ids_and_counts_agree_with_analytic_distances():
    expected = _read_json("expected.json")
    package = FIXTURES / PACKAGE_NAME
    aoi = _read_layer(package, "analysis_extent").geometry.loc["analysis-extent"]
    roads = _read_layer(package, "roads").geometry
    targets = _read_layer(package, "targets").geometry
    zones = _read_layer(package, "zones").geometry
    west, south, east, north = aoi.bounds
    divide = (west + east) / 2
    assert aoi.equals(box(west, south, east, north))
    assert zones.loc["zone-west"].equals(box(west, south, divide, north))
    assert zones.loc["zone-east"].equals(box(divide, south, east, north))

    road_distances = {}
    for road_id, line in roads.items():
        left, y, right, end_y = line.bounds
        assert west < left < right < east and y == end_y
        road_distances[road_id] = max(south - y, 0, y - north)
    radius = expected["distance_m"]
    candidate_ids = sorted(road_id for road_id, distance in road_distances.items() if distance <= radius)
    assert road_distances == expected["road_distance_to_extent_m"]
    assert candidate_ids == expected["candidate_road_ids"]

    distances = {
        target_id: {road_id: _distance_to_horizontal_segment(point, line) for road_id, line in roads.items()}
        for target_id, point in targets.items()
    }
    assert distances["building-edge-road"]["road-edge"] < radius < distances["building-edge-road"]["road-main"]
    assert all(distances["building-overlap"][road_id] < radius for road_id in candidate_ids)
    assert distances["building-far-road"]["road-far"] < radius
    assert all(distances["building-far-road"][road_id] > radius for road_id in candidate_ids)

    nearest = {
        target_id: min(values[road_id] for road_id in candidate_ids)
        for target_id, values in distances.items()
    }
    assert {target_id for target_id, distance in nearest.items() if distance == radius} == {"building-buffer-boundary"}
    buffer_edge = targets.loc["building-buffer-boundary"]
    main = roads.loc["road-main"]
    assert main.bounds[0] < buffer_edge.x < main.bounds[2]
    assert buffer_edge.y == main.bounds[1] - radius
    max_sagitta = expected["main_road_buffer"]["max_sagitta_m"]
    assert all(abs(distance - radius) > max_sagitta for distance in nearest.values() if distance != radius)

    assert set(expected["relations"]) == {"intersects", "within"}
    for relation, reference in expected["relations"].items():
        before_clip = sorted(
            target_id for target_id, distance in nearest.items()
            if distance < radius or (relation == "intersects" and distance == radius)
        )
        clipped = [
            target_id for target_id in before_clip
            if west <= targets.loc[target_id].x <= east and south <= targets.loc[target_id].y <= north
        ]
        assert before_clip == reference["before_clip_target_ids"]
        assert clipped == reference["clipped_target_ids"]

        zone_targets = {"zone-west": [], "zone-east": []}
        unassigned = []
        for target_id in clipped:
            point = targets.loc[target_id]
            if west < point.x < divide and south < point.y < north:
                zone_targets["zone-west"].append(target_id)
            elif divide < point.x < east and south < point.y < north:
                zone_targets["zone-east"].append(target_id)
            else:
                unassigned.append(target_id)
        counts = {zone: len(ids) for zone, ids in zone_targets.items()}
        counts.update(unassigned=len(unassigned), total=len(clipped))
        assert zone_targets == reference["zone_target_ids"]
        assert unassigned == reference["unassigned_target_ids"]
        assert counts == reference["counts"]
