"""Validate P12 benchmark inputs and optionally regenerate tiny GIS fixtures."""

import argparse
import hashlib
import json
import runpy
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt
from yaml.constructor import ConstructorError


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "benchmarks/manifest.yaml"
TIERS = ("S", "M", "L")
FAMILIES = ("vector", "raster", "remote_sensing")
METHODS = {
    "vector": ("road-buffer-statistics", 2, 7),
    "raster": ("dem-terrain-analysis", 1, 7),
    "remote_sensing": ("remote-sensing-index-change", 1, 8),
}
METRICS = {
    "wall_ns", "cpu_ns", "peak_rss_bytes", "disk_read_bytes", "disk_write_bytes",
    "peak_temp_bytes", "input_bytes", "output_bytes",
}


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Measurement(Record):
    repeats_per_cache_state: Annotated[int, Field(ge=3)]
    warmup_runs: PositiveInt
    cache_states: list[str]
    statistics: list[str]
    metrics: list[str]


class Profiles(Record):
    vector: str
    raster: str
    remote_sensing: str
    method: str


class Manifest(Record):
    version: Literal[1]
    protocol: str
    measurement: Measurement
    profiles: Profiles


class VectorTier(Record):
    side_tiles: PositiveInt
    road_vertices: Annotated[int, Field(ge=2, le=1025)]
    attribute_bytes: PositiveInt


class Vector(Record):
    family: Literal["vector"]
    format: Literal["GPKG"]
    crs: Literal["EPSG:32650"]
    tile_pitch_m: Annotated[int, Field(ge=4000)]
    origin_east_m: PositiveInt
    origin_north_m: PositiveInt
    road_template: str
    road_reference: str
    relations_template: str
    relations_reference: str
    tiers: dict[str, VectorTier]


class RasterTier(Record):
    side_pixels: PositiveInt


class Raster(Record):
    family: Literal["raster"]
    format: Literal["GTiff"]
    crs: Literal["EPSG:32650"]
    origin_east_m: PositiveInt
    origin_north_m: PositiveInt
    pixel_m: Literal[10]
    bands: Literal[1]
    dtype: Literal["float32"]
    nodata: Literal[-9999]
    halo_pixels: Literal[1]
    block_pixels: Literal[256]
    nodata_period_pixels: Annotated[int, Field(ge=4)]
    tiers: dict[str, RasterTier]


class RemoteSensing(Record):
    family: Literal["remote_sensing"]
    format: Literal["GTiff"]
    crs: Literal["EPSG:32632"]
    origin_east_m: PositiveInt
    origin_north_m: PositiveInt
    landsat_pixel_m: Literal[30]
    sentinel_pixel_m: Literal[10]
    scl_pixel_m: Literal[20]
    offset_m: Annotated[int, Field(ge=0, lt=10)]
    phase_columns: Literal[6]
    block_pixels: Literal[256]
    dtype: Literal["uint16"]
    landsat_config: str
    sentinel_config: str
    tiers: dict[str, RasterTier]


class MethodWorkload(Record):
    method_path: str
    method_id: str
    version: PositiveInt
    expected_steps: PositiveInt


class Method(Record):
    family: Literal["method"]
    workloads: dict[str, MethodWorkload]


class UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = super().construct_mapping(node, deep)
        if len(mapping) != len(node.value):
            raise ConstructorError(None, None, "duplicate or merged YAML mapping key", node.start_mark)
        return mapping


