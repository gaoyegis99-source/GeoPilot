"""Deduplicate clipped polygon parts and summarize their zone allocations."""

import math
from dataclasses import dataclass
from typing import Literal

from geopandas import GeoDataFrame
from pydantic import JsonValue
from pyproj import CRS
from shapely import union_all
from shapely.errors import GEOSException
from shapely.geometry.base import BaseGeometry

from geopilot.gis.validation.crs_validator import CRSValidationError, _parse_crs
from geopilot.gis.vector.boundary_assignment import (
    _AREA_ABSOLUTE_TOLERANCE_M2,
    _AREA_RELATIVE_TOLERANCE,
    _IDENTIFIER,
    _POLYGON_TYPES,
    _polygon_layer,
    _validate_area_scope,
    assign_by_centroid,
    assign_by_largest_overlap,
    split_area_by_zone,
)
from geopilot.gis.vector.spatial_relations import _checked_geometry


_ASSIGNMENTS = {"centroid": assign_by_centroid, "largest_overlap": assign_by_largest_overlap}


@dataclass(frozen=True, slots=True)
class PolygonAllocation:
    source_feature_id: str
    zone_id: str | None
    object_status: Literal["assigned", "unassigned"]
    area_m2: float


@dataclass(frozen=True, slots=True)
class PolygonStatistics:
    details: tuple[PolygonAllocation, ...]
    summary: dict[str, JsonValue]


def _source_ids(frame: GeoDataFrame, role: str) -> list[str]:
    if list(frame.columns).count(_IDENTIFIER) != 1:
        raise ValueError(f"{role} requires one {_IDENTIFIER} field")
    values = frame[_IDENTIFIER]
    identifiers = values.astype(str)
    if values.isna().any() or identifiers.str.strip().eq("").any():
        raise ValueError(f"{role} requires nonempty {_IDENTIFIER} values")
    return identifiers.tolist()


def _original_geometries(frame: GeoDataFrame) -> dict[str, BaseGeometry]:
    geometry = _checked_geometry(frame, "original_targets")
    if not geometry.geom_type.isin(_POLYGON_TYPES).all():
        raise ValueError("original_targets requires complete Polygon or MultiPolygon geometries")
    originals = {}
    for identifier, shape in zip(_source_ids(frame, "original_targets"), geometry, strict=True):
        if not originals.setdefault(identifier, shape).equals(shape):
            raise ValueError(f"original_targets has conflicting complete geometries for {_IDENTIFIER} {identifier!r}")
    return originals


def _polygon_parts(shape: BaseGeometry):
    if shape.is_empty:
        return
    if shape.geom_type in _POLYGON_TYPES:
        yield shape
    elif shape.geom_type == "GeometryCollection":
        for part in shape.geoms:
            yield from _polygon_parts(part)


def _merged_clips(frame: GeoDataFrame, retained_ids: list[str]) -> dict[str, BaseGeometry]:
    parts: dict[str, list[BaseGeometry]] = {identifier: [] for identifier in retained_ids}
    for identifier, shape in zip(retained_ids, frame.geometry, strict=True):
        parts[identifier].extend(_polygon_parts(shape))
    merged = {}
    for identifier, polygons in parts.items():
        if not polygons:
            continue
        try:
            shape = union_all(polygons)
        except GEOSException as error:
            raise ValueError(f"Could not union clipped parts of {_IDENTIFIER} {identifier!r}. {error}") from error
        merged[identifier] = shape
    return merged


def _frame(geometries: dict[str, BaseGeometry], crs: CRS) -> GeoDataFrame:
    return GeoDataFrame({_IDENTIFIER: list(geometries)}, geometry=list(geometries.values()), crs=crs)


def _uncovered_area(merged: dict[str, BaseGeometry], zones: GeoDataFrame) -> float:
    if not merged:
        return 0.0
    try:
        coverage = union_all(zones.geometry.to_numpy())
        return math.fsum(shape.difference(coverage).area for shape in merged.values())
    except GEOSException as error:
        raise ValueError(f"Could not calculate clipped area outside the zones. {error}") from error


