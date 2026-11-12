"""Check benchmark results against the independent, tiled P13-01 reference."""

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pyogrio
import shapely

from generate_vector import ID, main_road_capsule


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def tiles(reference: dict) -> list[str]:
    side = reference["side_tiles"]
    return [f"r{row:03d}c{col:03d}" for row in range(side) for col in range(side)]


def expected_ids(reference: dict, ids) -> Counter:
    counts = Counter(ids)
    return Counter({f"{tile}/{name}": count for tile in tiles(reference) for name, count in counts.items()})


def check_ids(frame, reference: dict, ids, label: str) -> None:
    observed = Counter(frame[ID])
    expected = expected_ids(reference, ids)
    require(observed == expected, f"{label}: missing={list(expected - observed)[:4]}, "
                                 f"unexpected={list(observed - expected)[:4]}")


def read_output(workdir: Path, output, reference: dict, package: str):
    frame = pyogrio.read_dataframe(workdir / output.path, layer=output.layer)
    require(frame.crs is not None and frame.crs.to_string() == reference["crs"][package],
            f"{output.path}: unexpected CRS {frame.crs}")
    require(bool(frame.geometry.is_valid.all() and not frame.geometry.is_empty.any()),
            f"{output.path}: invalid or empty geometry")
    return frame


def translate(geometries, tile_ids, reference: dict, package: str):
    coordinates, indexes = shapely.get_coordinates(geometries, return_index=True)
    pitch = reference["tile_pitch_m"]
    base = np.subtract(reference["tile_origin_xy_m"][package], reference["template_origin_xy_m"][package])
    offsets = np.array([(int(tile[5:8]), int(tile[1:4])) for tile in tile_ids]) * pitch + base
    return shapely.set_coordinates(np.array(geometries, dtype=object), coordinates + offsets[indexes])


def check_geometries(frame, geometries: dict, reference: dict, package: str, label: str) -> None:
    tile_ids, template_ids = zip(*(identifier.split("/") for identifier in frame[ID]), strict=True)
    expected = translate([geometries[identifier] for identifier in template_ids], tile_ids, reference, package)
    observed = frame.geometry.to_numpy()
    matches = shapely.equals(observed, expected)
    require(bool(matches.all()), f"{label}: differing geometry for {frame[ID][~matches].head(4).tolist()}")


def check_originals(frame, source, label: str) -> None:
    def rows(data):
        return Counter(zip(data[ID], shapely.to_wkb(shapely.normalize(data.geometry.to_numpy())), strict=True))

    require(rows(frame) == rows(source[source[ID].isin(frame[ID])]),
            f"{label}: original coordinates or row multiplicities differ")


def step_output(steps, key: str, role: str):
    return steps[key].result.outputs[role]


