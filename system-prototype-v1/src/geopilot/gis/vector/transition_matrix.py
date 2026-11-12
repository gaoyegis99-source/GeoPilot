"""Allocate land-use fragments and reconcile their provenance and area reports."""

import json
import math
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from numbers import Integral, Real

import shapely
from geopandas import GeoDataFrame
from pyproj import CRS
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from geopilot.domain.data_asset import VectorMetadata
from geopilot.gis.measurement.area import validate_area
from geopilot.gis.vector.boundary_assignment import (
    assign_by_centroid,
    assign_by_largest_overlap,
    split_area_by_zone,
)


_ID = "source_feature_id"
_PERIODS = ("first_period", "second_period")
_CLASSES = ("from_class", "to_class")
_SOURCES = ("first_source_feature_id", "second_source_feature_id", "first_source_code", "second_source_code")
_CHANGE_COLUMNS = (_ID, *_PERIODS, *_CLASSES, "is_change", "area_m2", "source_refs")
_MODES = {"split_by_area", "centroid", "largest_overlap"}


class TransitionEvidenceError(ValueError):
    """A discrepancy between reopened transition evidence and its expected value."""

    def __init__(self, field: str, *, observed: object, expected: object) -> None:
        super().__init__(f"{field} differs from the expected transition evidence")
        self.field = field
        self.observed = observed
        self.expected = expected


