"""Independently audit raw P13-03 L continuation records and the final summary."""
import argparse
from collections import Counter, defaultdict
import gzip
from hashlib import sha256
import json
from pathlib import Path
import sys

WORKLOADS = (
    "t13-points-intersects", "t13-points-within", "t13-lines-intersects",
    "t13-lines-within", "t13-polygons-within", "t14-centroid", "t14-largest_overlap",
)
CACHES = ("process_cold", "warm")
SLOTS = [("warmup", n) for n in range(1, 3)] + [("measurement", n) for n in range(1, 6)]
ENVIRONMENT_DIFFERENCES = {"argv", "cgroup_membership", "io_pressure", "load_average", "road_profile_sha256", "started_utc"}
RESOURCES = ("cpu_ns", "peak_rss_bytes", "disk_read_bytes", "disk_write_bytes")


def records(path, preview):
    raw = path.read_bytes()
    data = gzip.decompress(raw) if path.suffix == ".gz" else raw
    if not data.endswith(b"\n"):
        if not preview:
            raise ValueError("Final sample stream is truncated")
        data = data[:data.rfind(b"\n") + 1]
    return [json.loads(line) for line in data.splitlines()], sha256(data).hexdigest()


def observed_resources(rows):
    result = {}
    for name in (*RESOURCES, "peak_temp_bytes"):
        values = [row["peak_temp_bytes"] if name == "peak_temp_bytes"
                  else row["telemetry"]["document"]["resources"][name] for row in rows]
        assert all(item["status"] in {"sampled", "partial", "unavailable"} for item in values), name
        assert all((item["value"] is None) == (item["status"] == "unavailable") for item in values), name
        numeric = [item["value"] for item in values if item["value"] is not None]
        result[name] = {
            "coverage": dict(Counter(item["status"] for item in values)),
            "observed_range": [min(numeric), max(numeric)] if numeric else None,
        }
    return result


def check_run(row, input_files, manifest_sha):
    assert row["tier"] == "L" and row["input_profile"] == "p13-01"
    assert row["input_manifest_sha256"] == manifest_sha
    assert row["input"] == input_files[row["input"]["path"]] and row["input_bytes"] == row["input"]["bytes"]
    assert row["submission_attempted"] and not row.get("status_read_errors")
    assert row["independent_verification_ns"] > 0 and row["wall_ns"] > 0
    assert row["status"] == "FAIL" and not {"correctness", "checks", "artifacts"} & row.keys()
    state = row["run_status"]
    assert (state["run_id"], state["execution_status"], state["check_status"]) == (row["run_id"], "FAILED", "NOT_CHECKED")
    assert row["telemetry"]["status"] == "recorded"
    telemetry = row["telemetry"]["document"]
    assert (telemetry["run_id"], telemetry["execution_status"], telemetry["check_status"]) == (
        row["run_id"], "FAILED", "NOT_CHECKED"
    )
    workload = row["workload"]
    point_case = workload.startswith("t13-points-")
    steps = row["steps"]
    step_keys = ["apply_spatial_relation", "clip_back_to_analysis_extent"]
    if workload.startswith("t14-"):
        step_keys.append("aggregate_by_zone")
        assert row["method"] == {"id": "polygon-zone-statistics", "version": 1}
        assert row["critical_options"] == {
            "spatial_relation": "intersects", "boundary_assignment": workload.removeprefix("t14-"),
            "boundary_tie": "unassigned", "area_crs": "EPSG:6933",
        }
    else:
        assert row["method"] == {"id": "spatial-select-and-clip", "version": 1}
        assert row["critical_options"] == {"spatial_relation": workload.rsplit("-", 1)[-1]}
    assert [step["key"] for step in steps] == step_keys, f"Missing or unexpected planned steps for {row['run_id']}"
    assert steps[0]["parameters"] == {"spatial_relation": row["critical_options"]["spatial_relation"]}
    expected_step = "clip_back_to_analysis_extent" if point_case else "apply_spatial_relation"
    assert (state["current_step"], state["error"]["step_key"], state["error"]["code"]) == (
        expected_step, expected_step, "PROCESS_TIMEOUT"
    )
    timeout_index = int(point_case)
    if point_case:
        assert (steps[0]["status"], steps[0]["exit_code"]) == ("SUCCEEDED", 0)
    failed = steps[timeout_index]
    assert (failed["key"], failed["status"], failed["exit_code"]) == (expected_step, "FAILED", -15)
    assert all((step["status"], step["exit_code"]) == ("NOT_STARTED", None) for step in steps[timeout_index + 1:])
    assert [(step["step_key"], step["exit_code"]) for step in telemetry["steps"]] == [
        (step["key"], step["exit_code"]) for step in steps[:timeout_index + 1]
    ]
    processes = telemetry["steps"][-1]["processes"]
    assert processes and (processes[-1]["timed_out"], processes[-1]["exit_code"]) == (True, -15), (
        f"Missing process timeout evidence for {row['run_id']}"
    )


