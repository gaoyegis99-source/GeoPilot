import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pyogrio
import pytest
import shapely
from shapely.affinity import translate

from geopilot.gis.vector.polygon_statistics import aggregate_polygon_statistics


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/bench/generate_vector.py"
ID = "source_feature_id"


def run_generator(output: Path, tier="S", *, root=ROOT):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--tier", tier, "--output", str(output), "--root", str(root)],
        capture_output=True, text=True, check=False,
    )


def read_manifest(output: Path):
    return json.loads((output / "manifest.json").read_bytes())


def read_tile(package: Path, layer: str, tile: str):
    return pyogrio.read_dataframe(package, layer=layer, where=f"tile_id = '{tile}'")


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    directory = tmp_path_factory.mktemp("vector") / "S"
    result = run_generator(directory)
    assert result.returncode == 0, result.stderr
    return directory


def test_package_layers_and_hashes_match_the_manifest(small):
    manifest = read_manifest(small)
    assert (manifest["version"], manifest["family"], manifest["tier"]) == (1, "vector", "S")
    assert {path.name for path in small.iterdir()} == {"road.gpkg", "relations.gpkg", "t14.gpkg", "manifest.json"}
    reference = manifest["reference"]
    assert (reference["tiles"], reference["road_vertices"], reference["attribute_bytes"]) == (16, 2, 16)
    for filename, digest in manifest["source_sha256"].items():
        assert hashlib.sha256((ROOT / filename).read_bytes()).hexdigest() == digest
    for entry in manifest["files"]:
        path = small / entry["path"]
        assert entry["bytes"] == path.stat().st_size
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert set(pyogrio.list_layers(path)[:, 0]) == reference["layer_counts"][path.name].keys()
        for layer, count in reference["layer_counts"][path.name].items():
            frame = pyogrio.read_dataframe(path, layer=layer)
            expected = reference["layers"][path.name][layer]
            assert len(frame) == count == expected["features"]
            assert frame.crs.to_string() == reference["crs"][path.name]
            assert frame[ID].nunique() == expected["unique_source_ids"]
            assert frame[ID].is_monotonic_increasing
            assert (frame[ID] == frame["tile_id"] + "/" + frame["template_id"]).all()
            assert frame["bench_payload"].str.encode("ascii").str.len().eq(16).all()
            assert frame.geometry.is_valid.all()
            digest = hashlib.sha256()
            fields = frame[[ID, "tile_id", "template_id", "bench_payload"]]
            for attributes, wkb in zip(
                fields.itertuples(index=False, name=None), frame.geometry.to_wkb(hex=True, byte_order=1), strict=True,
            ):
                digest.update(json.dumps([*attributes, wkb], separators=(",", ":")).encode("utf-8") + b"\n")
            assert digest.hexdigest() == expected["logical_sha256"]


def test_road_references_preserve_boundary_and_outside_features(small):
    reference = read_manifest(small)["reference"]["road"]
    tile = "r000c000"
    area = read_tile(small / "road.gpkg", "analysis_extent", tile).geometry.item()
    roads = read_tile(small / "road.gpkg", "roads", tile).set_index("template_id").geometry
    assert tuple(area.bounds) == (244000, 4144000, 246000, 4146000)
    assert list(roads.loc["road-main"].coords) == [(244500, 4145500), (245500, 4145500)]
    assert roads.distance(area).to_dict() == reference["road_distance_to_extent_m"]
    candidates = roads[roads.distance(area) <= 500]
    assert candidates.index.tolist() == reference["candidate_road_ids"]
    capsule = shapely.from_wkt(reference["main_road_buffer"]["reference_wkt"])
    assert capsule.bounds == tuple(reference["main_road_buffer"]["bounds_xy_m"])
    assert capsule.area == pytest.approx(reference["main_road_buffer"]["discrete_area_m2"], abs=0.01)
    buffered = shapely.union_all(candidates.buffer(500, resolution=16))
    targets = read_tile(small / "road.gpkg", "targets", tile).set_index("template_id").geometry
    for predicate in ("intersects", "within"):
        actual = targets[getattr(targets, predicate)(buffered)]
        expected = reference["relations"][predicate]
        assert actual.index.tolist() == expected["before_clip_target_ids"]
        assert actual[actual.intersects(area)].index.tolist() == expected["clipped_target_ids"]
        assert expected["total_counts"]["total"] == len(expected["clipped_target_ids"]) * 16


def test_t13_late_tile_retains_boundary_and_clip_geometry(small):
    reference = read_manifest(small)["reference"]["t13"]["relations"]
    tile = "r003c002"
    area = read_tile(small / "relations.gpkg", "area", tile).geometry.item()
    assert len(area.interiors) == 1
    assert tuple(area.interiors[0].bounds) == (252040, 4156040, 252060, 4156060)
    for layer in ("points", "lines", "polygons"):
        features = read_tile(small / "relations.gpkg", layer, tile).set_index("template_id").geometry
        for predicate in ("intersects", "within"):
            expected = reference[predicate][layer]
            retained = features[getattr(features, predicate)(area)]
            assert retained.index.tolist() == expected["per_tile_ids"]
            for identifier, geometry in retained.items():
                clip = geometry.intersection(area)
                template = shapely.from_wkt(expected["per_tile_clipped"][identifier]["wkt"])
                assert clip.equals(translate(template, xoff=252000 - 500000, yoff=4156000 - 4400000))


