"""Exercise the raster benchmark command and its independent checks."""

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import rasterio
from httpx2 import Response
from rasterio.transform import from_origin
from rasterio.windows import Window


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts" / "bench"


def command(script, *arguments):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *map(str, arguments)], cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=150, env={
            **os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "GDAL_NUM_THREADS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def bench_modules(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("run_raster"), importlib.import_module("_raster_reference")


@pytest.mark.parametrize("profile,family", [("raster", "raster"), ("remote-sensing", "remote_sensing")])
def test_s_pilot_runs_real_tools_and_v1_with_independent_checks(tmp_path, profile, family):
    output = tmp_path / "run"
    completed = command(
        "run_raster.py", "--profile", f"benchmarks/datasets/{profile}.yaml", "--tiers", "S", "--cache", "warm",
        "--pilot", "--output", output,
    )
    assert completed.returncode == 0, completed.stdout
    report = json.loads((output / "report.json").read_text())
    assert (report["status"], report["mode"], report["family"]) == ("PASS", "pilot", family)
    assert report["inputs"]["S"]["first_fingerprints"] == report["inputs"]["S"]["last_fingerprints"]
    assert report["environment"]["os_page_cache"] == "uncontrolled"
    tools, method = jsonl(output / "trials.jsonl")
    assert tools["workload"] == "tools" and method["workload"] == "method"
    assert method["telemetry"]["run_id"] == method["run_id"]
    assert (method["telemetry"]["execution_status"], method["telemetry"]["check_status"]) == ("SUCCEEDED", "PASSED")
    assert method["wall_ns"] > 0 and method["pixels_per_second"] > 0
    assert method["correctness"]["area_statistics"]["observed"]["total_area_m2"] > 0
    assert len(method["telemetry"]["steps"]) == (7 if family == "raster" else 8)
    assert [step["step_key"] for step in method["executed_steps"]] == [
        step["step_key"] for step in method["telemetry"]["steps"]
    ]
    assert all(step["status"] == "SUCCEEDED" and step["exit_code"] == 0 for step in method["executed_steps"])
    for row in tools["steps"]:
        assert row["status"] == "PASS" and row["wall_ns"] > 0 and row["pre_read_bytes"] > 0
        assert row["resources"]["cpu_ns"]["value"] >= 0
        assert row["workspace_size"]["peak_temp_bytes"]["value"] >= 0
    assert all("wall_ns" not in stats for group in report["groups"] for stats in group["operations"].values())


def test_cold_tools_use_fresh_processes_and_record_boundary_windows(tmp_path):
    output = tmp_path / "run"
    result = command(
        "run_raster.py", "--profile", "benchmarks/datasets/raster.yaml", "--tiers", "S",
        "--workload", "tools", "--cache", "process_cold", "--pilot", "--output", output,
    )
    assert result.returncode == 0, result.stdout
    trial, = jsonl(output / "trials.jsonl")
    pids = {item["step_worker_pid"] for item in trial["steps"]}
    assert trial["worker_pid"] not in pids and len(pids) == 7
    assert all(item["step_worker_exit_code"] == 0 for item in trial["steps"])
    probes = {step["step_key"]: step["correctness"] for step in trial["steps"]}
    assert probes["window_read_block_boundary"]["window"] == {
        "col_offset": 240, "row_offset": 240, "width": 32, "height": 32,
    }
    assert probes["window_read_nodata_neighborhood"]["masked_pixels"] == 1
    assert probes["neighborhood_extent"]["expanded_window"] == {
        "col_offset": 0, "row_offset": 0, "width": 512, "height": 512,
    }


def test_existing_directory_and_changed_recipe_are_rejected(tmp_path):
    output = tmp_path / "run"
    output.mkdir()
    original = output / "marker"
    original.write_text("leave intact")
    result = command("run_raster.py", "--profile", "benchmarks/datasets/raster.yaml", "--output", output)
    assert result.returncode == 1 and original.read_text() == "leave intact"
    profile = tmp_path / "raster.yaml"
    profile.write_text((ROOT / "benchmarks/datasets/raster.yaml").read_text().replace("512", "513"))
    result = command("run_raster.py", "--profile", profile, "--output", tmp_path / "unused")
    assert result.returncode == 1 and "accepted P13-01 fixture recipe" in result.stdout
    assert not (tmp_path / "unused").exists()


def test_changed_input_fails_before_measurement(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "S"
    result = command("generate_raster.py", "--tier", "S", "--output", source)
    assert result.returncode == 0, result.stdout
    with (source / "dem.tif").open("ab") as stream:
        stream.write(b"changed")
    output = tmp_path / "report"
    result = command(
        "run_raster.py", "--profile", "benchmarks/datasets/raster.yaml", "--tiers", "S", "--pilot",
        "--input-root", inputs, "--output", output,
    )
    assert result.returncode == 1, result.stdout
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "FAIL" and "fingerprint differs" in report["error"]["message"]
    assert report["groups"] == [] and (output / "trials.jsonl").read_text() == ""


@pytest.mark.parametrize("corruption", ["mask", "pixel", "transform"])
def test_reference_detects_wrong_values_grid_or_mask(bench_modules, tmp_path, corruption):
    _, reference = bench_modules
    path = tmp_path / "data.tif"
    grid = reference.Grid("EPSG:32650", (1, 2), from_origin(500000, 4500000, 10, 10))
    transform = from_origin(500010, 4500000, 10, 10) if corruption == "transform" else grid.affine
    values = [[101 if corruption == "pixel" else 100, 110 if corruption == "mask" else -9999]]
    with rasterio.open(
        path, "w", driver="GTiff", width=2, height=1, count=1, crs=grid.crs, dtype="float32",
        nodata=-9999, transform=transform,
    ) as dataset:
        dataset.write(np.array(values, dtype="float32"), 1)
    with pytest.raises(ValueError, match={"mask": "mask", "pixel": "pixel error", "transform": "Grid"}[corruption]):
        reference.verify_raster(
            path, grid, dtypes=["float32"], nodata=[-9999],
            expected=lambda _: (np.array([[100, 110]]), np.array([[True, False]])),
        )


def test_independent_qa_phase_and_nearest_grid_semantics(bench_modules):
    _, reference = bench_modules
    geometry = {"landsat_side": 192, "offset": 7, "first_pixel": 30, "second_pixel": 10, "scl_pixel": 20}
    first, valid = reference.optical_values(
        Window(0, 0, 6, 1), role="first_product", stage="ndvi", **geometry,
    )
    assert valid.tolist() == [[True, True, True, False, True, False]]
    assert first[0, 1] == pytest.approx(11 / 17)
    change, common = reference.aligned_values(Window(0, 0, 6, 1), "change", **geometry)
    assert common.tolist() == [[True, True, True, False, False, False]]
    np.testing.assert_allclose(change[common], [0.5, 0.5 - 11 / 17, 0], rtol=0, atol=1e-15)


def test_statistics_preserve_failed_missing_and_warmup_samples(bench_modules):
    runner, _ = bench_modules
    rows = [
        {"kind": "warmup", "status": "PASS", "wall_ns": 500},
        {"kind": "measurement", "status": "PASS", "wall_ns": 1},
        {"kind": "measurement", "status": "PASS", "wall_ns": 2},
        {"kind": "measurement", "status": "FAIL", "wall_ns": 1000},
        {"kind": "measurement", "status": "PASS", "wall_ns": 4},
        {"kind": "measurement", "status": "PASS", "wall_ns": 3},
    ]
    summary = runner.summarize_trials(rows, ["method"])["method"]
    assert (summary["passed"], summary["failed"], summary["missing"]) == (4, 1, 0)
    assert summary["wall_ns"] == {"median": 2.5, "p95_nearest_rank": 4}
    short = runner.summarize_trials(rows[:4], ["method"])["method"]
    assert short == {"passed": 2, "failed": 1, "missing": 2}


def test_status_observation_preserves_storage_error_body_and_limits_reads(bench_modules, monkeypatch):
    runner, _ = bench_modules
    workload = importlib.import_module("_raster_workloads")
    monkeypatch.setattr(workload.time, "sleep", lambda _: None)
    body = {"detail": {"code": "STORAGE_FAILED", "message": "Could not read the run record."}}
    terminal = {"execution_status": "SUCCEEDED", "check_status": "PASSED"}

    class Client:
        def __init__(self, *responses):
            self.responses = iter(responses)

        def get(self, _):
            return next(self.responses)

    error = Response(500, json=body)
    trial = {}
    state = workload._terminal_state(
        Client(error, Response(200, json=terminal)), "/api/runs/example", trial, time.monotonic() + 1,
    )
    assert state == terminal
    assert trial == {"run_state": terminal, "status_poll_errors": [{"http_status": 500, "body": body}]}
    trial = {}
    with pytest.raises(RuntimeError, match="Three consecutive"):
        workload._terminal_state(
            Client(error, error, error), "/api/runs/example", trial, time.monotonic() + 1,
        )
    assert len(trial["status_poll_errors"]) == 3
    summary = runner.summarize_trials([{"kind": "measurement", "status": "FAIL", **trial}], ["method"])["method"]
    assert summary == {"passed": 0, "failed": 1, "missing": 4}


def test_cold_operation_records_exit_when_worker_sends_no_result(bench_modules, tmp_path):
    runner, _ = bench_modules
    result, observed = runner._spawn_operation(sys.exit, (7,), {}, tmp_path)
    assert result is None and observed["step_worker_exit_code"] == 7
    assert observed["error"] == {"type": "EOFError", "message": "Cold worker sent no result"}
    assert observed["wall_with_process_startup_ns"] > 0


@pytest.mark.parametrize("timeout", [False, True], ids=["passed", "timed-out-prefix"])
def test_collects_actual_normalized_requests_for_executed_steps(bench_modules, tmp_path, timeout):
    from geopilot.domain.step_io import StepRequest, StepResult
    from geopilot.persistence.run_repository import insert_run, save_run_step
    from geopilot.persistence.schema import initialize_database, open_database
    from geopilot.runs.models import Run, RunStep

    collector = importlib.import_module("_raster_workloads")
    db = tmp_path / "project.sqlite"
    workdir = tmp_path / "run"
    stamp = "2026-11-12T00:00:00Z"
    execution_status, check_status = ("FAILED", "NOT_CHECKED") if timeout else ("SUCCEEDED", "PASSED")
    initialize_database(db)
    with open_database(db) as connection, connection:
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES ('task', '{}', ?)", (stamp,))
        connection.execute(
            """INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at)
               VALUES ('plan', 1, 'task', 'confirmed', '{}', ?)""", (stamp,),
        )
        insert_run(connection, Run(id="run", plan_id="plan", plan_version=1, workdir=str(workdir), created_at=stamp,
                                   execution_status=execution_status, check_status=check_status))
        for position, key in enumerate(("prepare", "measure")):
            log_path = f"steps/{key}/process.log"
            request = StepRequest(
                run_id="run", step_key=key, tool={"id": key, "version": 1}, workdir_ref="run",
                inputs={}, parameters={"distance": {"value": 0.5, "unit": "kilometer"}}, output_targets={},
                log_path=log_path, cancel_path="cancel.flag",
            )
            normalized = workdir / "steps" / key / "request.json"
            normalized.parent.mkdir(parents=True)
            normalized.write_text(request.model_copy(update={"parameters": {"distance": 500}}).model_dump_json())
            failed = timeout and key == "measure"
            error = {
                "code": "PROCESS_TIMEOUT", "phase": "execute", "step_key": key, "field": None,
                "message": "Process exceeded 60 seconds", "suggestion": "Read the process log.", "log_path": log_path,
            } if failed else None
            result = StepResult(
                run_id="run", step_key=key, status="FAILED" if failed else "SUCCEEDED", exit_code=-15 if failed else 0,
                outputs={}, log_path=log_path, warnings=[], error=error, started_at=stamp, finished_at=stamp, environment={},
            )
            save_run_step(connection, RunStep(run_id="run", step_key=key, position=position, status=result.status,
                                             request=request, result=result, log_path=log_path))
        if timeout:
            save_run_step(connection, RunStep(run_id="run", step_key="pending", position=2))

    state = {"run_id": "run", "execution_status": execution_status, "check_status": check_status}
    observed = collector._executed_step_requests(db, workdir, "run", state)
    assert [(step["step_key"], step["status"], step["exit_code"]) for step in observed] == [
        ("prepare", "SUCCEEDED", 0), ("measure", execution_status, -15 if timeout else 0),
    ]
    assert all(step["parameters"] == {"distance": 500} for step in observed)
    assert [step["tool"] for step in observed] == [{"id": "prepare", "version": 1}, {"id": "measure", "version": 1}]

    normalized.write_text(request.model_copy(update={"run_id": "different-run"}).model_dump_json())
    with pytest.raises(ValueError, match="Normalized request identity differs"):
        collector._executed_step_requests(db, workdir, "run", state)
