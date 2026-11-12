"""Solve an undirected shortest route through an already noded metre network."""

import heapq
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from geopandas import GeoDataFrame
from pyproj import CRS
from shapely import STRtree, get_coordinate_dimension, get_parts, reverse
from shapely.geometry import LineString, Point

from geopilot.domain.data_asset import VectorMetadata
from geopilot.gis.measurement.distance import validate_distance
from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.gis.tools.vector_step_files import read_vector, write_vector_outputs
from geopilot.methods.land_use_overlay import _stage_outputs


_ID = "source_feature_id"
_Node = tuple[float, float]


@dataclass(frozen=True, slots=True)
class _Edge:
    identifier: str
    geometry: LineString
    first: _Node
    last: _Node
    metres: float


def _check_geometry(geometry, kind, field):
    if (
        not isinstance(geometry, kind) or geometry.is_empty or get_coordinate_dimension(geometry) != 2
        or not geometry.is_valid or not all(math.isfinite(value) for xy in geometry.coords for value in xy)
    ):
        raise ValueError(f"{field} requires a nonempty, finite, valid 2D {kind.__name__}")


def _edges(frame: GeoDataFrame) -> list[_Edge]:
    identifiers = frame[_ID].tolist() if _ID in frame else []
    if not identifiers or any(type(value) is not str or not value.strip() for value in identifiers):
        raise ValueError(f"network requires nonempty string {_ID} values")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError(f"network requires unique {_ID} values")
    edges = []
    for identifier, geometry in zip(identifiers, frame.geometry, strict=True):
        field = f"network edge {identifier}"
        _check_geometry(geometry, LineString, field)
        first, last = geometry.coords[0], geometry.coords[-1]
        metres = geometry.length
        if first == last or not geometry.is_simple or not math.isfinite(metres) or metres <= 0:
            raise ValueError(f"{field} requires a simple positive-length line with distinct endpoints")
        edges.append(_Edge(identifier, geometry, first, last, metres))
    return sorted(edges, key=lambda edge: edge.identifier)


def _graph(edges):
    tree = STRtree([edge.geometry for edge in edges])
    graph = defaultdict(list)
    for index, edge in enumerate(edges):
        graph[edge.first].append((edge.last, edge))
        graph[edge.last].append((edge.first, edge))
        for other_index in tree.query(edge.geometry, predicate="intersects"):
            if other_index <= index:
                continue
            other = edges[other_index]
            shared = {edge.first, edge.last} & {other.first, other.last}
            intersection = edge.geometry.intersection(other.geometry)
            if any(not isinstance(part, Point) or part.coords[0] not in shared for part in get_parts(intersection)):
                raise ValueError(
                    f"Network edges {edge.identifier} and {other.identifier} intersect away from shared endpoints"
                )
    return graph


def _point(frame, role):
    if len(frame) != 1:
        raise ValueError(f"{role} requires exactly one point")
    geometry = frame.geometry.iloc[0]
    _check_geometry(geometry, Point, role)
    return geometry.coords[0]


def _check_crs(frame, role, target):
    field = f"inputs.{role}.crs"
    if frame.crs is None or not target.equals(frame.crs, ignore_axis_order=True):
        raise ValueError(f"{field} must match the confirmed target_crs")
    metadata = VectorMetadata(
        format="GeoDataFrame", crs=target.to_string(), bounds=tuple(float(value) for value in frame.total_bounds),
        geometry_type="LineString" if role == "network" else "Point", feature_count=len(frame), fields=(),
    )
    validate_distance(metadata, field=field)


def _snap(point, nodes, tolerance, role):
    best, nearest, tied = math.inf, None, False
    for node in nodes:
        distance = math.dist(point, node)
        if distance < best:
            best, nearest, tied = distance, node, False
        elif distance == best:
            tied = True
    if nearest is None or best > tolerance:
        raise ValueError(f"{role} is outside the snap tolerance for network nodes")
    if tied:
        raise ValueError(f"{role} has multiple equally near network nodes")
    return nearest, best


def _shortest_route(graph, start, end):
    if start == end:
        raise ValueError("Start and end snap to the same node")
    queue = [(0.0, start)]
    best = {start: 0.0}
    previous = {}
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != best[node]:
            continue
        if node == end:
            break
        for neighbour, edge in graph[node]:
            candidate = distance + edge.metres
            if candidate < best.get(neighbour, math.inf):
                best[neighbour] = candidate
                previous[neighbour] = node, edge
                heapq.heappush(queue, (candidate, neighbour))
    if end not in previous:
        raise ValueError("The snapped start and end nodes are disconnected")
    route = []
    while end != start:
        node, edge = previous[end]
        route.append((node, edge))
        end = node
    route.reverse()
    return route


def compute_route(network, start, end, *, target_crs, snap_tolerance_m):
    """Return ordered, oriented route edges and a JSON summary of the routing inputs."""
    if not isinstance(target_crs, str) or not target_crs.strip():
        raise ValueError("Specify an explicit target_crs")
    target = CRS.from_user_input(target_crs)
    if type(snap_tolerance_m) not in (int, float) or not math.isfinite(snap_tolerance_m) or snap_tolerance_m < 0:
        raise ValueError("snap_tolerance_m must be finite and nonnegative")
    edges = _edges(network)
    start_point, end_point = _point(start, "start"), _point(end, "end")
    for role, frame in (("network", network), ("start", start), ("end", end)):
        _check_crs(frame, role, target)
    graph = _graph(edges)
    first, start_offset = _snap(start_point, graph, snap_tolerance_m, "start")
    last, end_offset = _snap(end_point, graph, snap_tolerance_m, "end")
    route = _shortest_route(graph, first, last)

    identifiers = [edge.identifier for _, edge in route]
    metres = [edge.metres for _, edge in route]
    route_edges = GeoDataFrame(
        {_ID: identifiers, "sequence": list(range(1, len(route) + 1)), "distance_m": metres},
        geometry=[edge.geometry if node == edge.first else reverse(edge.geometry) for node, edge in route],
        crs=target,
    )
    summary = {
        "schema_version": 1,
        "crs": target.to_string(),
        "distance_unit": "meter",
        "snap_tolerance_m": float(snap_tolerance_m),
        "start": {"node_xy": list(first), "offset_m": start_offset},
        "end": {"node_xy": list(last), "offset_m": end_offset},
        "edge_ids": identifiers,
        "total_distance_m": math.fsum(metres),
    }
    return route_edges, summary


def run_route(request: dict) -> dict:
    inputs = request["inputs"]
    frames = {role: read_vector(inputs[role], role=role, require_source_id=(role == "network"))[0]
              for role in ("network", "start", "end")}
    route, summary = compute_route(**frames, **request["parameters"])
    outputs = request["output_targets"]
    with _stage_outputs(outputs) as temporary:
        reference = temporary["route_summary"]
        target = Path(reference["path"])
        if target.suffix.lower() != ".json" or reference.get("layer") is not None:
            raise ValueError("route_summary requires a .json path without a layer")
        with target.open("x", encoding="utf-8") as stream:
            json.dump(summary, stream, indent=2, allow_nan=False)
            stream.write("\n")
        write_vector_outputs(temporary, {"route_edges": (route, "LineString")})
    return outputs


if __name__ == "__main__":
    raise SystemExit(run_tool_command(run_route, description=__doc__))