def check_t13(workdir, inputs, workload, reference, steps, outputs):
    package = workload.package
    layer = workload.layers["targets"]
    relation = workload.options["spatial_relation"]
    expected = reference["t13"]["relations"][relation][layer]
    selected = read_output(workdir, step_output(steps, "apply_spatial_relation", "targets"), reference, package)
    original = read_output(workdir, outputs["original_targets"], reference, package)
    clipped = read_output(workdir, outputs["clipped_targets"], reference, package)
    source = pyogrio.read_dataframe(inputs / package, layer=layer)
    for label, frame in (("T13 selected", selected), ("T13 original", original), ("T13 clipped", clipped)):
        check_ids(frame, reference, expected["per_tile_ids"], label)
    check_originals(selected, source, "T13 selected")
    check_originals(original, source, "T13 original")
    geometries = {key: shapely.from_wkt(value["wkt"]) for key, value in expected["per_tile_clipped"].items()}
    check_geometries(clipped, geometries, reference, package, "T13 clipped")
    length = float(clipped.loc[clipped.geometry.geom_type.isin(("LineString", "MultiLineString"))].length.sum())
    area = float(clipped.area.sum())
    require(math.isclose(length, expected["retained_line_length_m"], abs_tol=1e-6, rel_tol=0), "T13 length differs")
    require(math.isclose(area, expected["retained_area_m2"], abs_tol=1e-6, rel_tol=0), "T13 area differs")
    return {"selected": len(selected), "clipped": len(clipped), "retained_line_length_m": length, "retained_area_m2": area}


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def check_t14(workdir, inputs, workload, reference, steps, outputs):
    package = workload.package
    expected = reference["t14"]
    selected = read_output(workdir, step_output(steps, "apply_spatial_relation", "targets"), reference, package)
    original = read_output(workdir, outputs["original_targets"], reference, package)
    clipped = read_output(workdir, outputs["clipped_targets"], reference, package)
    source = pyogrio.read_dataframe(inputs / package, layer="targets")
    # The extra source row is a selected fragment that vanishes on clipping.
    require(len(selected) == expected["selected_rows"], "T14 selection count differs")
    check_originals(selected, source, "T14 selected")
    require(Counter(selected[ID]) == Counter(source[ID]), "T14 selected IDs differ")
    clip_ids = expected["clipped_rows_per_id_per_tile"]
    check_ids(clipped, reference, Counter(clip_ids).elements(), "T14 clipped")
    require(Counter(original[ID]) == Counter(clipped[ID]), "T14 original row multiplicities differ")
    check_originals(original, source, "T14 original")
    unique = clipped[[ID, "geometry"]].dissolve(by=ID).reset_index()
    geometries = {key: shapely.from_wkt(value["wkt"]) for key, value in expected["clipped_geometry_per_tile"].items()}
    check_geometries(unique, geometries, reference, package, "T14 clipped union by source ID")
    mode = workload.options["boundary_assignment"]
    allocation = expected["methods"][mode]
    detail = Counter()
    zones = defaultdict(list)
    unassigned = []
    for tile in tiles(reference):
        for row in allocation["details_per_tile"]:
            identifier = f"{tile}/{row[ID]}"
            zone = f"{tile}/{row['zone_id']}" if row["zone_id"] else ""
            detail[(identifier, zone, row["object_status"], row["area_m2"])] += 1
            if zone:
                zones[zone].append((identifier, row["area_m2"]))
            elif row["object_status"] == "unassigned":
                unassigned.append(identifier)
    rows = read_csv(workdir / outputs["allocation_details"].path)
    observed = Counter((row[ID], row["zone_id"], row["object_status"], float(row["area_m2"])) for row in rows)
    require(observed == detail, f"T14 allocations differ for {list(detail - observed)[:4]}")
    stats = json.loads((workdir / outputs["zone_statistics"].path).read_text(encoding="utf-8"))
    require(stats["counts"] == allocation["counts"] and stats["areas_m2"] == allocation["areas_m2"],
            "T14 counts or areas differ")
    require(stats["boundary_assignment"] == mode and stats["boundary_tie"] == "unassigned"
            and stats["area_crs"] == "EPSG:6933" and stats["area_unit"] == "square_meter",
            "T14 statistics options differ")
    expected_zones = {
        zone: (len(values), sum(area for _, area in values), sorted(identifier for identifier, _ in values))
        for zone, values in zones.items()
    }
    observed_zones = {row["zone_id"]: (row["distinct_count"], row["area_m2"], sorted(row["source_feature_ids"]))
                      for row in stats["zones"]}
    require(observed_zones == expected_zones and len(stats["zones"]) == len(expected_zones)
            and sorted(stats["unassigned_source_feature_ids"]) == sorted(unassigned),
            "T14 per-zone or unassigned IDs differ")
    return {"selected": len(selected), "clipped": len(clipped), "counts": stats["counts"], "areas_m2": stats["areas_m2"],
            "allocation_rows": len(rows), "zones": len(zones)}