def project_file(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or "\\" in name:
        raise ValueError(f"unsafe project-relative path: {name!r}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"project file is absent or escapes root: {name}")
    return path


def read_yaml(root: Path, name: str):
    return yaml.load(project_file(root, name).read_text(encoding="utf-8"), Loader=UniqueLoader)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def exact(values, expected, label: str) -> None:
    require(len(values) == len(expected) and set(values) == set(expected), f"{label}: expected {sorted(expected)}")


def growing(tiers: dict, field: str, family: str) -> None:
    values = [getattr(tiers[tier], field) for tier in TIERS]
    require(values[0] < values[1] < values[2], f"{family}.{field}: S < M < L required")


def footprint(west: int, south: int, east: int, north: int, family: str) -> None:
    require(
        166000 <= west < east <= 834000 and 0 <= south < north <= 9320000,
        f"{family}: grid exceeds supported synthetic UTM envelope",
    )


def vector_sizes(root: Path, profile: Vector) -> dict:
    for key in ("side_tiles", "road_vertices", "attribute_bytes"):
        growing(profile.tiers, key, "vector")
    references = {}
    for label, source, expected in (
        ("roads", profile.road_template, profile.road_reference),
        ("relations", profile.relations_template, profile.relations_reference),
    ):
        template = json.loads(project_file(root, source).read_bytes())
        reference = json.loads(project_file(root, expected).read_bytes())
        require(template.get("crs") == profile.crs and reference.get("crs") == profile.crs,
                f"vector {label}: template and reference CRS must match")
        layers = template.get("layers")
        require(isinstance(layers, dict) and bool(layers), f"vector {label}: empty template layers")
        expected_ids = reference.get("input_feature_ids")
        require(isinstance(expected_ids, dict), f"vector {label}: missing independent feature IDs")
        exact(layers, expected_ids, f"vector {label} layers")
        references[label] = {}
        for name, features in layers.items():
            require(
                isinstance(features, list) and bool(features)
                and all(
                    isinstance(row, dict) and isinstance(row.get("source_feature_id"), str)
                    and isinstance(row.get("wkt"), str) and bool(row["wkt"])
                    for row in features
                ),
                f"vector {label}.{name}: invalid template features",
            )
            ids = [row["source_feature_id"] for row in features]
            require(sorted(ids) == sorted(expected_ids[name]) and len(ids) == len(set(ids)),
                    f"vector {label}.{name}: template IDs differ from independent reference")
            references[label][name] = len(ids)
    result = {}
    for tier, layout in profile.tiers.items():
        n = layout.side_tiles
        stride = (n - 1) * profile.tile_pitch_m
        east, north = profile.origin_east_m, profile.origin_north_m
        footprint(east - 10, north, east + stride + 2000, north + stride + 3075, f"vector.{tier}")
        result[tier] = {
            "tiles": n * n,
            "roads": {layer: count * n * n for layer, count in references["roads"].items()},
            "relations": {layer: count * n * n for layer, count in references["relations"].items()},
        }
    return result


def raster_sizes(profile: Raster) -> dict:
    growing(profile.tiers, "side_pixels", "raster")
    period = profile.nodata_period_pixels
    result = {}
    for tier, grid in profile.tiers.items():
        n = grid.side_pixels
        require(n % profile.block_pixels == 0 and n % period == 0, f"raster.{tier}: grid must fit block and NoData periods")
        require(3 <= period // 2 <= period - 3, "raster: NoData centers must leave space for full Horn neighborhoods")
        east, north = profile.origin_east_m, profile.origin_north_m
        footprint(east, north - n * profile.pixel_m, east + n * profile.pixel_m, north, f"raster.{tier}")
        holes = (n // period) ** 2
        result[tier] = {
            "pixels": n * n,
            "input_nodata_pixels": holes,
            "valid_terrain_pixels": (n - 2) ** 2 - 9 * holes,
            "raw_input_bytes": 4 * n * n,
        }
    return result


def remote_sizes(root: Path, profile: RemoteSensing) -> dict:
    growing(profile.tiers, "side_pixels", "remote_sensing")
    for config, sensor, product, edition in (
        (profile.landsat_config, "landsat-8-oli-tirs", "L2SP", "02"),
        (profile.sentinel_config, "sentinel-2-msi", "L2A", "05.11"),
    ):
        declared = read_yaml(root, config)
        require(
            all(str(declared.get(key)) == value for key, value in (
                ("sensor", sensor), ("product_level", product), ("processing_version", edition),
            )),
            f"remote_sensing: unexpected product key in {config}",
        )
    result = {}
    for tier, grid in profile.tiers.items():
        n = grid.side_pixels
        require(n % profile.phase_columns == 0, f"remote_sensing.{tier}: grid must fit complete phase cycles")
        m = n * (profile.landsat_pixel_m // profile.sentinel_pixel_m) + 2
        east, north, offset = profile.origin_east_m, profile.origin_north_m, profile.offset_m
        extent = m * profile.sentinel_pixel_m
        footprint(east - offset, north + offset - extent, east - offset + extent, north + offset,
                  f"remote_sensing.{tier}")
        result[tier] = {
            "landsat_pixels": n * n,
            "sentinel_pixels": m * m,
            "native_scl_pixels": (m // 2) ** 2,
            "expected_pixels_per_phase": n * n // profile.phase_columns,
            "common_valid_pixels": n * n // 2,
            "raw_input_bytes": 2 * (4 * n * n + 3 * m * m),
        }
    return result


def audit(root: Path, manifest_path: str) -> dict:
    manifest = Manifest.model_validate(read_yaml(root, manifest_path))
    project_file(root, manifest.protocol)
    policy = manifest.measurement
    exact(policy.cache_states, {"process_cold", "warm"}, "measurement.cache_states")
    exact(policy.statistics, {"median", "p95_nearest_rank"}, "measurement.statistics")
    exact(policy.metrics, METRICS, "measurement.metrics")
    paths = manifest.profiles
    vector = Vector.model_validate(read_yaml(root, paths.vector))
    raster = Raster.model_validate(read_yaml(root, paths.raster))
    remote = RemoteSensing.model_validate(read_yaml(root, paths.remote_sensing))
    method = Method.model_validate(read_yaml(root, paths.method))
    for profile in (vector, raster, remote):
        exact(profile.tiers, TIERS, f"{profile.family}.tiers")
    exact(method.workloads, FAMILIES, "method.workloads")
    for family, workload in method.workloads.items():
        require((workload.method_id, workload.version, workload.expected_steps) == METHODS[family],
                f"method.{family}: benchmark identity, version or step count differs")
        declared = read_yaml(root, workload.method_path)
        steps = declared.get("steps")
        require(
            declared.get("id") == workload.method_id
            and declared.get("version") == workload.version
            and isinstance(steps, list) and len(steps) == workload.expected_steps,
            f"method.{family}: configured method identity, version or step count differs",
        )
    return {
        "status": "PASS",
        "manifest": manifest_path,
        "tiers": {
            "vector": vector_sizes(root, vector),
            "raster": raster_sizes(raster),
            "remote_sensing": remote_sizes(root, remote),
        },
        "method_workloads": {family: workload.model_dump() for family, workload in method.workloads.items()},
    }


def fixture_probe(root: Path) -> dict:
    import pyogrio
    import rasterio

    fixtures = (
        ("tests/fixtures/vector/build_fixtures.py", "road.gpkg", {"analysis_extent": 1, "roads": 3, "targets": 9, "zones": 2}),
        ("tests/fixtures/vector/t13/build_fixture.py", "relations.gpkg", None),
    )
    reopened = {}
    with TemporaryDirectory(prefix="geopilot-benchmark-probe-") as temporary:
        directory = Path(temporary)
        for script, filename, expected in fixtures:
            builder = runpy.run_path(str(project_file(root, script)))
            path = directory / filename
            builder["build_geopackage"](path)
            actual = {
                name: pyogrio.read_info(path, layer=name)
                for name, _ in pyogrio.list_layers(path)
            }
            if expected is None:
                source = json.loads(project_file(root, "tests/fixtures/vector/t13/source.json").read_bytes())
                expected = {name: len(records) for name, records in source["layers"].items()}
            require({key: value["features"] for key, value in actual.items()} == expected
                    and all(info["crs"] == "EPSG:32650" for info in actual.values()),
                    f"fixture probe: mismatching vector metadata for {filename}")
            reopened[filename] = {"crs": "EPSG:32650", "layers": expected}

        raster = runpy.run_path(str(project_file(root, "tests/fixtures/raster/generate.py")))
        raster["generate"](directory)
        with rasterio.open(directory / "dem-plane.tif") as dem:
            require(
                (dem.shape, dem.count, dem.crs.to_epsg(), dem.nodata, dem.dtypes)
                == ((7, 7), 1, 32650, -9999, ("float32",))
                and tuple(dem.transform[:6]) == (10, 0, 500000, 0, -10, 4400070)
                and int(dem.read(1, masked=True).count()) == 47,
                "fixture probe: invalid DEM",
            )
            reopened["dem-plane.tif"] = {"shape": dem.shape, "transform": dem.transform[:6], "valid_pixels": 47}
        remote = runpy.run_path(str(project_file(root, "tests/methods/remote_sensing_fixtures.py")))
        paths = remote["write_mixed_products"](directory / "remote")
        for key, bands, dimensions, transform in (
            ("first_product", 4, (3, 5), (30, 0, 500000, 0, -30, 5100000)),
            ("second_product", 3, (10, 16), (10, 0, 499993, 0, -10, 5100007)),
        ):
            with rasterio.open(paths[key][0]) as image:
                require(
                    (image.count, image.shape, tuple(image.transform[:6]), image.crs.to_epsg())
                    == (bands, dimensions, transform, 32632)
                    and all(dtype == "uint16" for dtype in image.dtypes),
                    f"fixture probe: invalid remote product {key}",
                )
                reopened[key] = {"bands": image.count, "shape": image.shape, "transform": image.transform[:6]}

        digests = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                with path.open("rb") as stream:
                    digests[path.relative_to(directory).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
        return {"status": "PASS", "reopened": reopened, "sha256": digests}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="GeoPilot prototype directory")
    parser.add_argument("--manifest", default=MANIFEST, help="project-relative manifest path")
    parser.add_argument("--probe", action="store_true", help="regenerate and reopen the small V1 input fixtures")
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        report = audit(root, args.manifest)
        if args.probe:
            report["fixture_probe"] = fixture_probe(root)
    except (OSError, ValueError, TypeError, AttributeError, ImportError, RecursionError, yaml.YAMLError) as error:
        parser.exit(1, f"Benchmark validation failed: {error}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
