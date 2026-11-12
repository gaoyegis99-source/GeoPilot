"""Measure the exact V1 vector methods in isolated, serial benchmark runs."""

import argparse
import csv
import importlib.metadata
import json
import math
import multiprocessing
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyogrio
import pyproj
import shapely
from fastapi.testclient import TestClient

from _vector_reference import check_road, check_t13, check_t14, require
from _vector_road_profile import ROAD_PROFILE, generate_corrected_road
from generate_vector import PROFILE, file_record, generate
from validate_benchmark_manifest import ROOT, TIERS, read_yaml

sys.path.insert(0, str(ROOT / "src"))

from geopilot.api.server import create_local_app
from geopilot.artifacts.service import list_artifacts
from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.observability.telemetry import SIDECAR_NAME
from geopilot.persistence.run_repository import get_run, list_run_steps
from geopilot.persistence.schema import open_database


POLL_SECONDS = 0.1
TEMP_SAMPLE_SECONDS = 0.25
TEMP_ENTRY_LIMIT = 4096
STEP_LOG_LIMIT = 4096
STATUS_READ_RETRIES = 3
TERMINAL_EXECUTION = {"SUCCEEDED", "FAILED", "CANCELLED"}
TERMINAL_CHECK = {"NOT_CHECKED", "PASSED", "WARNING", "FAILED"}
RESOURCES = ("cpu_ns", "peak_rss_bytes", "disk_read_bytes", "disk_write_bytes")


@dataclass(frozen=True)
class Workload:
    name: str
    method: str
    version: int
    expected_steps: int
    package: str
    layers: dict[str, str]
    options: dict


def workloads() -> dict[str, Workload]:
    profile = read_yaml(ROOT, "benchmarks/datasets/method.yaml")["workloads"]["vector"]
    items = [
        Workload(f"t13-{layer}-{relation}", "spatial-select-and-clip", 1, 2, "relations.gpkg",
                 {"targets": layer, "relation_area": "area", "analysis_extent": "area"},
                 {"spatial_relation": relation})
        for layer in ("points", "lines", "polygons") for relation in ("intersects", "within")
    ]
    items.extend(
        Workload(f"t14-{mode}", "polygon-zone-statistics", 1, 3, "t14.gpkg",
                 {role: role for role in ("targets", "relation_area", "analysis_extent", "zones")},
                 {"spatial_relation": "intersects", "boundary_assignment": mode,
                  "boundary_tie": "unassigned", "area_crs": "EPSG:6933"})
        for mode in ("split_by_area", "centroid", "largest_overlap")
    )
    items.append(Workload(
        "road", profile["method_id"], profile["version"], profile["expected_steps"], "road.gpkg",
        {role: role for role in ("analysis_extent", "roads", "targets", "zones")},
        {"distance_m": 500, "distance_method": "local_projected", "distance_crs_ref": "EPSG:32650",
         "spatial_relation": "intersects", "boundary_assignment": "centroid", "boundary_tie": "unassigned",
         "statistic": "count", "output_scope": "clipped_to_analysis_extent"},
    ))
    return {item.name: item for item in items}


def read_text(path: str):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def command_output(command, *, cwd=None):
    try:
        result = subprocess.run(command, cwd=cwd, timeout=30, text=True, capture_output=True)
        return {"exit_code": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"exit_code": None, "error": str(error)}


