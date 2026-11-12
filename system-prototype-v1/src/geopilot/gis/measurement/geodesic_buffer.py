"""Sample WGS84 geodesic offset curves and round caps for local road lines."""

import math
from collections.abc import Sequence

import numpy as np
import shapely
from pyproj import CRS, Geod
from shapely.errors import GEOSException
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon

from geopilot.domain.data_asset import AssetMetadata, VectorMetadata
from geopilot.gis.measurement.coverage import MeasurementDecision, MeasurementPolicyError
from geopilot.gis.validation.crs_validator import inspect_crs


MAX_SEGMENT_STEP_M = 25.0
MAX_CAP_ANGLE_DEG = 1.0
MIN_DISTANCE_M = 1.0
MAX_DISTANCE_M = 2_000.0
MAX_SEGMENT_LENGTH_M = 100_000.0
MAX_FEATURE_LENGTH_M = 200_000.0
MAX_SEGMENTS = 2_000
MAX_LATITUDE_DEG = 75.0
MAX_LONGITUDE_SPAN_DEG = 6.0
_WGS84 = CRS.from_epsg(4326)
_GEOD = Geod(ellps="WGS84")
_CAP_INTERVALS = math.ceil(180.0 / MAX_CAP_ANGLE_DEG)


def validate_geodesic_bounds(bounds: Sequence[float] | None) -> None:
    """Require a finite local longitude/latitude extent in one coordinate chart."""
    if bounds is None or len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
        raise ValueError("The WGS84 geodesic buffer requires a finite longitude/latitude extent")
    west, south, east, north = bounds
    if not -180 < west <= east < 180 or east - west > MAX_LONGITUDE_SPAN_DEG:
        raise ValueError(f"The extent must avoid the antimeridian and span at most {MAX_LONGITUDE_SPAN_DEG:g}° longitude")
    if not -MAX_LATITUDE_DEG <= south <= north <= MAX_LATITUDE_DEG:
        raise ValueError(f"The extent must stay between ±{MAX_LATITUDE_DEG:g}° latitude")


def validate_geodesic_buffer_input(
    metadata: AssetMetadata, *, field: str = "inputs.source.crs",
) -> MeasurementDecision:
    """Validate the input metadata of this WGS84 geodesic tool only."""
    if not isinstance(metadata, VectorMetadata) or metadata.geometry_type not in {"LineString", "MultiLineString"}:
        raise MeasurementPolicyError(
            "The WGS84 geodesic buffer requires a 2D line layer", field=field,
            observed=getattr(metadata, "geometry_type", metadata.kind),
        )
    crs = inspect_crs(metadata, field=field).crs
    if not crs.equals(_WGS84, ignore_axis_order=True):
        raise MeasurementPolicyError(
            "The geodesic buffer requires 2D WGS84 longitude/latitude input", field=field, observed=metadata.crs,
        )
    if metadata.feature_count != 0:
        try:
            validate_geodesic_bounds(metadata.bounds)
        except ValueError as error:
            raise MeasurementPolicyError(str(error), field=field, observed=metadata.bounds) from error
    return MeasurementDecision(method="geodesic", crs=_WGS84.to_string(), unit="meter")


def _offset(lons: np.ndarray, lats: np.ndarray, bearings: np.ndarray, distance: float) -> np.ndarray:
    x, y, _ = _GEOD.fwd(lons, lats, bearings, np.full(bearings.shape, distance))
    return np.column_stack((x, y))


def _round_cap(vertex: np.ndarray, angle: float, distance: float) -> np.ndarray:
    bearings = np.linspace(angle, angle + 180.0, _CAP_INTERVALS + 1)[1:-1]
    return _offset(np.full_like(bearings, vertex[0]), np.full_like(bearings, vertex[1]), bearings, distance)


