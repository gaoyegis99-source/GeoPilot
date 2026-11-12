import importlib
import json
import os
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

import httpx2
import numpy as np
import pyogrio
import pytest
import shapely
from shapely.affinity import translate
from shapely.geometry import Point


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/bench/run_vector.py"


@pytest.fixture
def benchmark(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    return importlib.import_module("run_vector")


def test_translates_two_and_three_digit_grid_coordinates(benchmark):
    reference = importlib.import_module("_vector_reference")
    layout = {"tile_pitch_m": 4, "tile_origin_xy_m": {"a": (10, 20)},
              "template_origin_xy_m": {"a": (1, 2)}}
    geometries = reference.translate([Point(1, 2), Point(2, 3)], ["r002c005", "r123c127"], layout, "a")
    np.testing.assert_array_equal(shapely.get_coordinates(geometries), [[30, 28], [519, 513]])


def sample(phase, status, repeat, elapsed):
    return {
        "tier": "S", "workload": "road", "cache": "warm", "phase": phase, "status": status, "repeat": repeat,
        "wall_ns": elapsed, "input_bytes": 3, "output_bytes": 2, "peak_temp_bytes": {"value": 4}, "telemetry": {},
    }


def test_nearest_rank_and_only_correct_measured_rows_enter_statistics(benchmark):
    rows = [sample("measurement", "PASS", i, value) for i, value in enumerate((70, 50, 100, 80, 60), 1)]
    rows.extend([sample("warmup", "PASS", 1, 1200), sample("probe", "PASS", 1, 1700),
                 sample("measurement", "FAIL", 6, 1900), sample("measurement", "NOT_RUN", 7, None)])
    result, = benchmark.summarize(rows, 5)
    assert (result["wall_ns_median"], result["wall_ns_p95"], result["wall_ns_n"]) == (70, 100, 5)
    assert (result["measured_pass"], result["measured_fail"], result["measured_not_run"]) == (5, 1, 1)
    assert result["warmup_pass"] == 1 and result["probe_pass"] == 1
    assert result["peak_rss_bytes_n"] == 0 and result["peak_rss_bytes_p95"] is None


@pytest.mark.parametrize(("count", "status", "median"), [(2, "INSUFFICIENT", None), (3, "PARTIAL", 20)])
def test_incomplete_groups_have_explicit_minimum(benchmark, count, status, median):
    rows = [sample("measurement", "PASS", i, i * 10) for i in range(1, count + 1)]
    result, = benchmark.summarize(rows, 5)
    assert result["status"] == status and result["wall_ns_median"] == median


def test_step_statistics_keep_versions_and_partial_resource_counts(benchmark):
    rows = []
    for repeat, elapsed in enumerate((10, 20, 100), 1):
        item = sample("measurement", "PASS", repeat, elapsed * 2)
        item["telemetry"] = {"document": {"steps": [{
            "step_key": "create_buffer", "tool": {"id": "qgis-buffer", "version": 1}, "wall_ns": elapsed,
            "resources": {name: {"value": elapsed if name == "peak_rss_bytes" else None,
                                 "status": "partial" if name == "peak_rss_bytes" else "unavailable"}
                          for name in benchmark.RESOURCES},
        }]}}
        rows.append(item)
    row, = benchmark.summarize_steps(rows)
    assert (row["tool"], row["tool_version"]) == ("qgis-buffer", 1)
    assert (row["wall_ns_median"], row["wall_ns_p95"]) == (20, 100)
    assert (row["peak_rss_bytes_n"], row["peak_rss_bytes_partial_n"]) == (3, 3)
    assert row["disk_read_bytes_median"] is None


def test_status_read_storage_failure_is_recorded_and_retried(benchmark, monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark, "POLL_SECONDS", 0)
    status = {"execution_status": "SUCCEEDED", "check_status": "PASSED"}
    responses = iter((
        httpx2.Response(500, json={"detail": {"code": "STORAGE_FAILED", "message": "Could not read the run record."}}),
        httpx2.Response(200, json=status),
    ))

    class Client:
        def get(self, route):
            assert route == "/api/runs/test"
            return next(responses)

    sample = {}
    benchmark.poll_run(Client(), "test", tmp_path, time.monotonic_ns(), 1, sample)
    assert sample["run_status"] == status and sample["wall_ns"] > 0
    assert len(sample["status_read_errors"]) == 1
    assert "STORAGE_FAILED" in sample["status_read_errors"][0]["response"]
    assert benchmark.terminal_run({"execution_status": "SUCCEEDED", "check_status": "NOT_CHECKED"})
    assert not benchmark.terminal_run({"execution_status": "SUCCEEDED", "check_status": "CHECKING"})


def test_count_bytes_deduplicates_hardlinks_and_excludes_formal_output(benchmark, tmp_path):
    (tmp_path / "steps").mkdir()
    data = tmp_path / "steps" / "data"
    data.write_bytes(b"1234")
    hardlink = data.with_name("linked")
    hardlink.hardlink_to(data)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "retained").write_bytes(b"123456789")
    assert benchmark.count_bytes([data, hardlink]) == 4
    assert benchmark.temporary_bytes(tmp_path) == 4
    formal = tmp_path / "steps" / "formal"
    formal.write_bytes(b"123456")
    formal.with_name("formal-alias").hardlink_to(formal)
    assert benchmark.temporary_bytes(tmp_path, ["steps/formal"]) == 4