def collect_environment(qgis: Path, source_commit: str | None = None) -> dict:
    """Return runtime and resource scope for the native P13 benchmark processes."""
    cpuinfo = (read_text("/proc/cpuinfo") or "").split("\n\n")
    cpu_records = [dict(line.split(":", 1) for line in block.splitlines() if ":" in line) for block in cpuinfo]
    cpu_records = [{key.strip(): value.strip() for key, value in item.items()} for item in cpu_records]
    physical = {(item["physical id"], item["core id"]) for item in cpu_records
                if {"physical id", "core id"} <= item.keys()}
    memory = (read_text("/proc/meminfo") or "").splitlines()
    cgroup_paths = (
        "cpu.max", "cpuset.cpus.effective", "memory.max", "memory.swap.max",
        "cpu/cpu.cfs_quota_us", "cpu/cpu.cfs_period_us", "memory/memory.limit_in_bytes",
    )
    return {
        "source_commit": source_commit or command_output(["git", "rev-parse", "HEAD"], cwd=ROOT).get("stdout"),
        "os_release": read_text("/etc/os-release"),
        "platform": platform.platform(),
        "cpu_models": sorted({item["model name"] for item in cpu_records if "model name" in item}),
        "logical_cpu_count": os.cpu_count(),
        "physical_core_count": len(physical) or None,
        "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "memory_and_swap": [line for line in memory if line.startswith(("MemTotal:", "SwapTotal:"))],
        "cgroup_membership": read_text("/proc/self/cgroup"),
        "cgroup_limits": {path: read_text(f"/sys/fs/cgroup/{path}") for path in cgroup_paths},
        "load_average": read_text("/proc/loadavg"),
        "io_pressure": read_text("/proc/pressure/io"),
        "storage_devices": command_output(["lsblk", "-o", "TYPE,ROTA,FSTYPE"]),
        "python": sys.version,
        "python_packages": {name: importlib.metadata.version(name) for name in
                            ("geopandas", "numpy", "pyogrio", "pyproj", "rasterio", "shapely", "fastapi", "starlette")},
        "qgis_process": str(qgis),
        "qgis_process_sha256": file_record(qgis)["sha256"],
        "qgis_process_binary_sha256": file_record(qgis.with_name(qgis.name + ".bin"))["sha256"]
        if qgis.with_name(qgis.name + ".bin").is_file() else None,
        "qgis_version": command_output([str(qgis), "--version"]),
        "pyogrio_gdal": pyogrio.__gdal_version_string__,
        "proj": pyproj.proj_version_str,
        "proj_data": pyproj.database.get_database_metadata("PROJ_DATA.VERSION"),
        "geos": shapely.geos_version_string,
        "thread_and_cache_settings": {key: os.environ.get(key) for key in (
            "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "GDAL_NUM_THREADS", "GDAL_CACHEMAX", "MKL_NUM_THREADS",
            "QT_QPA_PLATFORM", "QGIS_CUSTOM_CONFIG_PATH", "GEOPILOT_TELEMETRY", "GEOPILOT_TELEMETRY_QGIS_SCOPE",
        )},
    }


def write_json(path: Path, document) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def append_json(path: Path, document: dict) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(document, ensure_ascii=False, allow_nan=False) + "\n")


def post(client, path: str, body: dict):
    response = client.post(path, json=body)
    if response.is_error:
        raise ValueError(f"POST {path}: HTTP {response.status_code}: {response.text}")
    return response.json()


def prepare_plan(client, database: Path, inputs: Path, workload: Workload):
    started = time.monotonic_ns()
    roles = {role: register_asset(database, inputs / workload.package, layer=layer).ref.model_dump(mode="json")
             for role, layer in workload.layers.items()}
    registration_ns = time.monotonic_ns() - started
    started = time.monotonic_ns()
    task_id = post(client, "/api/tasks", {"goal": {"summary": f"Benchmark {workload.name}"}})["task_id"]
    draft = post(client, f"/api/tasks/{task_id}/plans", {
        "method": {"id": workload.method, "version": workload.version},
        "inputs": roles, "critical_options": workload.options,
    })
    plan = post(client, f"/api/plans/{draft['id']}/confirm", {"plan_version": draft["version"]})
    require(plan["task_id"] == task_id and plan["status"] == "confirmed"
            and len(plan["steps"]) == workload.expected_steps, "Unexpected confirmed plan")
    return plan, {"asset_registration_ns": registration_ns, "draft_and_confirmation_ns": time.monotonic_ns() - started}


def count_bytes(paths, excluded_inodes=()) -> int:
    inodes = {}
    for path in paths:
        item = path.stat(follow_symlinks=False)
        identity = (item.st_dev, item.st_ino)
        if identity not in excluded_inodes:
            inodes[identity] = item.st_size
    return sum(inodes.values())


def temporary_bytes(workdir: Path, formal_paths=()) -> int:
    # Run workspace has bounded depth; avoid repeated scanning of a large output tree.
    files = []
    entries = 0
    for root, directories, names in os.walk(workdir):
        if Path(root) == workdir and "outputs" in directories:
            directories.remove("outputs")
        entries += len(directories) + len(names)
        if entries > TEMP_ENTRY_LIMIT:
            raise ValueError("temporary_directory_entry_limit")
        files.extend(Path(root) / name for name in names)
    excluded_inodes = set()
    for target in formal_paths:
        try:
            item = (workdir / target).stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        excluded_inodes.add((item.st_dev, item.st_ino))
    return count_bytes(files, excluded_inodes)