def _capsule(start: np.ndarray, end: np.ndarray, azimuth: float, length: float, distance: float) -> Polygon:
    stations = np.linspace(0.0, length, math.ceil(length / MAX_SEGMENT_STEP_M) + 1)
    lons, lats, tangents = _GEOD.fwd(
        np.full_like(stations, start[0]), np.full_like(stations, start[1]), np.full_like(stations, azimuth),
        stations, return_back_azimuth=False,
    )
    lons[0], lats[0] = start
    lons[-1], lats[-1] = end
    left = _offset(lons, lats, tangents - 90.0, distance)
    right = _offset(lons, lats, tangents + 90.0, distance)
    end_cap = _round_cap(end, tangents[-1] - 90.0, distance)
    start_cap = _round_cap(start, tangents[0] + 90.0, distance)
    ring = np.concatenate((left, end_cap, right[::-1], start_cap))
    if not np.isfinite(ring).all():
        raise ValueError("The WGS84 offset calculation returned nonfinite coordinates")
    west, south = ring.min(axis=0)
    east, north = ring.max(axis=0)
    validate_geodesic_bounds((west, south, east, north))
    polygon = Polygon(ring)
    if polygon.is_empty or not polygon.is_valid:
        raise ValueError("The geodesic offset curve cannot form a valid segment buffer in the supported extent")
    return polygon


def merge_geodesic_buffers(polygons: Sequence[Polygon | MultiPolygon]) -> Polygon | MultiPolygon:
    """Dissolve geodesically constructed polygons in the verified longitude/latitude chart."""
    try:
        merged = shapely.union_all(polygons)
    except GEOSException as error:
        raise ValueError("Could not merge the geodesic buffer polygons") from error
    if not isinstance(merged, (Polygon, MultiPolygon)) or merged.is_empty or not merged.is_valid:
        raise ValueError("The geodesic buffer merge did not produce valid polygon geometry")
    validate_geodesic_bounds(merged.bounds)
    return merged


def _line_segments(line: LineString | MultiLineString) -> list[tuple[np.ndarray, np.ndarray, float, float]]:
    if not isinstance(line, (LineString, MultiLineString)) or line.is_empty:
        raise ValueError("The WGS84 geodesic buffer requires a nonempty 2D line")
    if line.has_z or line.has_m:
        raise ValueError("The WGS84 geodesic buffer requires 2D coordinates")
    validate_geodesic_bounds(line.bounds)
    if not line.is_valid:
        raise ValueError("The WGS84 geodesic buffer requires valid line geometry")

    segments = []
    total_length = 0.0
    parts = (line,) if isinstance(line, LineString) else line.geoms
    for part in parts:
        coordinates = np.asarray(part.coords)
        for start, end in zip(coordinates[:-1], coordinates[1:], strict=True):
            azimuth, _, length = _GEOD.inv(*start, *end)
            if not math.isfinite(length) or length > MAX_SEGMENT_LENGTH_M:
                raise ValueError(f"Geodesic segment length exceeds {MAX_SEGMENT_LENGTH_M:g} metres or is nonfinite")
            if length == 0:
                continue
            segments.append((start, end, azimuth, length))
            total_length += length
            if total_length > MAX_FEATURE_LENGTH_M:
                raise ValueError(f"Geodesic total length exceeds {MAX_FEATURE_LENGTH_M:g} metres per feature")
            if len(segments) > MAX_SEGMENTS:
                raise ValueError(f"Geodesic buffer supports at most {MAX_SEGMENTS} segments per feature")
    if not segments:
        raise ValueError("The WGS84 geodesic buffer requires at least one positive-length segment")
    return segments


def validate_geodesic_line(line: LineString | MultiLineString) -> None:
    """Check that a selected WGS84 road fits the buffer's verified geometry limits."""
    _line_segments(line)


def geodesic_buffer(line: LineString | MultiLineString, distance_m: float) -> Polygon | MultiPolygon:
    """Buffer the shortest WGS84 geodesic between each pair of adjacent vertices."""
    if type(distance_m) not in (int, float) or not MIN_DISTANCE_M <= distance_m <= MAX_DISTANCE_M:
        raise ValueError(f"Geodesic buffer distance must be between {MIN_DISTANCE_M:g} and {MAX_DISTANCE_M:g} metres")
    segments = _line_segments(line)
    return merge_geodesic_buffers([_capsule(*segment, distance_m) for segment in segments])
