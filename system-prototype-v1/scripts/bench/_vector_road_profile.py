"""Build the separately versioned L road input from the accepted P13-01 recipe."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pyogrio
import yaml

from generate_vector import GPKG_TIMESTAMP, PROFILE, TileGrid, densify_roads, file_record, read_layers, write_package
from validate_benchmark_manifest import Vector, read_yaml


ROAD_PROFILE = "benchmarks/vector/road-l-utm50n-south.v1.yaml"
ROAD_PACKAGE = "road.gpkg"


def load_road_profile(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict) or set(value) != {
        "id", "version", "base_profile", "tier", "package", "origin_north_m",
    }:
        raise ValueError("Invalid L road position profile fields")
    if ((value["id"], value["version"], value["base_profile"], value["tier"], value["package"])
            != ("road-l-utm50n-south", 1, PROFILE, "L", ROAD_PACKAGE)
            or type(value["origin_north_m"]) is not int or value["origin_north_m"] <= 0):
        raise ValueError("Invalid L road position profile")
    return value


def generate_corrected_road(root: Path, base_dir: Path, output: Path, path: Path) -> dict:
    """Generate only the translated road package in a new directory."""
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"{output} already exists")
    correction = load_road_profile(path)
    correction_hash = file_record(path)["sha256"]
    base_path = base_dir / "manifest.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))
    if (base["version"], base["family"], base["tier"]) != (1, "vector", correction["tier"]):
        raise ValueError("L road correction requires a P13-01 L input manifest")
    base_hash = file_record(base_path)["sha256"]
    original = base["reference"]
    original_file = next(item for item in base["files"] if item["path"] == ROAD_PACKAGE)
    profile = Vector.model_validate(read_yaml(root, PROFILE))
    source_paths = (PROFILE, profile.road_template, profile.road_reference)
    source_hashes = {name: base["source_sha256"][name] for name in source_paths}
    original_east, original_north = original["tile_origin_xy_m"][ROAD_PACKAGE]
    northing = correction["origin_north_m"]
    if northing >= original_north:
        raise ValueError("L road correction must translate the grid south")
    grid = TileGrid(original["side_tiles"], original["tile_pitch_m"], (original_east, northing))
    layers = read_layers(root, profile.road_template)
    layers["roads"] = densify_roads(layers["roads"], original["road_vertices"])
    reference = {key: original[key] for key in (
        "tiles", "side_tiles", "tile_pitch_m", "tile_id_format", "source_feature_id_format",
        "road_vertices", "attribute_bytes", "logical_encoding", "road",
    )}
    for key in ("crs", "template_origin_xy_m", "layer_counts"):
        reference[key] = {ROAD_PACKAGE: original[key][ROAD_PACKAGE]}
    reference["tile_origin_xy_m"] = {ROAD_PACKAGE: grid.origin_xy_m}
    manifest = {
        "version": 1, "family": "vector", "tier": "L",
        "input_profile": f"{correction['id']}@{correction['version']}",
        "position_profile": correction, "position_profile_sha256": correction_hash,
        "base_input_manifest_sha256": base_hash, "base_road_file": original_file,
        "translation_xy_m": (0, northing - original_north),
        "source_sha256": {**source_hashes, ROAD_PROFILE: correction_hash}, "reference": reference,
    }
    with TemporaryDirectory(prefix=".vector-road-position-", dir=output.parent) as temporary:
        staging = Path(temporary)
        previous_timestamp = pyogrio.get_gdal_config_option("OGR_CURRENT_DATE")
        pyogrio.set_gdal_config_options({"OGR_CURRENT_DATE": GPKG_TIMESTAMP})
        try:
            reference["layers"] = {ROAD_PACKAGE: write_package(
                staging / ROAD_PACKAGE, layers, grid, crs=reference["crs"][ROAD_PACKAGE],
                template_origin=tuple(reference["template_origin_xy_m"][ROAD_PACKAGE]),
                expected_counts=reference["layer_counts"][ROAD_PACKAGE], attribute_bytes=reference["attribute_bytes"],
            )}
        finally:
            pyogrio.set_gdal_config_options({"OGR_CURRENT_DATE": previous_timestamp})
        if (any(file_record(root / name)["sha256"] != digest for name, digest in source_hashes.items())
                or file_record(base_path)["sha256"] != base_hash or file_record(path)["sha256"] != correction_hash
                or file_record(base_dir / ROAD_PACKAGE) != original_file):
            raise ValueError("A source or profile changed during L road position correction")
        manifest["files"] = [file_record(staging / ROAD_PACKAGE)]
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        output.mkdir()
        published = []
        try:
            for name in (ROAD_PACKAGE, "manifest.json"):
                target = output / name
                os.link(staging / name, target)
                published.append(target)
        except OSError:
            for target in published:
                target.unlink()
            output.rmdir()
            raise
    return manifest
