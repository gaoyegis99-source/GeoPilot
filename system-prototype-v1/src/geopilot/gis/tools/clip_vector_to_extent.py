"""Clip already selected targets and keep originals for nonempty intersections."""

import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
import shapely
from geopandas import GeoSeries
from shapely.errors import GEOSException

from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.gis.tools.vector_step_files import read_vector, write_vector_outputs
from geopilot.gis.vector.spatial_relations import _prepare_selection, points_covered_by_scope


_COMPONENT_CLIP_TYPES = {"LineString", "MultiLineString", "Polygon", "MultiPolygon"}
_COMPONENT_CLIP_IDS = (
    shapely.GeometryType.LINESTRING, shapely.GeometryType.MULTILINESTRING,
    shapely.GeometryType.POLYGON, shapely.GeometryType.MULTIPOLYGON,
)
_COMPONENT_QUERY_PAIR_BUDGET = 1 << 20


def _declared_xy(reference: dict) -> bool:
    # Pyogrio can expose a measured layer as an XY layer after removing M.
    path = Path(reference["path"])
    if path.suffix.lower() != ".gpkg":
        return False
    layer = reference.get("layer")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0)) as database:
            rows = database.execute(
                "SELECT z, m FROM gpkg_geometry_columns WHERE ? IS NULL OR table_name = ?", (layer, layer),
            ).fetchmany(2)
    except (OSError, sqlite3.Error, ValueError):
        return False
    return rows == [(0, 0)]


def _integer_rectangles(geometry):
    simple = shapely.get_num_coordinates(geometry) == 5
    coordinates = shapely.get_coordinates(geometry[simple]).reshape(-1, 5, 2)
    edges = np.diff(coordinates, axis=1)
    rectangles = np.zeros(geometry.size, dtype=bool)
    rectangles[simple] = (
        (np.abs(coordinates) <= 2**52).all(axis=(1, 2))
        & (coordinates == np.trunc(coordinates)).all(axis=(1, 2))
        & ((edges == 0).sum(axis=2) == 1).all(axis=1)
    )
    return rectangles


def _rectangular_pair_clips(batch, parts, bounds, positions, components, counts):
    paired = counts[positions] == 2
    positions, components = positions[paired], components[paired]
    order = np.lexsort((components, positions))
    rows = positions[order][::2]
    pairs = components[order].reshape(-1, 2)
    targets = batch[rows]
    target_bounds = shapely.bounds(targets)[:, None, :]
    part_bounds = bounds[pairs]
    # Require two separated area intersections. Contact and mixed-dimension
    # results keep the whole scope's GEOS representation.
    lower = np.maximum(target_bounds[:, :, :2], part_bounds[:, :, :2])
    upper = np.minimum(target_bounds[:, :, 2:], part_bounds[:, :, 2:])
    separated = (part_bounds[:, 0, 2:] < part_bounds[:, 1, :2]) | (part_bounds[:, 1, 2:] < part_bounds[:, 0, :2])
    eligible = _integer_rectangles(targets) & (lower < upper).all(axis=(1, 2)) & separated.any(axis=1)
    # Use dissolved components in their original order, retaining the right
    # operand's MultiPolygon type and the target's original ring order.
    local_scope = shapely.multipolygons(parts[pairs[eligible]])
    return rows[eligible], shapely.intersection(targets[eligible], local_scope)


