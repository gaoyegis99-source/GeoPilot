"""Select complete vector features by a topological relation to a polygonal area."""

import numpy as np
import shapely
from geopandas import GeoDataFrame, GeoSeries
from shapely.errors import GEOSException
from shapely.geometry.base import BaseGeometry

from geopilot.gis.validation.crs_validator import CRSValidationError, _parse_crs


def _geometry_error(frame: GeoDataFrame, role: str, position: int, reason: str) -> ValueError:
    identity = f"row {position}, index {frame.index[position]!r}"
    if "source_feature_id" in frame:
        identifier = frame["source_feature_id"].iloc[position]
        identity += f", source_feature_id {identifier!r}"
    return ValueError(f"{role}.geometry has {reason} at {identity}")


def _checked_geometry(frame: GeoDataFrame, role: str, *, polygonal: bool = False) -> GeoSeries:
    if not isinstance(frame, GeoDataFrame):
        raise ValueError(f"{role} requires a GeoDataFrame")
    try:
        geometry = frame.geometry
    except AttributeError as error:
        raise ValueError(f"{role}.geometry requires an active geometry column") from error
    if polygonal and geometry.empty:
        raise ValueError(f"{role}.geometry requires at least one polygon")

    values = geometry.to_numpy()
    valid = shapely.is_valid(values)
    bad = ~valid | shapely.is_empty(values)
    if polygonal:
        bad |= ~geometry.geom_type.isin({"Polygon", "MultiPolygon"}).to_numpy()
    if bad.any():
        position = int(bad.argmax())
        shape = values[position]
        if shape is None:
            reason = "missing geometry"
        elif shape.is_empty:
            reason = "empty geometry"
        elif not valid[position]:
            reason = f"invalid geometry ({shapely.is_valid_reason(shape)})"
        else:
            reason = f"unsupported {shape.geom_type} geometry in a polygonal area"
        raise _geometry_error(frame, role, position, reason)
    return geometry


def _prepare_selection(targets: GeoDataFrame, area: GeoDataFrame) -> tuple[np.ndarray, BaseGeometry]:
    target_geometry = _checked_geometry(targets, "targets")
    area_geometry = _checked_geometry(area, "area", polygonal=True)
    try:
        target_crs = _parse_crs(target_geometry.crs, "targets.crs")
        area_crs = _parse_crs(area_geometry.crs, "area.crs")
    except CRSValidationError as error:
        raise ValueError(f"{error.field} requires a known CRS. {error.message}") from error
    if not area_crs.equals(target_crs, ignore_axis_order=True):
        raise ValueError(f"area.crs {area_crs.to_string()} must match targets.crs {target_crs.to_string()}")

    try:
        scope = shapely.union_all(area_geometry.to_numpy())
    except GEOSException as error:
        raise ValueError(f"Could not union area.geometry polygons. {error}") from error
    return target_geometry.to_numpy(), scope


def points_covered_by_scope(points: np.ndarray, scope: BaseGeometry) -> np.ndarray:
    """Test point coverage, including contact with the shell and hole boundaries."""
    shapely.prepare(scope)
    return shapely.covers(scope, points)


def select_intersecting(targets: GeoDataFrame, area: GeoDataFrame) -> GeoDataFrame:
    """Return original target rows that intersect the area, including boundary contact."""
    target_geometry, scope = _prepare_selection(targets, area)
    retained = _evaluate_prepared(scope, target_geometry, shapely.intersects, shapely.intersects)
    return targets.iloc[retained].copy()


def select_within(targets: GeoDataFrame, area: GeoDataFrame) -> GeoDataFrame:
    """Return original target rows contained in the area with intersecting interiors."""
    target_geometry, scope = _prepare_selection(targets, area)
    retained = _evaluate_prepared(scope, target_geometry, shapely.contains, shapely.within)
    return targets.iloc[retained].copy()


def _evaluate_prepared(scope: BaseGeometry, targets: np.ndarray, predicate, original_predicate) -> np.ndarray:
    if targets.size:
        try:
            shapely.prepare(scope)
            return predicate(scope, targets)
        except GEOSException:
            # Preserve the original predicate result or error if preparation cannot be used.
            pass
    return original_predicate(targets, scope)