def terminal_run(state: dict) -> bool:
    execution = state.get("execution_status")
    return execution in TERMINAL_EXECUTION and (
        execution != "SUCCEEDED" or state.get("check_status") in TERMINAL_CHECK
    )


def poll_run(client, run_id: str, workdir: Path, started: int, wait_seconds: float, sample: dict, *, formal_paths=()):
    deadline = time.monotonic() + wait_seconds
    last_temp = 0.0
    peak = None
    temp_samples = 0
    temp_error = None
    timeout = False
    status_retries = 0
    while True:
        response = client.get(f"/api/runs/{run_id}")
        now = time.monotonic()
        if response.is_error:
            detail = response.text[:STEP_LOG_LIMIT]
            sample.setdefault("status_read_errors", []).append({
                "http_status": response.status_code, "response": detail, "elapsed_ns": time.monotonic_ns() - started,
            })
            if response.status_code == 500 and response.json().get("detail", {}).get("code") == "STORAGE_FAILED":
                if status_retries < STATUS_READ_RETRIES and now < deadline:
                    status_retries += 1
                    time.sleep(POLL_SECONDS)
                    continue
            raise ValueError(f"GET /api/runs/{run_id}: HTTP {response.status_code}: {detail}")
        status_retries = 0
        state = response.json()
        sample["run_status"] = state
        terminal = terminal_run(state)
        if terminal:
            sample["wall_ns"] = time.monotonic_ns() - started
        if terminal or now - last_temp >= TEMP_SAMPLE_SECONDS:
            try:
                size = temporary_bytes(workdir, formal_paths)
                peak = max(peak or 0, size)
                temp_samples += 1
            except (OSError, ValueError) as error:
                temp_error = str(error)
            last_temp = now
        if terminal:
            break
        if now >= deadline:
            if timeout:
                raise TimeoutError(f"Run {run_id} remained active after cancellation")
            timeout = True
            sample["wait_timeout"] = wait_seconds
            sample["cancel_response"] = post(client, f"/api/runs/{run_id}/cancel", {})
            deadline = now + 10
        time.sleep(POLL_SECONDS)
    sample["peak_temp_bytes"] = {
        "value": peak, "status": ("partial" if temp_error else "sampled") if peak is not None else "unavailable",
        "samples": temp_samples, "reason": temp_error or ("no_active_observation" if peak is None else None),
    }


def collect_telemetry(workdir: Path, run_id: str) -> dict:
    path = workdir / SIDECAR_NAME
    deadline = time.monotonic() + 3
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not path.is_file():
        return {"status": "unavailable", "reason": "run_sidecar_not_published"}
    document = json.loads(path.read_text(encoding="utf-8"))
    require(document["schema_version"] == 1 and document["run_id"] == run_id, "Telemetry identity differs")
    return {"status": "recorded", "document": document}


