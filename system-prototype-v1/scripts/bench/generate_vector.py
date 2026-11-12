"""Generate a new tiled GeoPackage benchmark dataset and its independent references."""

import argparse
import hashlib
import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import geopandas as gpd
import numpy as np
import pyogrio
import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from yaml import YAMLError

from validate_benchmark_manifest import ROOT, TIERS, Vector, exact, project_file, read_yaml, vector_sizes


PROFILE = "benchmarks/datasets/vector.yaml"
ID = "source_feature_id"
TEMPLATE_ORIGIN = (500_000, 4_400_000)
T14_ORIGIN = (1_000_000, 2_000_000)
T14_CRS = "EPSG:6933"
T13_TEST = "tests/methods/test_full_geometry_before_clip.py"
T14_TEST = "tests/gis/test_source_feature_identity.py"
TILES_PER_BATCH = 1024
GPKG_TIMESTAMP = "2000-01-01T00:00:00.000Z"

# Hand-specified exceptions to full-geometry retention from the V1 T13 reference.
T13_CHANGED_CLIPS = {
    "line-crosses-hole": "MULTILINESTRING ((500030 4400050, 500040 4400050), (500060 4400050, 500070 4400050))",
    "line-crosses-shell": "LINESTRING (500000 4400030, 500100 4400030)",
    "line-touch-corner": "POINT (500000 4400000)",
    "polygon-crosses-hole": "POLYGON ((500035 4400045, 500040 4400045, 500040 4400055, 500035 4400055, 500035 4400045))",
    "polygon-crosses-shell": "POLYGON ((500000 4400030, 500015 4400030, 500015 4400050, 500000 4400050, 500000 4400030))",
    "polygon-encloses-hole": (
        "POLYGON ((500030 4400030, 500070 4400030, 500070 4400070, 500030 4400070, 500030 4400030), "
        "(500040 4400040, 500040 4400060, 500060 4400060, 500060 4400040, 500040 4400040))"
    ),
    "polygon-hole-edge-outside": "LINESTRING (500040 4400045, 500040 4400055)",
    "polygon-shell-edge-outside": "LINESTRING (500000 4400070, 500000 4400080)",
    "polygon-shell-vertex-outside": "POINT (500100 4400100)",
}


@dataclass(frozen=True)
class TileGrid:
    side_tiles: int
    pitch_m: int
    origin_xy_m: tuple[int, int]

    @property
    def tile_count(self) -> int:
        return self.side_tiles ** 2

    def batches(self, template_origin: tuple[int, int]):
        for start in range(0, self.tile_count, TILES_PER_BATCH):
            indices = np.arange(start, min(start + TILES_PER_BATCH, self.tile_count))
            rows, columns = np.divmod(indices, self.side_tiles)
            ids = [f"r{row:03d}c{col:03d}" for row, col in zip(rows, columns, strict=True)]
            offsets = np.column_stack((columns, rows)) * self.pitch_m
            offsets += np.asarray(self.origin_xy_m) - np.asarray(template_origin)
            yield ids, offsets


def read_document(root: Path, path: str) -> dict:
    return json.loads(project_file(root, path).read_bytes())


def read_layers(root: Path, path: str) -> dict:
    return {
        layer: [(row[ID], shapely.from_wkt(row["wkt"])) for row in rows]
        for layer, rows in read_document(root, path)["layers"].items()
    }


def densify_roads(rows: list, vertices: int) -> list:
    densified = []
    for identifier, geometry in rows:
        if geometry.geom_type != "LineString" or len(geometry.coords) != 2:
            raise ValueError(f"road {identifier!r} must be a straight two-vertex LineString")
        start, end = geometry.coords
        densified.append((identifier, LineString(np.linspace(start, end, vertices))))
    return densified


def t14_box(left: int, bottom: int, right: int, top: int) -> Polygon:
    x, y = T14_ORIGIN
    return box(x + left, y + bottom, x + right, y + top)


def t14_layers() -> dict:
    # The V1 fixture uses three separated bands and repeats the crossing source.
    crossing = t14_box(60, 10, 160, 40)
    return {
        "targets": [
            ("crossing", crossing), ("crossing", crossing),
            ("centroid-tie", t14_box(80, 10, 120, 20)),
            ("west-only", t14_box(10, 32, 30, 42)),
            ("partly-uncovered", t14_box(190, 70, 220, 80)),
            ("outside-zones", t14_box(220, 80, 230, 90)),
            ("boundary-contact", t14_box(120, 5, 130, 15)),
            ("drop-outside", t14_box(150, 40, 170, 45)),
        ],
        "relation_area": [("relation", t14_box(0, 0, 240, 100))],
        "analysis_extent": [
            ("first-band", t14_box(0, 0, 120, 20)),
            ("second-band", t14_box(0, 30, 120, 50)),
            ("outer-band", t14_box(190, 60, 240, 100)),
        ],
        "zones": [("west", t14_box(0, 0, 100, 100)), ("east", t14_box(100, 0, 200, 100))],
    }