def _component_intersections(geometry, scope, polygon_targets=False):
    parts = shapely.get_parts(scope)
    tree = shapely.STRtree(parts)
    candidate = np.full(geometry.size, -1, dtype=np.intp)
    intersections = np.empty_like(geometry)
    fallback = np.ones(geometry.size, dtype=bool)
    rectangular_scope = polygon_targets and _integer_rectangles(parts).all()
    bounds = shapely.bounds(parts) if rectangular_scope else None
    empty = shapely.Polygon() if rectangular_scope else None
    # A broad envelope can match every component. Bound the number of such
    # rows per query while allowing a single row to match the whole scope.
    batch_size = max(1, min(1024, _COMPONENT_QUERY_PAIR_BUDGET // len(parts)))
    for start in range(0, geometry.size, batch_size):
        batch = geometry[start:start + batch_size]
        positions, components = tree.query(batch)
        counts = np.bincount(positions, minlength=batch.size)
        unique = counts[positions] == 1
        candidate[start + positions[unique]] = components[unique]
        if rectangular_scope:
            disjoint = start + np.flatnonzero(counts == 0)
            intersections[disjoint] = empty
            fallback[disjoint] = False
            if (counts == 2).any():
                rows, clipped = _rectangular_pair_clips(batch, parts, bounds, positions, components, counts)
                intersections[start + rows] = clipped
                fallback[start + rows] = False

    local = candidate >= 0
    intersections[local] = shapely.intersection(geometry[local], parts[candidate[local]])
    fallback &= ~local
    intersections[fallback] = shapely.intersection(geometry[fallback], scope)
    return intersections


def _clip_geometries(geometry, scope, geometry_type, area_type, inputs):
    if (
        geometry.size and geometry_type == "Point" and area_type in {"Polygon", "MultiPolygon"}
        and (shapely.get_type_id(geometry) == shapely.GeometryType.POINT).all()
        and (shapely.get_coordinate_dimension(geometry) == 2).all()
        and shapely.get_coordinate_dimension(scope) == 2
        and _declared_xy(inputs["targets"]) and _declared_xy(inputs["area"])
    ):
        try:
            retained = points_covered_by_scope(geometry, scope)
        except GEOSException:
            # Let the established overlay determine the result or error.
            pass
        else:
            return retained, geometry[retained]
    if (
        geometry.size and geometry_type in _COMPONENT_CLIP_TYPES and area_type in {"Polygon", "MultiPolygon"}
        and shapely.get_type_id(scope) == shapely.GeometryType.MULTIPOLYGON
        and shapely.get_num_geometries(scope) > 1
        and shapely.get_coordinate_dimension(scope) == 2 and shapely.get_precision(scope) == 0
        and np.isin(shapely.get_type_id(geometry), _COMPONENT_CLIP_IDS).all()
        and (shapely.get_coordinate_dimension(geometry) == 2).all()
        and (shapely.get_precision(geometry) == 0).all()
        and _declared_xy(inputs["targets"]) and _declared_xy(inputs["area"])
    ):
        try:
            polygon_targets = (
                geometry_type == "Polygon"
                and (shapely.get_type_id(geometry) == shapely.GeometryType.POLYGON).all()
            )
            intersections = _component_intersections(geometry, scope, polygon_targets)
        except GEOSException:
            # Repeat the established whole-scope call to preserve its result or error.
            pass
        else:
            retained = ~shapely.is_empty(intersections)
            return retained, intersections[retained]
    try:
        intersections = shapely.intersection(geometry, scope)
    except GEOSException as error:
        raise ValueError(f"Could not clip targets.geometry to area.geometry. {error}") from error
    retained = ~shapely.is_empty(intersections)
    return retained, intersections[retained]


def clip_vector_to_extent(request: dict) -> dict:
    inputs = request["inputs"]
    targets, geometry_type = read_vector(inputs["targets"], role="targets", require_source_id=True)
    area, area_type = read_vector(inputs["area"], role="area")
    geometry, scope = _prepare_selection(targets, area)
    retained, clipped_geometry = _clip_geometries(geometry, scope, geometry_type, area_type, inputs)
    originals = targets.iloc[retained].copy()
    clipped = originals.set_geometry(GeoSeries(
        clipped_geometry, index=originals.index, name=originals.geometry.name, crs=originals.crs,
    ))

    outputs = request["output_targets"]
    # A nonempty contact can have lower dimension than its source feature.
    write_vector_outputs(outputs, {"targets": (clipped, "Unknown"), "original_targets": (originals, geometry_type)})
    print(f"Clipped {len(originals)} of {len(targets)} selected target(s) with nonempty intersections")
    return outputs


if __name__ == "__main__":
    raise SystemExit(run_tool_command(clip_vector_to_extent, description=__doc__))