def check_run(database, workdir, inputs, workload, reference, plan, run_id, sample):
    with open_database(database, read_only=True) as connection:
        run = get_run(connection, run_id)
        steps = {step.step_key: step for step in list_run_steps(connection, run_id)}
        artifacts = {item.role: item for item in list_artifacts(connection, run_id)}
    require(run is not None and run.plan_id == plan["id"] and run.plan_version == plan["version"]
            and run.workdir == str(workdir), "Run origin differs")
    sample["steps"] = [
        {"key": key, "status": step.status, "exit_code": step.result.exit_code if step.result else None,
         "tool": step.request.tool.model_dump(mode="json") if step.request else None,
         "parameters": step.request.parameters if step.request else None,
         "error": step.result.error.model_dump(mode="json") if step.result and step.result.error else None}
        for key, step in steps.items()
    ]
    require(run.execution_status == "SUCCEEDED" and run.check_status == "PASSED",
            f"Run state is {run.execution_status}/{run.check_status}")
    expected_steps = {step["key"]: step for step in plan["steps"]}
    require(list(steps) == list(expected_steps), "Run step order differs from confirmed plan")
    for key, step in steps.items():
        expected = expected_steps[key]
        require(step.status == "SUCCEEDED" and step.result.exit_code == 0, f"{key}: step failed")
        require(step.request.tool.model_dump(mode="json") == expected["tool"], f"{key}: tool version differs")
        require(step.request.parameters == expected["parameters"], f"{key}: parameters differ")
    expected_artifacts = {item["role"]: item for item in plan["expected_artifacts"]}
    require(artifacts.keys() == expected_artifacts.keys(), "Artifact roles differ")
    manifest = json.loads((workdir / "outputs/manifest.json").read_text(encoding="utf-8"))["artifacts"]
    require(manifest.keys() == artifacts.keys(), "Run manifest artifact roles differ")
    for role, artifact in artifacts.items():
        expected = expected_artifacts[role]
        source = artifact.source
        require(artifact.run_id == run_id and source.plan_id == run.plan_id and source.plan_version == run.plan_version
                and source.step_key == expected["step"] and source.output_name == expected["output"]
                and source.tool == steps[source.step_key].request.tool
                and source.inputs and all(plan["inputs"].get(key) == value.model_dump(mode="json")
                                          for key, value in source.inputs.items())
                and source.output_name in steps[source.step_key].result.outputs
                and steps[source.step_key].result.outputs[source.output_name] == artifact.output,
                f"{role}: artifact provenance differs")
        require(manifest[role] == {**artifact.output.model_dump(mode="json"), "fingerprint": artifact.fingerprint}
                and fingerprint_file(workdir / artifact.uri).digest == artifact.fingerprint,
                f"{role}: artifact fingerprint differs")
    report = json.loads((workdir / "checks/report.json").read_text(encoding="utf-8"))
    require(report["run_id"] == run_id and report["status"] == "PASSED"
            and list(report["rules"]) == plan["check_rules"] and all(rule["passed"] for rule in report["rules"].values()),
            "Persisted check report differs")
    sample["artifacts"] = {role: {"uri": item.uri, "layer": item.layer, "fingerprint": item.fingerprint,
                                "step": item.source.step_key, "output": item.source.output_name}
                           for role, item in artifacts.items()}
    sample["checks"] = {"report_sha256": file_record(workdir / "checks/report.json")["sha256"],
                        "rules": {name: rule["passed"] for name, rule in report["rules"].items()}}
    sample["output_bytes"] = count_bytes(workdir / item.uri for item in artifacts.values())
    validator = check_road if workload.name == "road" else check_t13 if workload.name.startswith("t13") else check_t14
    sample["correctness"] = validator(workdir, inputs, workload, reference, steps,
                                      {role: item.output for role, item in artifacts.items()})
    telemetry = sample["telemetry"].get("document")
    if telemetry:
        require(telemetry["execution_status"] == run.execution_status and telemetry["check_status"] == run.check_status
                and telemetry["exit_code"] == 0, "Sidecar status differs from persisted run")
        require([(step["step_key"], step["exit_code"]) for step in telemetry["steps"]]
                == [(key, step.result.exit_code) for key, step in steps.items()],
                "Sidecar steps differ from persisted steps")


def measure(client, database, workspace, inputs, workload, manifest, plan, wait_seconds, sample):
    package = inputs / workload.package
    sample["input"] = next(item for item in manifest["files"] if item["path"] == workload.package)
    sample["input_bytes"] = package.stat().st_size
    if sample["cache"] == "warm":
        started = time.monotonic_ns()
        with package.open("rb") as stream:
            for _ in iter(lambda: stream.read(1024 * 1024), b""):
                pass
        sample["input_preread_ns"] = time.monotonic_ns() - started
    started = time.monotonic_ns()
    sample["submission_attempted"] = True
    run_id = post(client, f"/api/plans/{plan['id']}/runs", {"plan_version": plan["version"]})["run_id"]
    sample["submission_ns"] = time.monotonic_ns() - started
    sample["run_id"] = run_id
    workdir = workspace / "tasks" / plan["task_id"] / "runs" / run_id
    sample["workdir"] = str(workdir)
    plan_steps = {step["key"]: step for step in plan["steps"]}
    formal_paths = [plan_steps[item["step"]]["outputs"][item["output"]]["target"] for item in plan["expected_artifacts"]]
    poll_run(client, run_id, workdir, started, wait_seconds, sample, formal_paths=formal_paths)
    sample["telemetry"] = collect_telemetry(workdir, run_id)
    sample["checks_only_ns"] = {"status": "unavailable", "reason": "sidecar_finalization_includes_artifact_registration"}
    check_started = time.monotonic_ns()
    try:
        check_run(database, workdir, inputs, workload, manifest["reference"], plan, run_id, sample)
    finally:
        sample["independent_verification_ns"] = time.monotonic_ns() - check_started