def _summarize(
    allocations: dict[str, dict[str | None, float]], zone_ids: list[str], *,
    boundary_assignment: str, boundary_tie: str, area_crs: str, total_area: float, uncovered: float,
) -> PolygonStatistics:
    details = []
    unassigned_ids = []
    by_zone: dict[str, list[PolygonAllocation]] = {zone_id: [] for zone_id in sorted(zone_ids)}
    for identifier, shares in sorted(allocations.items()):
        assigned = any(zone is not None and area > 0 for zone, area in shares.items())
        if not assigned:
            unassigned_ids.append(identifier)
        for zone_id, area in sorted(shares.items(), key=lambda item: (item[0] is None, item[0] or "")):
            row = PolygonAllocation(identifier, zone_id, "assigned" if assigned else "unassigned", area)
            details.append(row)
            if zone_id is not None:
                by_zone[zone_id].append(row)

    assigned_area = math.fsum(row.area_m2 for row in details if row.zone_id is not None)
    unassigned_area = math.fsum(row.area_m2 for row in details if row.zone_id is None)
    if not math.isclose(math.fsum((assigned_area, unassigned_area)), total_area,
                        rel_tol=_AREA_RELATIVE_TOLERANCE, abs_tol=_AREA_ABSOLUTE_TOLERANCE_M2):
        raise ValueError("Assigned and unassigned area must sum to the deduplicated clipped area")
    if not math.isfinite(uncovered):
        raise ValueError("Uncovered area must be finite")

    summary = {
        "boundary_assignment": boundary_assignment, "boundary_tie": boundary_tie,
        "area_crs": area_crs, "area_unit": "square_meter",
        "counts": {
            "total": len(allocations), "assigned": len(allocations) - len(unassigned_ids),
            "unassigned": len(unassigned_ids),
        },
        "areas_m2": {"total": total_area, "assigned": assigned_area, "unassigned": unassigned_area, "uncovered": uncovered},
        "zones": [
            {"zone_id": zone_id, "distinct_count": len(rows), "area_m2": math.fsum(row.area_m2 for row in rows),
             "source_feature_ids": [row.source_feature_id for row in rows]}
            for zone_id, rows in by_zone.items()
        ],
        "unassigned_source_feature_ids": unassigned_ids,
    }
    return PolygonStatistics(tuple(details), summary)


def aggregate_polygon_statistics(
    targets: GeoDataFrame, original_targets: GeoDataFrame, zones: GeoDataFrame, *,
    boundary_assignment: str, area_crs: str, boundary_tie: str = "unassigned",
) -> PolygonStatistics:
    """Report zone area and unique object counts for the surviving original IDs."""
    if boundary_assignment not in {"split_by_area", *_ASSIGNMENTS}:
        raise ValueError(f"Unsupported boundary_assignment {boundary_assignment!r}")
    if boundary_tie != "unassigned":
        raise ValueError("boundary_tie must be 'unassigned'")
    if not isinstance(area_crs, str) or not area_crs.strip():
        raise ValueError("area_crs requires an explicit CRS definition")
    selected_crs = _parse_crs(area_crs, "parameters.area_crs")

    _checked_geometry(targets, "targets")
    retained_ids = _source_ids(targets, "targets")
    originals = _original_geometries(original_targets)
    zone_ids, zone_metadata = _polygon_layer(zones, "zones")
    for role, frame in (("targets", targets), ("original_targets", original_targets), ("zones", zones)):
        field = f"inputs.{role}.crs"
        observed = _parse_crs(frame.crs, field)
        if not observed.equals(selected_crs, ignore_axis_order=True):
            raise CRSValidationError(
                f"{role} CRS must match area_crs {area_crs}", code="MEASUREMENT_POLICY_REJECTED",
                field=field, observed=observed.to_string(),
            )
    missing = set(retained_ids) - originals.keys()
    if missing:
        raise ValueError(f"original_targets is missing {_IDENTIFIER} {min(missing)!r}")

    merged = _merged_clips(targets, retained_ids)
    clip_frame = _frame(merged, targets.crs)
    _, clip_metadata = _polygon_layer(clip_frame, "clipped_targets")
    survivors = _frame({identifier: originals[identifier] for identifier in dict.fromkeys(retained_ids)}, original_targets.crs)
    _, original_metadata = _polygon_layer(survivors, "original_targets")
    _validate_area_scope(clip_metadata, zone_metadata)
    _validate_area_scope(original_metadata, zone_metadata)

    clipped_areas = {identifier: shape.area for identifier, shape in merged.items()}
    for identifier, area in clipped_areas.items():
        if not math.isfinite(area) or area <= 0:
            raise ValueError(f"Clipped {_IDENTIFIER} {identifier!r} requires finite positive polygon area")

    if boundary_assignment == "split_by_area":
        allocations = split_area_by_zone(clip_frame, zones)
        uncovered = math.fsum(shares.get(None, 0.0) for shares in allocations.values())
    else:
        positive_originals = survivors.loc[survivors[_IDENTIFIER].isin(clipped_areas)]
        assigned = _ASSIGNMENTS[boundary_assignment](positive_originals, zones, boundary_tie=boundary_tie)
        allocations = {identifier: {zone: clipped_areas[identifier]} for identifier, zone in assigned.items()}
        uncovered = _uncovered_area(merged, zones)
    for identifier in retained_ids:
        allocations.setdefault(identifier, {None: 0.0})

    return _summarize(
        allocations, zone_ids, boundary_assignment=boundary_assignment, boundary_tie=boundary_tie, area_crs=area_crs,
        total_area=math.fsum(clipped_areas.values()), uncovered=uncovered,
    )