def _require(condition: bool, field: str, observed: object, expected: object) -> None:
    if not condition:
        raise TransitionEvidenceError(field, observed=observed, expected=expected)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _number(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _area_close(field: str, observed: object, expected: float, tolerance: float) -> None:
    _require(_number(observed) and abs(observed - expected) <= tolerance, field, observed, expected)


def _tolerance(value: float) -> None:
    _require(_number(value) and value >= 0, "area_tolerance_m2", value, "Finite nonnegative tolerance")


def _read_rows(frame: GeoDataFrame, field: str, columns: tuple[str, ...]) -> dict[str, dict]:
    missing = sorted(set(columns) - set(frame.columns))
    _require(not missing, field, missing, "All required columns present")
    records = {}
    for position, row in enumerate(frame[list(columns)].to_dict("records")):
        identifier = row[_ID]
        _require(_text(identifier) and identifier not in records, f"{field}.{_ID}", identifier, "Unique text ID")
        shape = frame.geometry.iloc[position]
        _require(
            isinstance(shape, (Polygon, MultiPolygon)) and shape.is_valid and not shape.is_empty
            and math.isfinite(shape.area) and shape.area > 0,
            f"{field}.{identifier}.geometry", getattr(shape, "geom_type", None), "Valid positive-area polygon",
        )
        records[identifier] = {**row, "geometry": shape}
    return records


@dataclass(frozen=True)
class _Fragment:
    identifier: str
    geometry: BaseGeometry
    periods: tuple[str, str]
    classes: tuple[str, str]
    sources: dict[str, str]

    @property
    def changed(self) -> bool:
        return self.classes[0] != self.classes[1]

    def reference(self) -> dict[str, str]:
        return {"fragment_id": self.identifier, **self.sources}


def _fragments(overlay: GeoDataFrame, tolerance: float) -> dict[str, _Fragment]:
    rows = _read_rows(overlay, "overlay_polygons", (_ID, *_PERIODS, *_CLASSES, *_SOURCES, "is_change", "area_m2"))
    _require(bool(rows), "overlay_polygons", len(rows), "At least one overlaid fragment")
    fragments = {}
    for identifier, row in rows.items():
        field = f"overlay_polygons.{identifier}"
        for column in (*_PERIODS, *_CLASSES, *_SOURCES):
            _require(_text(row[column]), f"{field}.{column}", row[column], "Nonempty text")
        fragment = _Fragment(
            identifier, row["geometry"], (row["first_period"], row["second_period"]),
            (row["from_class"], row["to_class"]), {column: row[column] for column in _SOURCES},
        )
        _require(type(row["is_change"]) is bool and row["is_change"] == fragment.changed,
                 f"{field}.is_change", row["is_change"], fragment.changed)
        _area_close(f"{field}.area_m2", row["area_m2"], fragment.geometry.area, tolerance)
        fragments[identifier] = fragment
    _require(len({fragment.periods for fragment in fragments.values()}) == 1, "overlay_polygons.periods",
             len({fragment.periods for fragment in fragments.values()}), "One source-period pair")
    return fragments


def _area_crs(overlay: GeoDataFrame, **frames: GeoDataFrame) -> CRS:
    _require(overlay.crs is not None, "overlay_polygons.crs", None, "Known equal-area CRS")
    area_crs = CRS.from_user_input(overlay.crs)
    validate_area(
        VectorMetadata(
            format="GeoDataFrame", crs=area_crs.to_string(),
            bounds=tuple(float(value) for value in overlay.total_bounds) if not overlay.empty else None,
            geometry_type="Polygon", feature_count=len(overlay), fields=(),
        ),
        field="overlay_polygons.crs",
    )
    for role, frame in frames.items():
        _require(frame.crs is not None and area_crs.equals(frame.crs, ignore_axis_order=True), f"{role}.crs",
                 frame.crs.to_string() if frame.crs is not None else None, area_crs.to_string())
    return area_crs


def _polygon_parts(shape: BaseGeometry) -> Iterator[Polygon]:
    if isinstance(shape, Polygon) and not shape.is_empty:
        yield shape
    elif shape.geom_type in {"MultiPolygon", "GeometryCollection"}:
        for part in shape.geoms:
            yield from _polygon_parts(part)


def _complete_targets(
    fragments: dict[str, _Fragment], first: GeoDataFrame, second: GeoDataFrame, tolerance: float,
) -> GeoDataFrame:
    columns = (_ID, "period", "class_code", "source_class_code")
    period_rows = (_read_rows(first, "first_classes", columns), _read_rows(second, "second_classes", columns))
    pair_shapes: dict[tuple[str, str], tuple[Polygon, ...]] = {}
    shapes = []
    for fragment in fragments.values():
        pair = tuple(fragment.sources[column] for column in _SOURCES[:2])
        field = f"overlay_polygons.{fragment.identifier}"
        rows = []
        for index, (identifier, source_rows) in enumerate(zip(pair, period_rows, strict=True)):
            row = source_rows.get(identifier)
            _require(row is not None, f"{field}.{_SOURCES[index]}", identifier, "Existing projected source ID")
            observed = [row["period"], row["class_code"], row["source_class_code"]]
            expected = [fragment.periods[index], fragment.classes[index], fragment.sources[_SOURCES[index + 2]]]
            _require(observed == expected, f"{field}.source_refs", observed, expected)
            rows.append(row)
        if pair not in pair_shapes:
            pair_shapes[pair] = tuple(_polygon_parts(rows[0]["geometry"].intersection(rows[1]["geometry"])))
        matches = [
            shape for shape in pair_shapes[pair]
            if fragment.geometry.intersection(shape).area > 0
            and fragment.geometry.difference(shape).area <= tolerance
        ]
        _require(len(matches) == 1, f"{field}.geometry", len(matches), "One matching complete overlap part")
        shapes.append(matches[0])
    return GeoDataFrame({_ID: list(fragments)}, geometry=shapes, crs=first.crs)


def _zone_order(zone_ids: list[str]) -> list[str | None]:
    return [*zone_ids, None]


def allocate_transition_areas(
    overlay: GeoDataFrame, first: GeoDataFrame, second: GeoDataFrame, zones: GeoDataFrame, *,
    boundary_assignment: str, boundary_tie: str, area_tolerance_m2: float,
) -> dict:
    """Allocate clipped area using complete targets for the single-zone methods."""
    _tolerance(area_tolerance_m2)
    area_crs = _area_crs(overlay, first_classes=first, second_classes=second, zones=zones)
    fragments = _fragments(overlay, area_tolerance_m2)
    complete = _complete_targets(fragments, first, second, area_tolerance_m2)
    if _ID in zones:
        zone_ids = [str(value) if isinstance(value, Integral) and not isinstance(value, bool) else value
                    for value in zones[_ID]]
        zones = zones.assign(**{_ID: zone_ids})
    zone_ids = sorted(_read_rows(zones, "zones", (_ID,)))
    _require(boundary_assignment in _MODES, "boundary_assignment", boundary_assignment, sorted(_MODES))
    _require(boundary_tie == "unassigned", "boundary_tie", boundary_tie, "unassigned")
    if boundary_assignment == "split_by_area":
        areas = split_area_by_zone(overlay, zones)
    else:
        assign = assign_by_centroid if boundary_assignment == "centroid" else assign_by_largest_overlap
        chosen = assign(complete, zones, boundary_tie=boundary_tie)
        areas = {identifier: {chosen[identifier]: fragment.geometry.area} for identifier, fragment in fragments.items()}
    rows = [
        {"fragment_id": identifier, "zone_id": zone, "area_m2": value}
        for identifier in sorted(areas)
        for zone, value in sorted(areas[identifier].items(), key=lambda entry: (entry[0] is None, entry[0] or ""))
    ]
    return {
        "area_crs": area_crs.to_string(), "area_unit": "square_meter", "boundary_assignment": boundary_assignment,
        "zone_ids": zone_ids, "allocations": rows,
    }


def _connected_parts(group: list[_Fragment]) -> Iterator[list[_Fragment]]:
    geometries = [fragment.geometry for fragment in group]
    tree = shapely.STRtree(geometries)
    remaining = set(range(len(group)))
    for root in range(len(group)):
        if root not in remaining:
            continue
        remaining.remove(root)
        stack, members = [root], []
        while stack:
            index = stack.pop()
            members.append(index)
            geometry = geometries[index]
            for other in tree.query(geometry, predicate="touches"):
                if other in remaining and geometry.intersection(geometries[other]).length > 0:
                    remaining.remove(other)
                    stack.append(other)
        yield [group[index] for index in sorted(members)]


def _patches(fragments: dict[str, _Fragment], merge: bool) -> Iterator[list[_Fragment]]:
    groups = defaultdict(list)
    for fragment in sorted(fragments.values(), key=lambda item: (item.classes, item.identifier)):
        groups[fragment.classes].append(fragment)
    for group in groups.values():
        if merge:
            yield from _connected_parts(group)
        else:
            yield from ([fragment] for fragment in group)


def build_change_polygons(
    overlay: GeoDataFrame, *, minimum_change_area_m2: float, merge_adjacent_classes: bool, area_tolerance_m2: float,
) -> tuple[GeoDataFrame, dict]:
    """Merge adjacent equal transitions and apply the confirmed threshold to changed patches."""
    _tolerance(area_tolerance_m2)
    _require(_number(minimum_change_area_m2) and minimum_change_area_m2 >= 0, "minimum_change_area_m2",
             minimum_change_area_m2, "Finite nonnegative area")
    _require(type(merge_adjacent_classes) is bool, "merge_adjacent_classes", merge_adjacent_classes, "Boolean")
    area_crs = _area_crs(overlay)
    fragments = _fragments(overlay, area_tolerance_m2)
    retained, filtered = [], []
    for index, members in enumerate(_patches(fragments, merge_adjacent_classes), start=1):
        identifier = f"patch-{index:06d}"
        shape = shapely.union_all([member.geometry for member in members])
        _require(isinstance(shape, (Polygon, MultiPolygon)) and shape.is_valid and not shape.is_empty,
                 f"change_polygons.{identifier}.geometry", shape.geom_type, "Valid polygonal union")
        if isinstance(shape, Polygon):
            shape = MultiPolygon([shape])
        _area_close(f"change_polygons.{identifier}.area_m2", shape.area,
                    math.fsum(member.geometry.area for member in members), area_tolerance_m2)
        first = members[0]
        if first.changed and shape.area < minimum_change_area_m2:
            filtered.append({
                _ID: identifier, "fragment_ids": [member.identifier for member in members], "area_m2": shape.area,
            })
            continue
        retained.append({
            _ID: identifier, "first_period": first.periods[0], "second_period": first.periods[1],
            "from_class": first.classes[0], "to_class": first.classes[1], "is_change": first.changed,
            "area_m2": shape.area, "source_refs": json.dumps([member.reference() for member in members], ensure_ascii=False),
            "geometry": shape,
        })
    pre_filter_area = math.fsum(fragment.geometry.area for fragment in fragments.values())
    retained_area = math.fsum(row["area_m2"] for row in retained)
    filtered_area = math.fsum(row["area_m2"] for row in filtered)
    _area_close("change_report.pre_filter_area_m2", retained_area + filtered_area, pre_filter_area, area_tolerance_m2)
    changes = GeoDataFrame(retained, columns=[*_CHANGE_COLUMNS, "geometry"], geometry="geometry", crs=area_crs)
    changes = changes.astype({"area_m2": "float64", "is_change": "bool"})
    report = {
        "minimum_change_area_m2": minimum_change_area_m2, "merge_adjacent_classes": merge_adjacent_classes,
        "area_tolerance_m2": area_tolerance_m2, "pre_filter_area_m2": pre_filter_area,
        "retained_area_m2": retained_area, "filtered_area_m2": filtered_area,
        "pre_filter_patch_count": len(retained) + len(filtered),
        "retained_patch_count": len(retained), "filtered_patch_count": len(filtered),
        "pre_filter_fragment_count": len(fragments),
        "filtered_fragment_count": sum(len(row["fragment_ids"]) for row in filtered),
        "filtered": filtered,
    }
    return changes, report


def _compare(field: str, observed: object, expected: object, tolerance: float) -> None:
    if isinstance(expected, dict):
        _require(isinstance(observed, dict) and observed.keys() == expected.keys(), field, observed, list(expected))
        for key, value in expected.items():
            _compare(f"{field}.{key}", observed[key], value, tolerance)
    elif isinstance(expected, list):
        _require(isinstance(observed, list) and len(observed) == len(expected), field, observed, expected)
        for index, (actual, value) in enumerate(zip(observed, expected, strict=True)):
            _compare(f"{field}.{index}", actual, value, tolerance)
    elif type(expected) is float:
        _area_close(field, observed, expected, tolerance)
    else:
        _require(type(observed) is type(expected) and observed == expected, field, observed, expected)


def _source_refs(value: object, field: str) -> list[dict]:
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as error:
        raise TransitionEvidenceError(field, observed=value, expected="JSON source references") from error
    return result


def _verified_changes(
    overlay: GeoDataFrame, changes: GeoDataFrame, change_report: dict, tolerance: float,
) -> dict[str, dict]:
    _area_crs(overlay, change_polygons=changes)
    _require(isinstance(change_report, dict), "change_report", change_report, "JSON object")
    _area_close("change_report.area_tolerance_m2", change_report.get("area_tolerance_m2"), tolerance, 0.0)
    expected, report = build_change_polygons(
        overlay, minimum_change_area_m2=change_report.get("minimum_change_area_m2"),
        merge_adjacent_classes=change_report.get("merge_adjacent_classes"), area_tolerance_m2=tolerance,
    )
    _compare("change_report", change_report, report, tolerance)
    actual_rows = _read_rows(changes, "change_polygons", _CHANGE_COLUMNS)
    expected_rows = _read_rows(expected, "change_polygons", _CHANGE_COLUMNS)
    _require(actual_rows.keys() == expected_rows.keys(), "change_polygons.source_feature_id", sorted(actual_rows),
             sorted(expected_rows))
    for identifier, row in actual_rows.items():
        field = f"change_polygons.{identifier}"
        expected_row = expected_rows[identifier]
        for column in _CHANGE_COLUMNS:
            if column != "source_refs":
                _compare(f"{field}.{column}", row[column], expected_row[column], tolerance)
        sources = _source_refs(row["source_refs"], f"{field}.source_refs")
        _compare(f"{field}.source_refs", sources, json.loads(expected_row["source_refs"]), tolerance)
        _area_close(f"{field}.geometry", row["geometry"].symmetric_difference(expected_row["geometry"]).area,
                    0.0, tolerance)
        _area_close(f"{field}.area_m2", row["area_m2"], row["geometry"].area, tolerance)
        row["source_refs"] = sources
    return actual_rows


def _validated_allocations(
    allocation: dict, fragments: dict[str, _Fragment], area_crs: str, tolerance: float,
) -> tuple[list[str], dict[str, dict[str | None, float]]]:
    _require(isinstance(allocation, dict), "allocations", allocation, "JSON object")
    _compare("allocations.area_crs", allocation.get("area_crs"), area_crs, tolerance)
    _compare("allocations.area_unit", allocation.get("area_unit"), "square_meter", tolerance)
    mode = allocation.get("boundary_assignment")
    _require(isinstance(mode, str) and mode in _MODES, "allocations.boundary_assignment", mode, sorted(_MODES))
    zones, entries = allocation.get("zone_ids"), allocation.get("allocations")
    _require(isinstance(zones, list) and all(_text(zone) for zone in zones) and zones == sorted(set(zones)),
             "allocations.zone_ids", zones, "Sorted unique zone IDs")
    _require(isinstance(entries, list), "allocations.allocations", entries, "Allocation rows")
    allowed_zones = {*zones, None}
    groups: dict[str, dict[str | None, float]] = defaultdict(dict)
    for index, entry in enumerate(entries):
        field = f"allocations.allocations.{index}"
        _require(isinstance(entry, dict) and entry.keys() == {"fragment_id", "zone_id", "area_m2"},
                 field, entry, "Fragment ID, zone ID and area")
        identifier, zone, area = entry["fragment_id"], entry["zone_id"], entry["area_m2"]
        _require(_text(identifier) and identifier in fragments, f"{field}.fragment_id", identifier, "Known fragment ID")
        _require((zone is None or _text(zone)) and zone in allowed_zones and zone not in groups[identifier],
                 f"{field}.zone_id", zone, "A single entry per fragment and declared zone")
        _require(_number(area) and area > 0, f"{field}.area_m2", area, "Finite positive area")
        groups[identifier][zone] = float(area)
    _require(groups.keys() == fragments.keys(), "allocations.fragment_id", sorted(groups), sorted(fragments))
    for identifier, group in groups.items():
        if mode != "split_by_area":
            _require(len(group) == 1, f"allocations.{identifier}.zone_id", len(group), 1)
        _area_close(f"allocations.{identifier}.area_m2", math.fsum(group.values()),
                    fragments[identifier].geometry.area, tolerance)
    return zones, groups


def _matrix(classes: list[str], areas: dict[tuple[str, str], list[float]]) -> dict:
    matrix = [[math.fsum(areas.get((source, target), ())) for target in classes] for source in classes]
    return {
        "areas_m2": matrix, "total_area_m2": math.fsum(value for row in matrix for value in row),
        "changed_area_m2": math.fsum(value for i, row in enumerate(matrix) for j, value in enumerate(row) if i != j),
        "unchanged_area_m2": math.fsum(row[i] for i, row in enumerate(matrix)),
    }


def build_transition_reports(
    overlay: GeoDataFrame, changes: GeoDataFrame, allocations: dict, change_report: dict, *, area_tolerance_m2: float,
) -> tuple[dict, dict]:
    """Aggregate actual retained geometry and its pre-merge zone contributions."""
    tolerance = area_tolerance_m2
    _tolerance(tolerance)
    area_crs = _area_crs(overlay).to_string()
    fragments = _fragments(overlay, tolerance)
    actual = _verified_changes(overlay, changes, change_report, tolerance)
    zone_ids, contributions = _validated_allocations(allocations, fragments, area_crs, tolerance)
    classes = sorted({category for fragment in fragments.values() for category in fragment.classes})
    global_areas = defaultdict(list)
    zone_areas = {zone: defaultdict(list) for zone in _zone_order(zone_ids)}
    for row in actual.values():
        pair = row["from_class"], row["to_class"]
        global_areas[pair].append(row["geometry"].area)
        for source in row["source_refs"]:
            for zone, value in contributions[source["fragment_id"]].items():
                zone_areas[zone][pair].append(value)
    basis = {"area_crs": area_crs, "area_unit": "square_meter", "classes": classes}
    matrix = {**basis, **_matrix(classes, global_areas)}
    zones = [{"zone_id": zone, **_matrix(classes, entries)} for zone, entries in zone_areas.items()]
    for i, row in enumerate(matrix["areas_m2"]):
        for j, cell in enumerate(row):
            _area_close(f"zone_statistics.areas_m2.{i}.{j}",
                        math.fsum(zone["areas_m2"][i][j] for zone in zones), cell, tolerance)
    totals = {key: math.fsum(zone[key] for zone in zones)
              for key in ("total_area_m2", "changed_area_m2", "unchanged_area_m2")}
    _area_close("zone_statistics.total_area_m2", totals["total_area_m2"], matrix["total_area_m2"], tolerance)
    return matrix, {**basis, **totals, "boundary_assignment": allocations["boundary_assignment"], "zones": zones}


def verify_transition_outputs(
    overlay: GeoDataFrame, changes: GeoDataFrame, allocations: dict, change_report: dict, transition_matrix: dict,
    zone_statistics: dict, *, area_tolerance_m2: float,
) -> dict:
    """Reopen-based check for the final matrices, raw fragment links and retained geometry."""
    matrix, zones = build_transition_reports(
        overlay, changes, allocations, change_report, area_tolerance_m2=area_tolerance_m2,
    )
    _compare("transition_matrix", transition_matrix, matrix, area_tolerance_m2)
    _compare("zone_statistics", zone_statistics, zones, area_tolerance_m2)
    polygon_area = math.fsum(changes.geometry.area)
    _area_close("transition_matrix.total_area_m2", transition_matrix["total_area_m2"], polygon_area, area_tolerance_m2)
    _area_close("zone_statistics.total_area_m2", zone_statistics["total_area_m2"], polygon_area, area_tolerance_m2)
    return {
        "polygon_area_m2": polygon_area, "matrix_area_m2": transition_matrix["total_area_m2"],
        "zone_area_m2": zone_statistics["total_area_m2"], "filtered_area_m2": change_report["filtered_area_m2"],
        "pre_filter_area_m2": change_report["pre_filter_area_m2"], "classes": matrix["classes"],
    }