def collect_step_logs(workdir: Path) -> dict:
    excerpts = {}
    for log in sorted((workdir / "steps").glob("*/process.log")):
        try:
            with log.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - STEP_LOG_LIMIT))
                excerpt = stream.read(STEP_LOG_LIMIT).decode("utf-8", errors="replace")
            excerpts[str(log.relative_to(workdir))] = {"bytes": size, "truncated": size > STEP_LOG_LIMIT, "text": excerpt}
        except OSError as error:
            excerpts[str(log.relative_to(workdir))] = {"error": str(error)}
    return excerpts


def run_batch(path, inputs, workload, tier, cache, trials, qgis, wait_seconds, deadline, result_path):
    start = time.monotonic_ns()
    manifest = json.loads((inputs / "manifest.json").read_text(encoding="utf-8"))
    database, workspace = path / "metadata.sqlite3", path / "workspace"
    app = create_local_app(database, workspace, qgis_process=qgis)
    with TestClient(app) as client:
        service_ns = time.monotonic_ns() - start
        input_profile = manifest.get("input_profile", "p13-01")
        input_sha = file_record(inputs / "manifest.json")["sha256"]
        base_sample = {"tier": tier, "workload": workload.name, "cache": cache, "input_profile": input_profile,
                       "input_manifest_sha256": input_sha, "critical_options": workload.options}
        try:
            plan, setup = prepare_plan(client, database, inputs, workload)
        except ValueError as error:
            for index, (phase, repeat) in enumerate(trials):
                outcome = {"status": "NOT_RUN", "reason": "plan_setup_failed"}
                if index == 0:
                    outcome = {"status": "FAIL", "stage": "plan_setup", "error": str(error)}
                    print(tier, workload.name, cache, phase, repeat, "FAIL", error, flush=True)
                append_json(result_path, {**base_sample, "phase": phase, "repeat": repeat, **outcome})
            return
        uncertain_run = False
        for index, (phase, repeat) in enumerate(trials):
            sample = {**base_sample, "phase": phase, "repeat": repeat, "method": plan["method"],
                      "plan_id": plan["id"], "plan_version": plan["version"], "service_reused": index > 0}
            if uncertain_run or (deadline is not None and time.monotonic() >= deadline):
                reason = "previous_run_terminal_state_unknown" if uncertain_run else "budget_exhausted"
                append_json(result_path, {**sample, "status": "NOT_RUN", "reason": reason})
                continue
            if index == 0:
                sample["setup"] = {"service_and_reference_load_ns": service_ns, **setup}
            try:
                measure(client, database, workspace, inputs, workload, manifest, plan, wait_seconds, sample)
                sample["status"] = "PASS"
            except Exception as error:
                sample.update(status="FAIL", error=f"{type(error).__name__}: {error}")
                uncertain_run = sample.get("submission_attempted", False) and not terminal_run(sample.get("run_status", {}))
            if "workdir" in sample:
                sample["step_logs"] = collect_step_logs(Path(sample["workdir"]))
            append_json(result_path, sample)
            fields = [tier, workload.name, cache, phase, str(repeat), sample["status"]]
            if sample["status"] == "FAIL":
                fields.append(sample["error"])
            print(" ".join(fields), flush=True)


def nearest_rank_p95(values) -> int:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def set_distribution(record: dict, name: str, values: list, partial: int = 0) -> None:
    record[f"{name}_n"] = len(values)
    record[f"{name}_partial_n"] = partial
    record[f"{name}_median"] = statistics.median(values) if len(values) >= 3 else None
    record[f"{name}_p95"] = nearest_rank_p95(values) if len(values) >= 3 else None