def audit(args):
    rows, stream_sha = records(args.samples, args.preview)
    expected_keys = [(workload, cache, *slot) for workload in WORKLOADS for cache in CACHES for slot in SLOTS]
    actual_keys = [(row["workload"], row["cache"], row["phase"], row["repeat"]) for row in rows]
    assert rows and actual_keys == expected_keys[:len(rows)], "Samples do not match the planned sequence"
    if not args.preview:
        assert len(rows) == len(expected_keys), "Final audit requires all 98 records"
    initial, environment = (json.loads(path.read_bytes()) for path in (args.initial_environment, args.environment))
    differences = sorted(key for key in initial.keys() | environment.keys() if initial.get(key) != environment.get(key))
    assert set(differences) <= ENVIRONMENT_DIFFERENCES, differences
    manifest = args.manifest.read_bytes()
    manifest_sha = sha256(manifest).hexdigest()
    assert manifest == args.initial_manifest.read_bytes(), "L input manifests differ"
    input_files = {entry["path"]: entry for entry in json.loads(manifest)["files"]}
    initial_rows, _ = records(args.initial_samples, False)
    run_ids = {row["run_id"] for row in initial_rows}
    assert len(run_ids) == len(initial_rows) == 42
    groups = defaultdict(list)
    for row in rows:
        assert row["run_id"] not in run_ids, f"Duplicate run {row['run_id']}"
        run_ids.add(row["run_id"])
        check_run(row, input_files, manifest_sha)
        groups[(row["workload"], row["cache"])].append(row)
    results = []
    for (workload, cache), group in groups.items():
        if cache == "process_cold":
            assert len({row["plan_id"] for row in group}) == len(group)
            assert not any(row["service_reused"] for row in group)
        else:
            assert len({row["plan_id"] for row in group}) == 1
            assert not group[0]["service_reused"] and all(row["service_reused"] for row in group[1:])
            assert all(row["input_preread_ns"] > 0 for row in group)
        measurement = [row for row in group if row["phase"] == "measurement"]
        wall_ns = [row["wall_ns"] for row in measurement]
        results.append({
            "workload": workload, "cache": cache, "complete": len(group) == len(SLOTS),
            "warmup_fail": len(group) - len(measurement), "measured_fail": len(measurement),
            "failed_step": group[0]["run_status"]["current_step"],
            "failed_measurement_wall_ns_range": [min(wall_ns), max(wall_ns)] if wall_ns else None,
            "failed_measurement_resources": observed_resources(measurement),
        })
    if not args.preview:
        summary = json.loads(args.summary.read_bytes())
        assert not summary["probe"] and summary["status"] == "INCOMPLETE"
        assert summary["sample_counts"] == {"PASS": 0, "FAIL": len(rows), "NOT_RUN": 0}
        assert not summary["step_groups"], "There are no successful step distributions in this batch"
        index = {(item["workload"], item["cache"]): item for item in summary["groups"]}
        assert len(index) == len(summary["groups"]) == len(groups)
        for result in results:
            item = index[(result["workload"], result["cache"])]
            assert item["status"] == "INSUFFICIENT" and item["input_profile"] == "p13-01"
            assert (item["warmup_fail"], item["measured_fail"]) == (result["warmup_fail"], result["measured_fail"])
            assert all(not item[field] for field in (
                "warmup_pass", "warmup_not_run", "measured_pass", "measured_not_run", "probe_pass"
            ))
            assert all(value is None for key, value in item.items() if key.endswith(("_median", "_p95")))
    return {
        "status": "PREVIEW" if args.preview else "PASS_L_CONTINUATION_AUDIT",
        "source_commit": environment["source_commit"], "complete_groups": sum(row["complete"] for row in results),
        "observed_records": len(rows), "planned_records": len(expected_keys),
        "unique_run_ids_in_both_L_batches": len(run_ids), "input_manifest_sha256": manifest_sha,
        "raw_sample_prefix_sha256": stream_sha, "allowed_environment_differences": differences, "groups": results,
    }


parser = argparse.ArgumentParser(description=__doc__)
for name in ("samples", "environment", "manifest", "initial-samples", "initial-environment", "initial-manifest"):
    parser.add_argument(f"--{name}", type=Path, required=True)
mode = parser.add_mutually_exclusive_group(required=True)
mode.add_argument("--preview", action="store_true")
mode.add_argument("--summary", type=Path)
args = parser.parse_args()
try:
    print(json.dumps(audit(args), ensure_ascii=False, indent=2))
except (AssertionError, KeyError, OSError, TypeError, ValueError) as error:
    print(f"L continuation audit failed: {error}", file=sys.stderr)
    raise SystemExit(1) from error