def test_t14_duplicates_and_allocation_match_manual_reference(small):
    reference = read_manifest(small)["reference"]["t14"]
    tile = "r003c003"
    package = small / "t14.gpkg"
    originals = read_tile(package, "targets", tile)
    assert originals.crs.to_string() == "EPSG:6933"
    assert Counter(originals["template_id"])["crossing"] == 2
    extent = read_tile(package, "analysis_extent", tile).geometry.union_all()
    clipped = originals.copy()
    clipped.geometry = clipped.geometry.intersection(extent)
    clipped = clipped.loc[~clipped.geometry.is_empty]
    assert len(clipped) == reference["clipped_rows"] // 16
    assert Counter(clipped["template_id"]) == reference["clipped_rows_per_id_per_tile"]
    for row in clipped.itertuples():
        expected = shapely.from_wkt(reference["clipped_geometry_per_tile"][row.template_id]["wkt"])
        assert row.geometry.equals(translate(expected, xoff=12000, yoff=12000))
    zones = read_tile(package, "zones", tile)
    for method, expected in reference["methods"].items():
        result = aggregate_polygon_statistics(clipped, originals, zones, boundary_assignment=method, area_crs="EPSG:6933")
        assert result.summary["counts"] == {key: value / 16 for key, value in expected["counts"].items()}
        assert result.summary["areas_m2"] == {key: value / 16 for key, value in expected["areas_m2"].items()}
        details = {(row.source_feature_id, row.zone_id): (row.object_status, row.area_m2) for row in result.details}
        assert details == {
            (f"{tile}/{row[ID]}", f"{tile}/{row['zone_id']}" if row["zone_id"] else None):
            (row["object_status"], row["area_m2"]) for row in expected["details_per_tile"]
        }


def test_small_package_replays_byte_for_byte(tmp_path, small):
    repeated = tmp_path / "repeated"
    result = run_generator(repeated)
    assert result.returncode == 0, result.stderr
    assert (repeated / "manifest.json").read_bytes() == (small / "manifest.json").read_bytes()
    for path in small.glob("*.gpkg"):
        assert (repeated / path.name).read_bytes() == path.read_bytes()


def test_medium_tier_adds_vertices_and_payload_without_moving_endpoints(tmp_path):
    output = tmp_path / "M"
    result = run_generator(output, "M")
    assert result.returncode == 0, result.stderr
    manifest = read_manifest(output)
    assert manifest["reference"]["tiles"] == 1024
    roads = read_tile(output / "road.gpkg", "roads", "r031c031").set_index("template_id")
    coordinates = list(roads.loc["road-main", "geometry"].coords)
    assert coordinates == [(368500 + index * 125, 4269500) for index in range(9)]
    assert roads["bench_payload"].str.encode("ascii").str.len().eq(64).all()


@pytest.mark.parametrize("existing", ("file", "directory", "dangling_symlink"))
def test_existing_output_is_preserved(tmp_path, existing):
    output = tmp_path / "output"
    if existing == "file":
        output.write_bytes(b"existing")
    elif existing == "directory":
        output.mkdir()
        (output / "sentinel").write_bytes(b"existing")
    else:
        output.symlink_to(tmp_path / "missing")
    result = run_generator(output)
    assert result.returncode == 2 and "already exists" in result.stderr
    if existing == "file":
        assert output.read_bytes() == b"existing"
    elif existing == "directory":
        assert list(output.iterdir()) == [output / "sentinel"]
        assert (output / "sentinel").read_bytes() == b"existing"
    else:
        assert output.is_symlink() and not output.exists()


def test_bad_road_geometry_leaves_no_output_or_changes_to_inputs(tmp_path):
    prototype = tmp_path / "prototype"
    profile = "benchmarks/datasets/vector.yaml"
    for filename in (
        profile, "tests/fixtures/vector/source.json", "tests/fixtures/vector/expected.json",
        "tests/fixtures/vector/t13/source.json", "tests/fixtures/vector/t13/expected.json",
        "tests/methods/test_full_geometry_before_clip.py", "tests/gis/test_source_feature_identity.py",
    ):
        target = prototype / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / filename, target)
    roads = prototype / "tests/fixtures/vector/source.json"
    document = json.loads(roads.read_bytes())
    document["layers"]["roads"][0]["wkt"] = "LINESTRING (500500 4401500, 501000 4401600, 501500 4401500)"
    roads.write_text(json.dumps(document), encoding="utf-8")
    before = roads.read_bytes()
    output = tmp_path / "bad"
    result = run_generator(output, root=prototype)
    assert result.returncode == 2 and "straight two-vertex LineString" in result.stderr
    assert not output.exists()
    assert roads.read_bytes() == before