def summarize(samples, repeats):
    groups = {}
    for sample in samples:
        key = sample["tier"], sample["workload"], sample["cache"], sample.get("input_profile", "p13-01")
        groups.setdefault(key, []).append(sample)
    report = []
    for (tier, workload, cache, input_profile), group in groups.items():
        measured = [sample for sample in group if sample["phase"] == "measurement"]
        correct = [sample for sample in measured if sample["status"] == "PASS"]
        summary = {"tier": tier, "workload": workload, "cache": cache,
                   "input_profile": input_profile,
                   "warmup_pass": sum(sample["phase"] == "warmup" and sample["status"] == "PASS" for sample in group),
                   "warmup_fail": sum(sample["phase"] == "warmup" and sample["status"] == "FAIL" for sample in group),
                   "warmup_not_run": sum(sample["phase"] == "warmup" and sample["status"] == "NOT_RUN" for sample in group),
                   "measured_pass": len(correct), "measured_fail": sum(sample["status"] == "FAIL" for sample in measured),
                   "measured_not_run": sum(sample["status"] == "NOT_RUN" for sample in measured),
                   "probe_pass": sum(sample["phase"] == "probe" and sample["status"] == "PASS" for sample in group),
                   "measured_pass_with_status_read_errors": sum(bool(sample.get("status_read_errors")) for sample in correct)}
        summary["status"] = "COMPLETE" if len(correct) >= repeats else "PARTIAL" if len(correct) >= 3 else "INSUFFICIENT"
        metrics = {name: [] for name in ("wall_ns", "queue_wall_ns", "execute_steps_wall_ns", "finalize_wall_ns",
                                        *RESOURCES, "peak_temp_bytes", "input_bytes", "output_bytes")}
        partial = {name: 0 for name in metrics}
        for sample in correct:
            sidecar = sample["telemetry"].get("document", {})
            metrics_with_status = {**sidecar.get("resources", {}), "peak_temp_bytes": sample["peak_temp_bytes"]}
            for name, reading in metrics_with_status.items():
                if reading.get("status") == "partial":
                    partial[name] += 1
            values = {
                "wall_ns": sample["wall_ns"], "input_bytes": sample["input_bytes"], "output_bytes": sample["output_bytes"],
                "peak_temp_bytes": sample["peak_temp_bytes"]["value"],
                "queue_wall_ns": sidecar.get("queue_wall_ns", {}).get("value"),
                "execute_steps_wall_ns": sidecar.get("execute_steps_wall_ns"),
                "finalize_wall_ns": sidecar.get("finalize_wall_ns"),
                **{name: sidecar.get("resources", {}).get(name, {}).get("value") for name in RESOURCES},
            }
            for name, value in values.items():
                if value is not None:
                    metrics[name].append(value)
        for name, values in metrics.items():
            set_distribution(summary, name, values, partial[name])
        report.append(summary)
    return report


def summarize_steps(samples: list) -> list:
    groups = {}
    for sample in samples:
        if sample["phase"] != "measurement" or sample["status"] != "PASS":
            continue
        for step in sample["telemetry"].get("document", {}).get("steps", []):
            tool = step["tool"]
            key = (sample["tier"], sample["workload"], sample["cache"], sample.get("input_profile", "p13-01"),
                   step["step_key"], tool["id"], tool["version"])
            groups.setdefault(key, []).append(step)
    rows = []
    for (tier, workload, cache, input_profile, step_key, tool, version), steps in groups.items():
        row = {"tier": tier, "workload": workload, "cache": cache, "input_profile": input_profile, "step": step_key,
               "tool": tool, "tool_version": version}
        set_distribution(row, "wall_ns", [step["wall_ns"] for step in steps])
        for name in RESOURCES:
            readings = [step["resources"][name] for step in steps]
            set_distribution(row, name, [item["value"] for item in readings if item["value"] is not None],
                             sum(item["status"] == "partial" for item in readings))
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        if rows:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def verify_inputs(path: Path, manifest: dict):
    for record in manifest["files"]:
        require(file_record(path / record["path"]) == record, f"Input {record['path']} changed")


