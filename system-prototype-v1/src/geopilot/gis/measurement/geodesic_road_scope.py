"""Prepare WGS84 inputs and retain roads that can influence a local analysis area."""

import math
from collections.abc import Sequence

import numpy as np
import shapely
from pyproj import CRS, Geod, Transformer
from pyproj.exceptions import ProjError
from shapely import GeometryType
from shapely.affinity import affine_transform
from shapely.errors import GEOSException
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from geopilot.domain.data_asset import AssetMetadata, VectorMetadata
from geopilot.gis.measurement.coverage import MeasurementDecision, MeasurementPolicyError, geographic_bounds
from geopilot.gis.measurement.geodesic_buffer import (
    MAX_DISTANCE_M, MIN_DISTANCE_M, validate_geodesic_bounds,
)
from geopilot.gis.validation.crs_validator import inspect_crs


WGS84 = CRS.from_epsg(4326)
ROAD_STATION_SPACING_M = 5.0
PROJECTED_POLYGON_SPACING_M = 100.0
_NUMERICAL_PADDING_M = 0.01
CANDIDATE_PADDING_M = ROAD_STATION_SPACING_M / 2 + _NUMERICAL_PADDING_M
_AOI_EDGE_SPACING_M = 5_000.0
_ROAD_ARC_CHUNK_M = 25_000.0
_ROUND_QUADRANT_SEGMENTS = 64

_GEOD = Geod(ellps="WGS84")
_RADIANS_PER_DEGREE = math.pi / 180
_E_SQUARED = _GEOD.f * (2 - _GEOD.f)
_EQUATOR_METRES_PER_DEGREE = _GEOD.a * _RADIANS_PER_DEGREE
_MIN_MERIDIONAL_METRES_PER_DEGREE = _EQUATOR_METRES_PER_DEGREE * (1 - _E_SQUARED)
_MAX_METRES_PER_DEGREE = _EQUATOR_METRES_PER_DEGREE / math.sqrt(1 - _E_SQUARED)


def _check_source_crs(source: CRS) -> None:
    if len(source.axis_info) != 2 or not (source.is_geographic or source.is_projected):
        raise ValueError("WGS84 preparation requires a two-dimensional geographic or projected CRS")


def validate_geodesic_scope_input(
    metadata: AssetMetadata, *, field: str, allow_empty: bool,
) -> MeasurementDecision:
    """Validate source CRS and bounds before the AOI worker starts."""
    if not isinstance(metadata, VectorMetadata):
        raise MeasurementPolicyError("WGS84 influence scope requires a vector layer", field=field, observed=metadata.kind)
    source = inspect_crs(metadata, field=field).crs
    try:
        _check_source_crs(source)
        if not allow_empty or metadata.feature_count != 0:
            validate_geodesic_bounds(geographic_bounds(source, metadata.bounds, field=field))
    except MeasurementPolicyError:
        raise
    except ValueError as error:
        raise MeasurementPolicyError(str(error), field=field, observed=metadata.bounds) from error
    return MeasurementDecision(method="geodesic", crs=WGS84.to_string(), unit="meter")


def _check_shapes(geometry: BaseGeometry | np.ndarray) -> np.ndarray:
    coordinates = shapely.get_coordinates(geometry)
    if (
        np.any(shapely.has_z(geometry) | shapely.has_m(geometry) | shapely.is_empty(geometry))
        or not np.all(shapely.is_valid(geometry))
        or not np.isfinite(coordinates).all()
    ):
        raise ValueError("WGS84 preparation requires valid, finite, nonempty 2D features")
    return coordinates


def _check_wgs84_coordinates(coordinates: np.ndarray) -> None:
    if np.any(np.abs(coordinates[:, 0]) > 180) or np.any(np.abs(coordinates[:, 1]) > 90):
        raise ValueError("WGS84 coordinates must fall within valid longitude and latitude limits")


