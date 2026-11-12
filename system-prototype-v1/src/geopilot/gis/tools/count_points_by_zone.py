"""Count retained point features by the zone that covers their original centroid."""

import csv
import json
from pathlib import Path

import pyogrio
from geopandas import GeoDataFrame
from pyproj import CRS
from shapely import STRtree
from shapely.geometry.base import BaseGeometry

from geopilot.data.errors import SourceReadError
from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.vector_reader import local_ogr_path, read_layer_metadata
from geopilot.domain.data_asset import VectorMetadata
from geopilot.gis.tools.tool_command import run_tool_command


_IDENTIFIER = "source_feature_id"
_POINT_TYPES = {"Point", "MultiPoint"}
_POLYGON_TYPES = {"Polygon", "MultiPolygon"}


def _read_features(
    reference: dict, role: str, allowed_types: set[str],
) -> tuple[CRS, list[tuple[str, BaseGeometry]]]:
    layer = reference.get("layer")
    try:
        path = local_ogr_path(reference["path"])
        if path.suffix.lower() == ".gpkg":
            layer, metadata = read_geopackage_metadata(path, layer)
        else:
            metadata = read_layer_metadata(path, layer)
        if not isinstance(metadata, VectorMetadata) or metadata.geometry_type.split()[0] not in allowed_types:
            raise ValueError(f"{role} requires a layer of type {', '.join(sorted(allowed_types))}")
        if metadata.crs is None:
            raise ValueError(f"{role} requires a known CRS")
        if _IDENTIFIER not in {field.name for field in metadata.fields}:
            raise ValueError(f"{role} requires the {_IDENTIFIER} field")
        frame = pyogrio.read_dataframe(path, layer=layer, columns=[_IDENTIFIER])
    except (SourceReadError, pyogrio.errors.DataLayerError) as error:
        raise ValueError(f"Cannot read {role}: {error}") from error

    crs = CRS.from_user_input(metadata.crs)
    if (
        not isinstance(frame, GeoDataFrame) or frame.crs is None
        or not frame.crs.equals(crs, ignore_axis_order=True) or len(frame) != metadata.feature_count
    ):
        raise ValueError(f"{role} layer differs from its inspected metadata")
    identifiers = frame[_IDENTIFIER]
    ids = identifiers.astype(str)
    if identifiers.isna().any() or ids.str.strip().eq("").any():
        raise ValueError(f"{role} requires nonempty {_IDENTIFIER} values")
    geometry = frame.geometry
    if not geometry.geom_type.isin(allowed_types).all() or geometry.is_empty.any() or not geometry.is_valid.all():
        raise ValueError(f"{role} contains invalid, empty, or unexpected geometry")
    return crs, list(zip(ids, geometry, strict=True))


def _point_locations(features: list[tuple[str, BaseGeometry]], role: str) -> dict[str, BaseGeometry]:
    locations = {}
    for identifier, point in features:
        # Normalize the order of a MultiPoint's components before comparing duplicates.
        location = point.normalize()
        previous = locations.setdefault(identifier, location)
        if not previous.equals_exact(location, tolerance=0):
            raise ValueError(f"{role} has conflicting locations for {_IDENTIFIER} {identifier!r}")
    return locations


def count_points_by_zone(request: dict) -> dict:
    parameters = request["parameters"]
    if (parameters["boundary_assignment"], parameters["boundary_tie"], parameters["statistic"]) != (
        "centroid", "unassigned", "count",
    ):
        raise ValueError("Point counting requires centroid assignment with ties unassigned")

    inputs = request["inputs"]
    targets_crs, targets = _read_features(inputs["targets"], "targets", _POINT_TYPES)
    originals_crs, originals = _read_features(inputs["original_targets"], "original_targets", _POINT_TYPES)
    zones_crs, zones = _read_features(inputs["zones"], "zones", _POLYGON_TYPES)
    for role, crs in (("original_targets", originals_crs), ("zones", zones_crs)):
        if not crs.equals(targets_crs, ignore_axis_order=True):
            raise ValueError(f"{role} CRS {crs.to_string()} does not match targets CRS {targets_crs.to_string()}")

    retained = _point_locations(targets, "targets")
    original_locations = _point_locations(originals, "original_targets")
    missing = retained.keys() - original_locations.keys()
    if missing:
        raise ValueError(f"original_targets is missing retained {_IDENTIFIER} {min(missing)!r}")
    zone_geometries = dict(zones)
    if len(zone_geometries) != len(zones):
        raise ValueError(f"zones requires unique {_IDENTIFIER} values")

    zone_ids = sorted(zone_geometries)
    zone_members = {identifier: [] for identifier in zone_ids}
    unassigned = []
    tree = STRtree([zone_geometries[identifier] for identifier in zone_ids])
    for identifier in sorted(retained):
        # covered_by is the inverse of polygon covers and includes the polygon boundary.
        matches = tree.query(original_locations[identifier].centroid, predicate="covered_by")
        if len(matches) == 1:
            zone_members[zone_ids[matches[0]]].append(identifier)
        else:
            unassigned.append(identifier)

    output = request["output_targets"]["statistics"]
    path = Path(output["path"])
    if path.suffix.lower() != ".csv" or output.get("layer") is not None:
        raise ValueError("statistics requires a .csv path without a layer")
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("zone_id", "assignment_status", "count", "source_feature_ids"))
        for zone_id, members in zone_members.items():
            writer.writerow((zone_id, "assigned", len(members), json.dumps(members, ensure_ascii=False)))
        writer.writerow(("", "unassigned", len(unassigned), json.dumps(unassigned, ensure_ascii=False)))

    print(f"Counted {len(retained)} retained target(s) across {len(zone_ids)} zone(s), {len(unassigned)} unassigned")
    return {"statistics": {"path": output["path"]}}


if __name__ == "__main__":
    raise SystemExit(run_tool_command(count_points_by_zone, description=__doc__))