def execute(args) -> int:
    protocol = read_yaml(ROOT, "benchmarks/manifest.yaml")["measurement"]
    repeats, warmups = protocol["repeats_per_cache_state"], protocol["warmup_runs"]
    caches = args.cache_state or (["warm"] if args.probe else protocol["cache_states"])
    trials = [("probe", 1)] if args.probe else (
        [("warmup", index + 1) for index in range(warmups)] +
        [("measurement", index + 1) for index in range(repeats)]
    )
    output = args.output
    output.mkdir(parents=True)
    environment = collect_environment(args.qgis_process, args.source_commit)
    environment.update({
        "started_utc": datetime.now(UTC).isoformat(),
        "argv": [sys.executable, *sys.argv],
        "protocol_sha256": file_record(ROOT / "benchmarks/manifest.yaml")["sha256"],
        "profile_sha256": file_record(args.profile)["sha256"],
        "runner_sha256": {path.name: file_record(path)["sha256"] for path in (
            Path(__file__), Path(__file__).with_name("_vector_reference.py"),
            Path(__file__).with_name("_vector_road_profile.py"),
        )},
        "road_profile_sha256": file_record(args.road_profile)["sha256"] if args.road_profile else None,
        "storage_filesystem": command_output(["stat", "-f", "-c", "%T", str(output)]),
        "cache_state": {"os_page_cache": "uncontrolled", "process_cold": "fresh process and project",
                        "warm": "reused process, project and confirmed plan; input file pre-read"},
        "poll_seconds": POLL_SECONDS, "temp_sample_seconds": TEMP_SAMPLE_SECONDS,
        "temp_entry_limit": TEMP_ENTRY_LIMIT, "probe": args.probe,
        "workspace_retention": "temporary_directory_removed_after_worker_exit",
        "step_log_limit_bytes": STEP_LOG_LIMIT, "status_read_retries": STATUS_READ_RETRIES,
        "planned_repeats": repeats, "planned_warmups": warmups,
        "budget_seconds": args.budget_seconds, "run_wait_seconds": args.wait_seconds,
    })
    write_json(output / "environment.json", environment)
    require(environment["qgis_version"]["exit_code"] == 0, "QGIS version command failed")
    all_workloads = workloads()
    evidence = output / "samples.jsonl"
    deadline = time.monotonic() + args.budget_seconds if args.budget_seconds is not None else None
    dataset_records = {}
    context = multiprocessing.get_context("spawn")
    for tier in args.tier:
        input_dir = output / f"input-{tier}"
        start = time.monotonic_ns()
        manifest = generate(ROOT, tier, input_dir)
        generation_ns = time.monotonic_ns() - start
        verify_inputs(input_dir, manifest)
        dataset_records[tier] = {"generation_ns": generation_ns, "files_before": manifest["files"],
                                 "input_manifest_sha256": file_record(input_dir / "manifest.json")["sha256"]}
        print(f"{tier} generated {manifest['reference']['tiles']} tiles in {generation_ns / 1e9:.3f}s", flush=True)
        inputs = {name: (input_dir, manifest, dataset_records[tier]) for name in args.workload}
        if tier == "L" and args.road_profile is not None:
            corrected_dir = output / "input-L-road-corrected"
            start = time.monotonic_ns()
            corrected = generate_corrected_road(ROOT, input_dir, corrected_dir, args.road_profile)
            corrected_ns = time.monotonic_ns() - start
            verify_inputs(corrected_dir, corrected)
            road_record = {"generation_ns": corrected_ns, "files_before": corrected["files"],
                           "input_profile": corrected["input_profile"],
                           "input_manifest_sha256": file_record(corrected_dir / "manifest.json")["sha256"]}
            dataset_records[tier]["road_position_correction"] = road_record
            inputs["road"] = corrected_dir, corrected, road_record
            print(f"L generated corrected road package in {corrected_ns / 1e9:.3f}s", flush=True)
        for name, (workload_input, input_manifest, record) in inputs.items():
            for cache in caches:
                batches = [[trial] for trial in trials] if cache == "process_cold" else [trials]
                for batch in batches:
                    offset = evidence.stat().st_size if evidence.is_file() else 0
                    reason = "budget_exhausted"
                    if deadline is None or time.monotonic() < deadline:
                        with tempfile.TemporaryDirectory(prefix=f".{tier}-{name}-{cache}-", dir=output) as workdir:
                            worker = context.Process(target=run_batch, args=(
                                Path(workdir), workload_input, all_workloads[name], tier, cache, batch, args.qgis_process,
                                args.wait_seconds, deadline, evidence,
                            ))
                            worker.start()
                            worker.join()
                            reason = f"worker_exit_{worker.exitcode}"
                            if worker.exitcode:
                                print(tier, name, cache, reason, flush=True)
                    completed = set()
                    if evidence.is_file():
                        with evidence.open("rb") as stream:
                            stream.seek(offset)
                            completed = {(sample["phase"], sample["repeat"]) for sample in map(json.loads, stream)}
                    for phase, repeat in batch:
                        if (phase, repeat) not in completed:
                            append_json(evidence, {
                                "tier": tier, "workload": name, "cache": cache, "phase": phase, "repeat": repeat,
                                "input_profile": input_manifest.get("input_profile", "p13-01"),
                                "input_manifest_sha256": record["input_manifest_sha256"],
                                "status": "NOT_RUN" if reason == "budget_exhausted" else "FAIL", "reason": reason,
                            })
        input_records = [(input_dir, manifest, dataset_records[tier])]
        if tier == "L" and args.road_profile is not None:
            input_records.append(inputs["road"])
        for package, input_manifest, record in input_records:
            verify_inputs(package, input_manifest)
            record["files_after"] = [file_record(package / item["path"]) for item in input_manifest["files"]]
            require(file_record(package / "manifest.json")["sha256"] == record["input_manifest_sha256"],
                    f"{package.name} manifest changed")
        write_json(output / "inputs.json", dataset_records)
    with evidence.open(encoding="utf-8") as stream:
        samples = [json.loads(line) for line in stream]
    groups = summarize(samples, repeats)
    steps = summarize_steps(samples)
    write_csv(output / "summary.csv", groups)
    write_csv(output / "step-summary.csv", steps)
    success = all(sample["status"] == "PASS" for sample in samples)
    report = {"status": "PASS" if success else "INCOMPLETE", "finished_utc": datetime.now(UTC).isoformat(),
              "finished_load_average": read_text("/proc/loadavg"),
              "finished_io_pressure": read_text("/proc/pressure/io"),
              "probe": args.probe, "sample_counts": {status: sum(sample["status"] == status for sample in samples)
                                                   for status in ("PASS", "FAIL", "NOT_RUN")},
              "groups": groups, "step_groups": steps}
    write_json(output / "summary.json", report)
    print(json.dumps({key: report[key] for key in ("status", "probe", "sample_counts")}), flush=True)
    return 0 if success else 1


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number of seconds")
    return seconds