def main_road_capsule(road_reference: dict) -> str:
    """Calculate the reference from the analytic straight-segment capsule."""
    segment = road_reference["main_road_buffer"]
    start, end = segment["endpoints_xy_m"]
    quarter_segments = road_reference["buffer_options"]["segments_per_quarter"]
    radius = road_reference["distance_m"]
    direction = math.atan2(end[1] - start[1], end[0] - start[0])
    boundary = []
    for (x, y), initial in ((end, direction - math.pi / 2), (start, direction + math.pi / 2)):
        for index in range(2 * quarter_segments + 1):
            angle = initial + index * math.pi / (2 * quarter_segments)
            boundary.append((x + radius * math.cos(angle), y + radius * math.sin(angle)))
    return shapely.to_wkt(Polygon(boundary), rounding_precision=-1)


def geometry_reference(geometry) -> dict:
    return {
        "wkt": shapely.to_wkt(geometry, rounding_precision=-1),
        "geometry_type": geometry.geom_type,
        "length_m": geometry.length,
        "area_m2": geometry.area,
    }


def relation_reference(reference: dict, template: dict, tile_count: int) -> dict:
    # Use the checked-in ID truth table and hand-written output coordinates.
    changes = {identifier: shapely.from_wkt(wkt) for identifier, wkt in T13_CHANGED_CLIPS.items()}
    results = {}
    for predicate, layers in reference["relations"].items():
        results[predicate] = {}
        for layer, ids in layers.items():
            source = dict(template[layer])
            per_tile = {}
            for identifier in ids:
                geometry = changes[identifier] if predicate == "intersects" and identifier in changes else source[identifier]
                per_tile[identifier] = geometry_reference(geometry)
            results[predicate][layer] = {
                "per_tile_ids": ids,
                "per_tile_clipped": per_tile,
                "count": len(ids) * tile_count,
                "retained_line_length_m": math.fsum(
                    record["length_m"] for record in per_tile.values()
                    if record["geometry_type"] in ("LineString", "MultiLineString")
                ) * tile_count,
                "retained_area_m2": math.fsum(record["area_m2"] for record in per_tile.values()) * tile_count,
            }
    return {"area": reference["reference"], "boundary_only_ids": reference["boundary_only_ids"], "relations": results}


def t14_reference(tile_count: int) -> dict:
    # These allocations are the manual V1 reference, evaluated before any GIS run.
    common = [
        ("boundary-contact", None, "unassigned", 0),
        ("west-only", "west", "assigned", 200),
    ]
    allocations = {
        "split_by_area": [
            ("centroid-tie", "east", "assigned", 200), ("centroid-tie", "west", "assigned", 200),
            ("crossing", "east", "assigned", 400), ("crossing", "west", "assigned", 800),
            ("outside-zones", None, "unassigned", 100), ("partly-uncovered", "east", "assigned", 100),
            ("partly-uncovered", None, "assigned", 200),
        ],
        "centroid": [
            ("centroid-tie", None, "unassigned", 400), ("crossing", "east", "assigned", 1200),
            ("outside-zones", None, "unassigned", 100), ("partly-uncovered", None, "unassigned", 300),
        ],
        "largest_overlap": [
            ("centroid-tie", None, "unassigned", 400), ("crossing", "east", "assigned", 1200),
            ("outside-zones", None, "unassigned", 100), ("partly-uncovered", "east", "assigned", 300),
        ],
    }
    x, y = T14_ORIGIN
    clips = {
        "crossing": MultiPolygon([t14_box(60, 10, 120, 20), t14_box(60, 30, 120, 40)]),
        "centroid-tie": t14_box(80, 10, 120, 20),
        "west-only": t14_box(10, 32, 30, 42),
        "partly-uncovered": t14_box(190, 70, 220, 80),
        "outside-zones": t14_box(220, 80, 230, 90),
        "boundary-contact": LineString([(x + 120, y + 5), (x + 120, y + 15)]),
    }
    methods = {}
    for method, rows in allocations.items():
        details = sorted(common + rows, key=lambda row: (row[0], row[1] is None, row[1] or ""))
        assigned_ids = {identifier for identifier, zone, _, _ in details if zone is not None}
        methods[method] = {
            "details_per_tile": [
                {ID: identifier, "zone_id": zone, "object_status": status, "area_m2": area}
                for identifier, zone, status, area in details
            ],
            "counts": {
                "total": 6 * tile_count, "assigned": len(assigned_ids) * tile_count,
                "unassigned": (6 - len(assigned_ids)) * tile_count,
            },
            "areas_m2": {
                "total": 2200 * tile_count, "uncovered": 300 * tile_count,
                "assigned": sum(row[3] for row in details if row[1] is not None) * tile_count,
                "unassigned": sum(row[3] for row in details if row[1] is None) * tile_count,
            },
        }
    return {
        "selected_rows": 8 * tile_count, "clipped_rows": 7 * tile_count,
        "clipped_rows_per_id_per_tile": {identifier: 2 if identifier == "crossing" else 1 for identifier in clips},
        "clipped_geometry_per_tile": {identifier: geometry_reference(geometry) for identifier, geometry in clips.items()},
        "methods": methods,
    }