def prepare_wgs84_geometry(
    geometry: BaseGeometry | np.ndarray, source_crs: CRS | str,
) -> BaseGeometry | np.ndarray:
    """Transform one geometry or a layer of one geometry family to EPSG:4326.

    Projected polygon edges are sampled in their source grid before conversion.
    Road vertices stay intact for the T04 shortest-arc interpretation.
    """
    source = CRS.from_user_input(source_crs)
    _check_source_crs(source)
    coordinates = _check_shapes(geometry)
    if coordinates.size == 0:
        return geometry
    try:
        if not source.equals(WGS84, ignore_axis_order=True):
            if source.is_projected and np.all(np.isin(
                shapely.get_type_id(geometry), (GeometryType.POLYGON, GeometryType.MULTIPOLYGON),
            )):
                metres_per_unit = max(axis.unit_conversion_factor for axis in source.axis_info)
                if not math.isfinite(metres_per_unit) or metres_per_unit <= 0:
                    raise ValueError("Projected polygon axes require finite linear units")
                geometry = shapely.segmentize(geometry, PROJECTED_POLYGON_SPACING_M / metres_per_unit)
            transformer = Transformer.from_crs(source, WGS84, always_xy=True, allow_ballpark=False, only_best=True)
            geometry = shapely.transform(
                geometry, lambda x, y: transformer.transform(x, y, errcheck=True), interleaved=False,
            )
            coordinates = _check_shapes(geometry)
    except (ProjError, GEOSException) as error:
        raise ValueError("Vector geometry cannot be converted to WGS84 with the available transformation") from error
    _check_wgs84_coordinates(coordinates)
    return geometry


def _edge_influence(start: Sequence[float], end: Sequence[float], radius: float) -> Polygon:
    # Any path of length <= radius stays within radius / M_min degrees of its starting latitude.
    latitude_delta = radius / _MIN_MERIDIONAL_METRES_PER_DEGREE
    south = min(start[1], end[1]) - latitude_delta
    north = max(start[1], end[1]) + latitude_delta
    near_equator = 0.0 if south <= 0 <= north else min(abs(south), abs(north))
    far_from_equator = max(abs(south), abs(north))
    if far_from_equator >= 90:
        raise ValueError("The WGS84 influence envelope reaches a pole")

    # ds² = (M dφ)² + (N cos(φ) dλ)², where M and N are the WGS84 curvature radii.
    # Lower bounds on M and N cos(φ) follow from this edge's reachable latitude band.
    # A path's length in this edge's affine chart cannot exceed its ellipsoidal length.
    curvature_factor = math.sqrt(1 - _E_SQUARED * math.sin(math.radians(near_equator)) ** 2)
    latitude_scale = _MIN_MERIDIONAL_METRES_PER_DEGREE / curvature_factor ** 3
    longitude_scale = _EQUATOR_METRES_PER_DEGREE * math.cos(math.radians(far_from_equator)) / curvature_factor
    edge = LineString([(0.0, 0.0), ((end[0] - start[0]) * longitude_scale, (end[1] - start[1]) * latitude_scale)])
    # Circumscribe the true metric cap to avoid gaps between the round cap's vertices.
    outer_radius = radius / math.cos(math.pi / (4 * _ROUND_QUADRANT_SEGMENTS))
    cover = edge.buffer(outer_radius, quad_segs=_ROUND_QUADRANT_SEGMENTS)
    return affine_transform(cover, (1 / longitude_scale, 0, 0, 1 / latitude_scale, start[0], start[1]))