def main(argv=None) -> int:
    available = list(workloads())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path(PROFILE))
    parser.add_argument("--road-profile", type=Path, help="Use the versioned L road position correction")
    parser.add_argument("--output", type=Path, help="New evidence and input directory")
    parser.add_argument("--qgis-process", type=Path, default=shutil.which("qgis_process"))
    parser.add_argument("--source-commit")
    parser.add_argument("--tier", action="append", choices=TIERS)
    parser.add_argument("--workload", action="append", choices=available)
    parser.add_argument("--cache-state", action="append", choices=("process_cold", "warm"))
    parser.add_argument("--probe", action="store_true", help="One run per selected group; excludes all measurements")
    parser.add_argument("--wait-seconds", type=positive_seconds, default=600)
    parser.add_argument("--budget-seconds", type=positive_seconds, help="Stop starting samples at the elapsed limit")
    args = parser.parse_args(argv)
    args.profile = args.profile.resolve()
    if args.profile != (ROOT / PROFILE).resolve():
        parser.error(f"--profile must refer to the accepted {PROFILE}")
    args.tier = list(dict.fromkeys(args.tier or TIERS))
    args.workload = list(dict.fromkeys(args.workload or available))
    if args.road_profile is not None:
        args.road_profile = args.road_profile.resolve()
        if args.road_profile != (ROOT / ROAD_PROFILE).resolve():
            parser.error(f"--road-profile must refer to {ROAD_PROFILE}")
        if "L" not in args.tier or "road" not in args.workload:
            parser.error("--road-profile requires tier L and the road workload")
    if args.cache_state:
        args.cache_state = list(dict.fromkeys(args.cache_state))
    if args.qgis_process is None:
        parser.error("qgis_process is unavailable; pass --qgis-process")
    # Keep the original executable name; its script may select an adjacent binary.
    args.qgis_process = args.qgis_process.absolute()
    if not args.qgis_process.is_file() or not os.access(args.qgis_process, os.X_OK):
        parser.error("--qgis-process must be an executable file")
    if args.output is None:
        args.output = Path(tempfile.mkdtemp(prefix="geopilot-vector-benchmark-")) / "evidence"
    args.output = args.output.absolute()
    if args.output.exists() or args.output.is_symlink():
        parser.error("--output must be a new directory")
    os.environ["GEOPILOT_TELEMETRY"] = "1"
    print(f"Evidence directory: {args.output}", flush=True)
    try:
        return execute(args)
    except (OSError, ValueError) as error:
        print(f"vector benchmark: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