@pytest.mark.parametrize(("options", "error"), [
    (["--tier", "XL"], "invalid choice"),
    (["--wait-seconds", "nan"], "positive finite"),
    (["--budget-seconds", "0"], "positive finite"),
    (["--profile", "benchmarks/datasets/method.yaml"], "accepted"),
    (["--road-profile", "benchmarks/datasets/vector.yaml"], "--road-profile must refer"),
    (["--tier", "M", "--road-profile", "benchmarks/vector/road-l-utm50n-south.v1.yaml"], "requires tier L"),
    (["--qgis-process", "/no-such/qgis_process"], "executable file"),
])
def test_invalid_cli_does_not_create_an_output(options, error, tmp_path):
    output = tmp_path / "new"
    result = subprocess.run([sys.executable, str(SCRIPT), "--output", str(output), *options],
                            cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 2 and error in result.stderr
    assert not output.exists()


def test_corrected_road_generation_preserves_a_small_fixture_except_northing(benchmark, tmp_path):
    road_profile = importlib.import_module("_vector_road_profile")
    original_dir = tmp_path / "original"
    original = benchmark.generate(ROOT, "S", original_dir)
    # Use the small accepted grid to exercise the physical writer quickly. The CLI
    # restricts the position correction to the real L cohort.
    original["tier"] = "L"
    base_path = original_dir / "manifest.json"
    base_path.write_text(json.dumps(original), encoding="utf-8")
    base_bytes = base_path.read_bytes()
    output = tmp_path / "corrected"
    corrected = road_profile.generate_corrected_road(ROOT, original_dir, output, ROOT / road_profile.ROAD_PROFILE)
    assert list(sorted(path.name for path in output.iterdir())) == ["manifest.json", "road.gpkg"]
    assert base_path.read_bytes() == base_bytes
    assert benchmark.file_record(original_dir / "road.gpkg") == corrected["base_road_file"]
    assert corrected["input_profile"] == "road-l-utm50n-south@1"
    assert corrected["translation_xy_m"] == (0, -264000)
    assert corrected["reference"]["layer_counts"]["road.gpkg"] == original["reference"]["layer_counts"]["road.gpkg"]
    assert corrected["reference"]["road"] == original["reference"]["road"]
    original_roads = pyogrio.read_dataframe(original_dir / "road.gpkg", layer="roads")
    corrected_roads = pyogrio.read_dataframe(output / "road.gpkg", layer="roads")
    assert original_roads.drop(columns="geometry").equals(corrected_roads.drop(columns="geometry"))
    assert all(shapely.equals(corrected_roads.geometry, [translate(shape, yoff=-264000) for shape in original_roads.geometry]))


def test_failed_warm_plan_setup_does_not_duplicate_an_unsubmitted_sample(benchmark, monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark, "create_local_app", lambda *args, **kwargs: None)
    monkeypatch.setattr(benchmark, "TestClient", lambda app: nullcontext())

    def reject(*args):
        raise ValueError("POST /api/plans/test/confirm: HTTP 422: MEASUREMENT_POLICY_REJECTED")

    monkeypatch.setattr(benchmark, "prepare_plan", reject)
    (tmp_path / "manifest.json").write_text('{"input_profile":"test-profile"}', encoding="utf-8")
    output = tmp_path / "samples.jsonl"
    benchmark.run_batch(tmp_path, tmp_path, benchmark.workloads()["road"], "L", "warm",
                        [("warmup", 1), ("warmup", 2), ("measurement", 1)],
                        Path("/usr/bin/qgis_process"), 600, None, output)
    first, *rest = [json.loads(line) for line in output.read_text().splitlines()]
    assert first["status"] == "FAIL" and first["stage"] == "plan_setup" and "HTTP 422" in first["error"]
    assert all(sample["status"] == "NOT_RUN" and sample["reason"] == "plan_setup_failed" for sample in rest)


def test_existing_output_is_preserved(tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("existing", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(tmp_path), "--qgis-process", sys.executable],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 2 and "new directory" in result.stderr
    assert marker.read_text(encoding="utf-8") == "existing"


@pytest.mark.skipif(not os.environ.get("GEOPILOT_BENCH_QGIS"), reason="native QGIS benchmark executable is unavailable")
def test_native_probe_runs_confirmed_plan_and_excludes_measurements(tmp_path):
    output = tmp_path / "probe"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(output), "--qgis-process", os.environ["GEOPILOT_BENCH_QGIS"],
         "--tier", "S", "--workload", "t13-lines-intersects", "--probe"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((output / "summary.json").read_text())
    assert summary["sample_counts"] == {"PASS": 1, "FAIL": 0, "NOT_RUN": 0}
    assert summary["groups"][0]["measured_pass"] == 0
    sample = json.loads((output / "samples.jsonl").read_text())
    assert sample["phase"] == "probe"
    assert sample["correctness"] == {"selected": 128, "clipped": 128, "retained_line_length_m": 3936, "retained_area_m2": 0}
    assert sample["run_status"]["check_status"] == "PASSED"
    assert sample["telemetry"]["document"]["run_id"] == sample["run_id"]