def expand_wgs84_analysis_extent(geometry: Polygon | MultiPolygon, distance_m: float) -> Polygon | MultiPolygon:
    """Cover the geodesic influence zone plus half a road sampling interval.

    The original area is unioned with conservative ellipsoid distance envelopes
    along every exterior and interior edge. The padding is for candidate selection.
    """
    if type(distance_m) not in (int, float) or not MIN_DISTANCE_M <= distance_m <= MAX_DISTANCE_M:
        raise ValueError(f"Influence distance must be between {MIN_DISTANCE_M:g} and {MAX_DISTANCE_M:g} metres")
    if not isinstance(geometry, (Polygon, MultiPolygon)):
        raise ValueError("WGS84 influence extent requires a polygon or multipolygon")
    _check_shapes(geometry)
    validate_geodesic_bounds(geometry.bounds)
    radius = distance_m + CANDIDATE_PADDING_M
    split = shapely.segmentize(geometry, _AOI_EDGE_SPACING_M / _MAX_METRES_PER_DEGREE)
    polygons = (split,) if isinstance(split, Polygon) else split.geoms
    envelope = [geometry]
    for polygon in polygons:
        for ring in (polygon.exterior, *polygon.interiors):
            coordinates = list(ring.coords)
            envelope.extend(
                _edge_influence(start, end, radius)
                for start, end in zip(coordinates[:-1], coordinates[1:], strict=True) if start != end
            )
    try:
        expanded = shapely.union_all(envelope)
    except GEOSException as error:
        raise ValueError("WGS84 influence envelope could not form a polygon") from error
    if not isinstance(expanded, (Polygon, MultiPolygon)) or expanded.is_empty or not expanded.is_valid:
        raise ValueError("WGS84 influence envelope must be a valid polygon")
    validate_geodesic_bounds(expanded.bounds)
    return expanded


def _longitude_near(longitude: float | np.ndarray, pivot: float) -> float | np.ndarray:
    return (longitude - pivot + 180) % 360 - 180 + pivot


def _arc_may_reach(lon: float, lat: float, radius: float, bounds: Sequence[float]) -> bool:
    west, south, east, north = bounds
    latitude_delta = radius / _MIN_MERIDIONAL_METRES_PER_DEGREE
    if lat + latitude_delta < south or lat - latitude_delta > north:
        return False
    latitude_limit = abs(lat) + latitude_delta
    if latitude_limit >= 90:
        return True
    longitude_delta = radius / (_EQUATOR_METRES_PER_DEGREE * math.cos(math.radians(latitude_limit)))
    centered = _longitude_near(lon, (west + east) / 2)
    return west - longitude_delta <= centered <= east + longitude_delta


def _segment_reaches_scope(
    start: Sequence[float], end: Sequence[float], area: Polygon | MultiPolygon, bounds: Sequence[float],
) -> bool:
    azimuth, _, length = _GEOD.inv(*start, *end)
    if not math.isfinite(length):
        raise ValueError("The shortest geodesic arc of a road segment could not be resolved")
    if length == 0:
        return False
    pivot = (bounds[0] + bounds[2]) / 2
    intervals = [(0.0, length)]
    while intervals:
        low, high = intervals.pop()
        span = high - low
        midpoint = (low + high) / 2
        lon, lat, _ = _GEOD.fwd(*start, azimuth, midpoint)
        if not _arc_may_reach(lon, lat, span / 2 + _NUMERICAL_PADDING_M, bounds):
            continue
        if span > _ROAD_ARC_CHUNK_M:
            intervals.extend(((midpoint, high), (low, midpoint)))
            continue
        stations = np.linspace(low, high, math.ceil(span / ROAD_STATION_SPACING_M) + 1)
        longitude, latitude, _ = _GEOD.fwd(
            np.full_like(stations, start[0]), np.full_like(stations, start[1]), np.full_like(stations, azimuth), stations,
        )
        longitude = _longitude_near(longitude, pivot)
        coordinates = np.column_stack((longitude, latitude))
        if not np.isfinite(coordinates).all() or np.any(np.abs(np.diff(longitude)) >= 180):
            raise ValueError("A potentially influencing road arc cannot be represented in the local WGS84 chart")
        if area.intersects(LineString(coordinates)):
            return True
    return False


def road_intersects_influence(line: LineString | MultiLineString, expanded_extent: Polygon | MultiPolygon) -> bool:
    """Test the same shortest WGS84 arcs used by the buffer without clipping the road."""
    if not isinstance(line, (LineString, MultiLineString)):
        raise ValueError("Road selection requires line or multiline features")
    _check_wgs84_coordinates(_check_shapes(line))
    bounds = expanded_extent.bounds
    validate_geodesic_bounds(bounds)
    parts = (line,) if isinstance(line, LineString) else line.geoms
    for part in parts:
        vertices = list(part.coords)
        if any(_segment_reaches_scope(start, end, expanded_extent, bounds)
               for start, end in zip(vertices[:-1], vertices[1:], strict=True)):
            return True
    return False
