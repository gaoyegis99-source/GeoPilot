"""Assign complete polygons to zones and allocate clipped polygon area."""

import math

from geopandas import GeoDataFrame
from shapely import STRtree, union_all
from shapely.errors import GEOSException
from shapely.geometry.base import BaseGeometry

from geopilot.domain.data_asset import VectorMetadata
from geopilot.gis.measurement.area import _uses_equal_area_method, validate_area
from geopilot.gis.measurement.coverage import MeasurementPolicyError, verify_projection_coverage
from geopilot.gis.validation.crs_validator import CRSInfo, CRSValidationError, inspect_crs


_IDENTIFIER = "source_feature_id"
_POLYGON_TYPES = {"Polygon", "MultiPolygon"}
_AREA_ABSOLUTE_TOLERANCE_M2 = 1e-6
_AREA_RELATIVE_TOLERANCE = 1e-9
_AREA_INTERSECTION = "2********"


def _polygon_layer(frame: GeoDataFrame, role: str) -> tuple[list[str], VectorMetadata]:
    if not isinstance(frame, GeoDataFrame) or frame.active_geometry_name is None:
        raise ValueError(f"{role} requires a GeoDataFrame with polygon geometry")
    if list(frame.columns).count(_IDENTIFIER) != 1:
        raise ValueError(f"{role} requires one {_IDENTIFIER} field")
    source_ids = frame[_IDENTIFIER]
    identifiers = source_ids.astype(str)
    if source_ids.isna().any() or identifiers.str.strip().eq("").any() or not identifiers.is_unique:
        raise ValueError(f"{role} requires nonempty, unique {_IDENTIFIER} values")

    geometry = frame.geometry
    if not geometry.geom_type.isin(_POLYGON_TYPES).all() or geometry.is_empty.any() or not geometry.is_valid.all():
        raise ValueError(f"{role} contains missing, empty, invalid, or nonpolygon geometry")
    crs = None if frame.crs is None else frame.crs.to_string()
    metadata = VectorMetadata(
        format="GeoDataFrame", crs=crs,
        bounds=None if frame.empty else tuple(float(value) for value in geometry.total_bounds),
        geometry_type="/".join(sorted(geometry.geom_type.unique())) or "Unknown",
        feature_count=len(frame), fields=(), issues=("CRS_UNKNOWN",) if crs is None else (),
    )
    return identifiers.tolist(), metadata


def _matching_crs(
    targets: VectorMetadata, zones: VectorMetadata, *, require_projected_metres: bool = False,
) -> tuple[CRSInfo, CRSInfo]:
    target_info = inspect_crs(targets, field="inputs.targets.crs", require_projected_metres=require_projected_metres)
    zone_info = inspect_crs(zones, field="inputs.zones.crs", require_projected_metres=require_projected_metres)
    if not target_info.crs.equals(zone_info.crs, ignore_axis_order=True):
        raise CRSValidationError(
            "Targets and zones must use the same CRS", code="MEASUREMENT_POLICY_REJECTED",
            field="inputs.zones.crs", observed=zones.crs,
        )
    return target_info, zone_info


def assign_by_centroid(
    targets: GeoDataFrame, zones: GeoDataFrame, *, boundary_tie: str = "unassigned",
) -> dict[str, str | None]:
    """Return a zone ID for each complete polygon with a uniquely interior centroid."""
    if boundary_tie != "unassigned":
        raise ValueError("boundary_tie must be 'unassigned'")

    target_ids, target_metadata = _polygon_layer(targets, "targets")
    zone_ids, zone_metadata = _polygon_layer(zones, "zones")
    target_info, zone_info = _matching_crs(target_metadata, zone_metadata, require_projected_metres=True)
    if target_metadata.bounds is not None:
        verify_projection_coverage(target_info.crs, target_metadata.bounds, field="inputs.targets.crs")
    if zone_metadata.bounds is not None:
        verify_projection_coverage(zone_info.crs, zone_metadata.bounds, field="inputs.zones.crs")

    assignments: dict[str, str | None] = dict.fromkeys(target_ids)
    if not target_ids or not zone_ids:
        return assignments

    tree = STRtree(zones.geometry.array)
    for identifier, polygon in zip(target_ids, targets.geometry.array, strict=True):
        centroid = polygon.centroid
        matches = tree.query(centroid, predicate="intersects")
        if len(matches) == 1:
            index = matches[0]
            if tree.geometries[index].contains(centroid):
                assignments[identifier] = zone_ids[index]
    return assignments


def _validate_area_scope(targets: VectorMetadata, zones: VectorMetadata) -> None:
    target_info, _ = _matching_crs(targets, zones)
    bounds = targets.bounds if targets.bounds is not None else zones.bounds
    if bounds is None:
        # Empty layers have no extent to test, but still declare a measurement CRS.
        horizontal = target_info.crs.to_2d()
        if horizontal.is_bound and horizontal.source_crs is not None:
            horizontal = horizontal.source_crs.to_2d()
        if (target_info.metres_per_unit != 1 or target_info.is_web_mercator
                or not _uses_equal_area_method(horizontal.coordinate_operation)):
            raise MeasurementPolicyError(
                "Area measurement requires a supported equal-area metre CRS",
                field="inputs.targets.crs", observed=targets.crs,
            )
        return

    for role, metadata in (("targets", targets), ("zones", zones)):
        if metadata.bounds is None:
            # The other layer supplies the only actual extent in the shared CRS.
            metadata = metadata.model_copy(update={"bounds": bounds})
        validate_area(metadata, field=f"inputs.{role}.crs")