def check_buffers(frame, reference: dict):
    road = reference["road"]
    main = shapely.from_wkt(road["main_road_buffer"]["reference_wkt"])
    edge = shapely.from_wkt(main_road_capsule({
        **road, "main_road_buffer": {"endpoints_xy_m": [[500150, 4402300], [501850, 4402300]]},
    }))
    expected = main.union(edge)
    observed = shapely.get_parts(frame.geometry.to_numpy())
    # Disconnected output components belong to a tile by their centers. Union
    # within a tile first so a single/multipart layer encoding cannot affect comparison.
    origin = reference["tile_origin_xy_m"]["road.gpkg"]
    pitch = reference["tile_pitch_m"]
    centers = shapely.get_coordinates(shapely.centroid(observed))
    indices = np.floor((centers - origin) / pitch).astype(int)
    groups = defaultdict(list)
    for (col, row), shape in zip(indices, observed, strict=True):
        groups[f"r{row:03d}c{col:03d}"].append(shape)
    tile_ids = tiles(reference)
    require(set(groups) == set(tile_ids), "Road buffer tile identities differ")
    observations = [shapely.union_all(groups[tile]) for tile in tile_ids]
    target = translate([expected] * len(tile_ids), tile_ids, reference, "road.gpkg")
    tolerance = road["main_road_buffer"]["absolute_tolerances"]
    bounds_error = float(np.max(np.abs(shapely.bounds(observations) - shapely.bounds(target))))
    area_error = float(np.max(np.abs(shapely.area(observations) - shapely.area(target))))
    boundary_error = float(np.max(shapely.hausdorff_distance(shapely.boundary(observations), shapely.boundary(target))))
    require(bounds_error <= tolerance["bounds_m"] and area_error <= tolerance["area_m2"]
            and boundary_error <= tolerance["boundary_hausdorff_m"],
            f"Road buffer geometry differs: bounds {bounds_error}, area {area_error}, Hausdorff {boundary_error}")
    return {"area_m2": float(shapely.area(observations).sum()), "max_bounds_error_m": bounds_error,
            "max_area_error_m2": area_error, "max_boundary_hausdorff_m": boundary_error}


def check_road(workdir, inputs, workload, reference, steps, outputs):
    package = workload.package
    road = reference["road"]
    expected = road["relations"][workload.options["spatial_relation"]]
    roads = read_output(workdir, step_output(steps, "select_roads_from_expanded_extent", "roads"), reference, package)
    candidates = read_output(workdir, step_output(steps, "apply_spatial_relation", "targets"), reference, package)
    targets = read_output(workdir, outputs["selected_targets"], reference, package)
    check_ids(roads, reference, road["candidate_road_ids"], "Road candidates")
    check_ids(candidates, reference, expected["before_clip_target_ids"], "Road targets before clipping")
    check_ids(targets, reference, expected["clipped_target_ids"], "Road targets after clipping")
    source = pyogrio.read_dataframe(inputs / package, layer="targets")
    check_originals(candidates, source, "Road targets before clipping")
    check_originals(targets, source, "Road targets after clipping")
    zone_targets = {
        f"{tile}/{zone}": sorted(f"{tile}/{name}" for name in identifiers)
        for tile in tiles(reference) for zone, identifiers in expected["zone_target_ids"].items()
    }
    zone_targets[""] = sorted(expected_ids(reference, expected["unassigned_target_ids"]))
    rows = read_csv(workdir / outputs["zone_statistics"].path)
    observed = {row["zone_id"]: sorted(json.loads(row["source_feature_ids"])) for row in rows}
    require(len(observed) == len(rows) and observed == zone_targets, "Road zone assignment differs")
    counts = Counter()
    for row in rows:
        count = int(row["count"])
        require(count == len(observed[row["zone_id"]]), "Road zone count differs from its ID set")
        counts[row["zone_id"].partition("/")[2] or "unassigned"] += count
    counts["total"] = sum(counts.values())
    require(dict(counts) == expected["total_counts"], "Road total counts differ")
    buffer = read_output(workdir, outputs["buffer_extent"], reference, package)
    return {"road_candidates": len(roads), "targets_before_clip": len(candidates), "targets_after_clip": len(targets),
            "counts": dict(counts), "buffer": check_buffers(buffer, reference)}
