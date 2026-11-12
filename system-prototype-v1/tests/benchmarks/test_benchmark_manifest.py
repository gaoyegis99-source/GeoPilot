import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/bench/validate_benchmark_manifest.py"
MANIFEST = "benchmarks/manifest.yaml"


@pytest.fixture
def prototype(tmp_path):
    for path in ("benchmarks", "config", "tests/fixtures/vector"):
        shutil.copytree(ROOT / path, tmp_path / path)
    return tmp_path


def run_validator(root, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True, text=True, check=False,
    )


def replace(root, path, old, new):
    file = root / path
    content = file.read_text(encoding="utf-8")
    assert content.count(old) == 1
    file.write_text(content.replace(old, new), encoding="utf-8")


def test_profiles_have_deterministic_counts_and_pinned_methods(prototype):
    first = run_validator(prototype)
    second = run_validator(prototype)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    report = json.loads(first.stdout)
    assert report["status"] == "PASS"
    assert report["tiers"]["vector"]["L"]["roads"] == {
        "analysis_extent": 16384, "roads": 49152, "targets": 147456, "zones": 32768,
    }
    assert report["tiers"]["raster"]["S"] == {
        "pixels": 262144, "input_nodata_pixels": 16, "valid_terrain_pixels": 259956,
        "raw_input_bytes": 1048576,
    }
    assert report["tiers"]["remote_sensing"]["S"]["sentinel_pixels"] == 578 ** 2
    assert report["tiers"]["remote_sensing"]["L"]["common_valid_pixels"] == 3072 ** 2 // 2
    assert {name: spec["expected_steps"] for name, spec in report["method_workloads"].items()} == {
        "vector": 7, "raster": 7, "remote_sensing": 8,
    }


def test_policy_minimum_accepts_three_and_rejects_two(prototype):
    replace(prototype, MANIFEST, "repeats_per_cache_state: 5", "repeats_per_cache_state: 3")
    assert run_validator(prototype).returncode == 0
    replace(prototype, MANIFEST, "repeats_per_cache_state: 3", "repeats_per_cache_state: 2")
    result = run_validator(prototype)
    assert result.returncode == 1 and "repeats_per_cache_state" in result.stderr


@pytest.mark.parametrize(
    ("path", "old", "new", "diagnostic"),
    [
        (MANIFEST, "  warmup_runs: 2", "  warmup_runs: 2\n  warmup_runs: 2", "duplicate"),
        (MANIFEST, "benchmarks/README.md", "../outside.md", "unsafe project-relative"),
        ("benchmarks/datasets/vector.yaml", "side_tiles: 32", "side_tiles: 4", "S < M < L required"),
        ("benchmarks/datasets/vector.yaml", "side_tiles: 128", "side_tiles: 300", "UTM envelope"),
        ("tests/fixtures/vector/source.json", "\"source_feature_id\": \"road-main\"", "\"source_feature_id\": \"moved-id\"", "template IDs differ"),
        ("benchmarks/datasets/raster.yaml", "side_pixels: 512", "side_pixels: 511", "block and NoData periods"),
        ("benchmarks/datasets/raster.yaml", "side_pixels: 512", "side_pixels: true", "valid integer"),
        ("benchmarks/datasets/remote-sensing.yaml", "side_pixels: 192", "side_pixels: 193", "phase cycles"),
        ("benchmarks/datasets/remote-sensing.yaml", "landsat-8-c2-l2sp", "landsat-8-c2-l2sr", "unexpected product key"),
        ("benchmarks/datasets/method.yaml", "    expected_steps: 8", "    expected_steps: 7", "benchmark identity"),
    ],
)
def test_invalid_manifest_fails_without_passing_report(prototype, path, old, new, diagnostic):
    replace(prototype, path, old, new)
    result = run_validator(prototype)
    assert result.returncode == 1 and diagnostic in result.stderr
    assert not result.stdout


def test_probe_generates_real_tiny_files_and_returns_sha256():
    result = run_validator(ROOT, "--probe")
    assert result.returncode == 0, result.stderr
    probe = json.loads(result.stdout)["fixture_probe"]
    assert probe["status"] == "PASS"
    assert probe["reopened"]["dem-plane.tif"]["valid_pixels"] == 47
    assert probe["reopened"]["second_product"]["transform"] == [10, 0, 499993, 0, -10, 5100007]
    assert {
        "road.gpkg", "relations.gpkg", "dem-plane.tif", "two-band.tif",
        "remote/first-landsat.tif", "remote/second-sentinel.tif", "remote/analysis-extent.gpkg",
    } <= probe["sha256"].keys()
    assert all(len(digest) == 64 and int(digest, 16) > 0 for digest in probe["sha256"].values())