def _split_polygon(
    identifier: str, polygon: BaseGeometry, tree: STRtree, zone_ids: list[str],
) -> dict[str | None, float]:
    total = polygon.area
    if not math.isfinite(total) or total <= 0:
        raise ValueError(f"Target {identifier!r} requires a finite positive area")

    parts: dict[int, float] = {}
    for index in sorted(tree.query(polygon, predicate="intersects")):
        zone = tree.geometries[index]
        area = polygon.intersection(zone).area
        if not math.isfinite(area):
            raise ValueError(f"Intersection area for target {identifier!r}, zone {zone_ids[index]!r} is not finite")
        if area == 0:
            continue

        # Test overlap on the source zones so rounding at a shared edge does not create a false tie.
        for neighbour in tree.query(zone, predicate="intersects"):
            if neighbour not in parts:
                continue
            other_zone = tree.geometries[neighbour]
            if (zone.relate_pattern(other_zone, _AREA_INTERSECTION)
                    and polygon.relate_pattern(zone.intersection(other_zone), _AREA_INTERSECTION)):
                raise ValueError(f"Zones {zone_ids[neighbour]!r} and {zone_ids[index]!r} overlap within target {identifier!r}")
        parts[index] = area

    if not parts:
        return {None: total}
    assigned: dict[str | None, float] = {zone_ids[index]: area for index, area in parts.items()}
    covered = union_all([tree.geometries[index] for index in parts])
    unassigned = polygon.difference(covered).area
    if not math.isfinite(unassigned):
        raise ValueError(f"Unassigned area for target {identifier!r} is not finite")
    if unassigned > 0:
        assigned[None] = unassigned

    # This comparison only allows numeric summation and overlay roundoff.
    allocated = math.fsum(assigned.values())
    if not math.isclose(allocated, total,
                        rel_tol=_AREA_RELATIVE_TOLERANCE, abs_tol=_AREA_ABSOLUTE_TOLERANCE_M2):
        raise ValueError(f"Split area for target {identifier!r} totals {allocated} square metres, expected {total}")
    return assigned


def split_area_by_zone(targets: GeoDataFrame, zones: GeoDataFrame) -> dict[str, dict[str | None, float]]:
    """Allocate each clipped target's square metres, with None for uncovered area."""
    target_ids, target_metadata = _polygon_layer(targets, "targets")
    zone_ids, zone_metadata = _polygon_layer(zones, "zones")
    _validate_area_scope(target_metadata, zone_metadata)
    tree = STRtree(zones.geometry.array)
    return {
        identifier: _split_polygon(identifier, polygon, tree, zone_ids)
        for identifier, polygon in zip(target_ids, targets.geometry.array, strict=True)
    }


def _largest_overlap(identifier: str, polygon: BaseGeometry, tree: STRtree, zone_ids: list[str]) -> str | None:
    total = polygon.area
    if not math.isfinite(total) or total <= 0:
        raise ValueError(f"Target {identifier!r} requires a finite positive area")

    overlaps: dict[str, float] = {}
    for index in tree.query(polygon, predicate="intersects"):
        zone_id = zone_ids[index]
        try:
            area = polygon.intersection(tree.geometries[index]).area
        except GEOSException as error:
            raise ValueError(f"Cannot intersect target {identifier!r} with zone {zone_id!r}. {error}") from error
        if not math.isfinite(area):
            raise ValueError(f"Intersection area for target {identifier!r}, zone {zone_id!r} is not finite")
        if area > 0:
            overlaps[zone_id] = area
    if not overlaps:
        return None

    largest = max(overlaps.values())
    winners = [
        zone_id for zone_id, area in overlaps.items()
        if math.isclose(area, largest, rel_tol=_AREA_RELATIVE_TOLERANCE, abs_tol=_AREA_ABSOLUTE_TOLERANCE_M2)
    ]
    return winners[0] if len(winners) == 1 else None


def assign_by_largest_overlap(
    targets: GeoDataFrame, zones: GeoDataFrame, *, boundary_tie: str = "unassigned",
) -> dict[str, str | None]:
    """Assign each complete polygon to its unique maximum positive overlap in square metres."""
    if boundary_tie != "unassigned":
        raise ValueError("boundary_tie must be 'unassigned'")

    target_ids, target_metadata = _polygon_layer(targets, "targets")
    zone_ids, zone_metadata = _polygon_layer(zones, "zones")
    _validate_area_scope(target_metadata, zone_metadata)
    tree = STRtree(zones.geometry.array)
    return {
        identifier: _largest_overlap(identifier, polygon, tree, zone_ids)
        for identifier, polygon in zip(target_ids, targets.geometry.array, strict=True)
    }