def write_package(
    path: Path, layers: dict, grid: TileGrid, *, crs: str, template_origin: tuple[int, int],
    expected_counts: dict, attribute_bytes: int,
) -> dict:
    summary = {}
    payload = ("0123456789abcdef" * ((attribute_bytes + 15) // 16))[:attribute_bytes]
    for layer, rows in layers.items():
        template_ids, geometries = zip(*sorted(rows, key=lambda row: row[0]), strict=True)
        template_counts = Counter(template_ids)
        digest = hashlib.sha256()
        for batch_number, (tiles, offsets) in enumerate(grid.batches(template_origin)):
            count = len(template_ids)
            tile_ids = np.repeat(tiles, count)
            original_ids = np.tile(template_ids, len(tiles))
            ids = [f"{tile}/{identifier}" for tile, identifier in zip(tile_ids, original_ids, strict=True)]
            shapes = np.tile(geometries, len(tiles))
            coords, shape_indices = shapely.get_coordinates(shapes, return_index=True)
            coords += offsets[shape_indices // count]
            shapes = shapely.set_coordinates(shapes, coords)
            frame = gpd.GeoDataFrame(
                {ID: ids, "tile_id": tile_ids, "template_id": original_ids, "bench_payload": payload},
                geometry=shapes, crs=crs,
            )
            for identifier, tile, template, wkb in zip(
                ids, tile_ids, original_ids, shapely.to_wkb(shapes, byte_order=1, hex=True), strict=True,
            ):
                canonical = json.dumps([identifier, tile, template, payload, wkb], separators=(",", ":"))
                digest.update(canonical.encode("ascii") + b"\n")
            pyogrio.write_dataframe(frame, path, layer=layer, driver="GPKG", append=batch_number != 0)
        info = pyogrio.read_info(path, layer=layer, force_feature_count=True)
        if info["features"] != expected_counts[layer] or info["crs"] != crs:
            raise ValueError(f"{path.name}:{layer} did not reopen with the expected count and CRS")
        summary[layer] = {
            "features": info["features"],
            "unique_source_ids": len(template_counts) * grid.tile_count,
            "per_tile_input_id_counts": dict(template_counts),
            "geometry_type": info["geometry_type"],
            "bounds_xy_m": list(info["total_bounds"]),
            "logical_sha256": digest.hexdigest(),
        }
    return summary


def file_record(path: Path) -> dict:
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    return {"path": path.name, "bytes": path.stat().st_size, "sha256": digest}


def generate(root: Path, tier: str, output: Path) -> dict:
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"{output} already exists")
    profile_digest = file_record(project_file(root, PROFILE))["sha256"]
    profile = Vector.model_validate(read_yaml(root, PROFILE))
    exact(profile.tiers, TIERS, "vector tiers")
    sources = (profile.road_template, profile.road_reference, profile.relations_template,
               profile.relations_reference, T13_TEST, T14_TEST)
    input_hashes = {name: file_record(project_file(root, name))["sha256"] for name in sources}
    input_hashes[PROFILE] = profile_digest
    counts = vector_sizes(root, profile)[tier]
    layout = profile.tiers[tier]
    grid = TileGrid(layout.side_tiles, profile.tile_pitch_m, (profile.origin_east_m, profile.origin_north_m))
    equal_area_grid = TileGrid(layout.side_tiles, profile.tile_pitch_m, T14_ORIGIN)
    roads = read_layers(root, profile.road_template)
    roads["roads"] = densify_roads(roads["roads"], layout.road_vertices)
    relations = read_layers(root, profile.relations_template)
    road_reference = read_document(root, profile.road_reference)
    relations_reference = read_document(root, profile.relations_reference)
    road_reference.pop("input_file")
    road_reference["main_road_buffer"]["reference_wkt"] = main_road_capsule(road_reference)
    for result in road_reference["relations"].values():
        result["total_counts"] = {key: value * grid.tile_count for key, value in result["counts"].items()}

    reference = {
        "crs": {"road.gpkg": profile.crs, "relations.gpkg": profile.crs, "t14.gpkg": T14_CRS},
        "tiles": grid.tile_count,
        "side_tiles": grid.side_tiles,
        "tile_pitch_m": grid.pitch_m,
        "tile_id_format": "r{row:03d}c{col:03d}",
        "source_feature_id_format": "{tile_id}/{template_id}",
        "tile_origin_xy_m": {"road.gpkg": grid.origin_xy_m, "relations.gpkg": grid.origin_xy_m, "t14.gpkg": T14_ORIGIN},
        "template_origin_xy_m": {"road.gpkg": TEMPLATE_ORIGIN, "relations.gpkg": TEMPLATE_ORIGIN, "t14.gpkg": T14_ORIGIN},
        "road_vertices": layout.road_vertices,
        "attribute_bytes": layout.attribute_bytes,
        "layer_counts": {
            "road.gpkg": counts["roads"], "relations.gpkg": counts["relations"],
            "t14.gpkg": {layer: size * grid.tile_count for layer, size in
                         (("targets", 8), ("relation_area", 1), ("analysis_extent", 3), ("zones", 2))},
        },
        "logical_encoding": "ascending source_feature_id; UTF-8 compact JSON lines [source_feature_id,tile_id,template_id,bench_payload,little-endian WKB hex]",
        "road": road_reference,
        "t13": relation_reference(relations_reference, relations, grid.tile_count),
        "t14": t14_reference(grid.tile_count),
    }
    manifest = {"version": 1, "family": "vector", "tier": tier, "source_sha256": input_hashes, "reference": reference}
    with TemporaryDirectory(prefix=".vector-benchmark-", dir=output.parent) as temporary:
        staging = Path(temporary)
        previous_timestamp = pyogrio.get_gdal_config_option("OGR_CURRENT_DATE")
        pyogrio.set_gdal_config_options({"OGR_CURRENT_DATE": GPKG_TIMESTAMP})
        try:
            reference["layers"] = {
                name: write_package(
                    staging / name, layers, tile_grid, crs=reference["crs"][name], template_origin=origin,
                    expected_counts=reference["layer_counts"][name], attribute_bytes=layout.attribute_bytes,
                )
                for name, layers, tile_grid, origin in (
                    ("road.gpkg", roads, grid, TEMPLATE_ORIGIN),
                    ("relations.gpkg", relations, grid, TEMPLATE_ORIGIN),
                    ("t14.gpkg", t14_layers(), equal_area_grid, T14_ORIGIN),
                )
            }
        finally:
            pyogrio.set_gdal_config_options({"OGR_CURRENT_DATE": previous_timestamp})
        if any(file_record(project_file(root, name))["sha256"] != digest for name, digest in input_hashes.items()):
            raise ValueError("a benchmark input changed while the generator was running")
        files = sorted(reference["crs"])
        manifest["files"] = [file_record(staging / name) for name in files]
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        output.mkdir()
        published = []
        try:
            for name in (*files, "manifest.json"):
                os.link(staging / name, output / name)
                published.append(name)
        except OSError:
            for name in published:
                (output / name).unlink()
            output.rmdir()
            raise
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", required=True, choices=TIERS)
    parser.add_argument("--output", required=True, type=Path, help="new directory in an existing parent directory")
    parser.add_argument("--root", type=Path, default=ROOT, help="prototype root holding the pinned benchmark inputs")
    args = parser.parse_args()
    try:
        generate(args.root.resolve(), args.tier, args.output)
    except (OSError, ValueError, YAMLError, pyogrio.errors.DataSourceError, pyogrio.errors.DataLayerError) as exc:
        parser.error(str(exc))
    print(f"Generated vector tier {args.tier}: {args.output / 'manifest.json'}")


if __name__ == "__main__":
    main()
