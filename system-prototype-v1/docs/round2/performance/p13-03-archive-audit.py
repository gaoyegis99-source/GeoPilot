from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from statistics import median
import csv
import gzip
import json
import math
import shlex
import sys


EVIDENCE = Path(sys.argv[1]).resolve()
BATCHES = {
    "S": ("S", {"PASS": 140, "FAIL": 0, "NOT_RUN": 0}),
    "M": ("M", {"PASS": 115, "FAIL": 0, "NOT_RUN": 25}),
    "M-cont": ("M", {"PASS": 28, "FAIL": 0, "NOT_RUN": 0}),
    "L-formal": ("L", {"PASS": 0, "FAIL": 42, "NOT_RUN": 0}),
}
SOURCE = "37cb329be04eaddfeb23ac62789926d0fcdb63aa"
IMAGE = "sha256:c618d0d9d91d0cf473d61e5fc284aa335e87caabd51381b73cd61b2d0f93cb87"
RESOURCES = ("cpu_ns", "peak_rss_bytes", "disk_read_bytes", "disk_write_bytes")
WORKLOADS = {
    *(f"t13-{g}-{r}" for g in ("points", "lines", "polygons") for r in ("intersects", "within")),
    *(f"t14-{m}" for m in ("split_by_area", "centroid", "largest_overlap")),
    "road",
}
CACHES = {"process_cold", "warm"}
TRIALS = {("warmup", i) for i in range(1, 3)} | {("measurement", i) for i in range(1, 6)}
L_FIRST = {"road", "t13-polygons-intersects", "t14-split_by_area"}
M_CONT = {"road", "t14-largest_overlap"}
RUNS = set()
ENVIRONMENTS = {}
BATCH_GROUPS = {}
SUMMARY_GROUPS = {}


def path(batch, suffix):
    return EVIDENCE / f"p13-03-{batch}-{suffix}"


def load(batch, suffix):
    return json.loads(path(batch, suffix).read_bytes())


def identity(data):
    return (data["tier"], data["workload"], data["cache"], data.get("input_profile", "p13-01"))


def distribution(values):
    values = sorted(values)
    if len(values) < 3:
        return None, None
    return median(values), values[math.ceil(.95 * len(values)) - 1]


def compare_metrics(summary, name, observed, partial):
    central, tail = distribution(observed)
    actual = tuple(summary[f"{name}_{key}"] for key in ("n", "partial_n", "median", "p95"))
    assert actual == (len(observed), partial, central, tail), (identity(summary), name, actual, observed)


def vector_result(sample, reference):
    workload = sample["workload"]
    actual = sample["correctness"]
    if workload.startswith("t13-"):
        _, geom, rel = workload.split("-")
        expected = reference["t13"]["relations"][rel][geom]
        assert actual["selected"] == actual["clipped"] == reference["tiles"] * len(expected["per_tile_ids"])
        assert math.isclose(actual["retained_line_length_m"], expected["retained_line_length_m"], abs_tol=1e-6)
        assert math.isclose(actual["retained_area_m2"], expected["retained_area_m2"], abs_tol=1e-6)
    elif workload.startswith("t14-"):
        case = reference["t14"]
        expected = case["methods"][workload.removeprefix("t14-")]
        assert actual["counts"] == expected["counts"] and actual["areas_m2"] == expected["areas_m2"]
        assert (actual["selected"], actual["clipped"]) == (case["selected_rows"], case["clipped_rows"])
        assert actual["allocation_rows"] == reference["tiles"] * len(expected["details_per_tile"])
    else:
        road = reference["road"]
        expected = road["relations"][sample["critical_options"]["spatial_relation"]]
        tiles = reference["tiles"]
        assert actual["counts"] == expected["total_counts"]
        assert actual["road_candidates"] == tiles * len(road["candidate_road_ids"])
        assert actual["targets_before_clip"] == tiles * len(expected["before_clip_target_ids"])
        assert actual["targets_after_clip"] == tiles * len(expected["clipped_target_ids"])
        assert actual["buffer"]["max_boundary_hausdorff_m"] <= 1e-6


def record_failures(rows):
    info = {}
    for key, members in rows.items():
        key = "/".join(key)
        samples = []
        for row in members:
            assert row["status"] == "FAIL" and row["telemetry"]["status"] == "recorded"
            state = row["run_status"]
            assert state["execution_status"] == "FAILED" and state["check_status"] == "NOT_CHECKED"
            assert not {"correctness", "checks", "artifacts"} & row.keys()
            code = state["error"]["code"]
            steps = row["steps"]
            if row["workload"] == "road":
                assert (code, state["error"]["field"]) == ("OUTPUT_INVALID", "artifacts.zone_statistics.path")
                assert "field larger than field limit (131072)" in state["error"]["message"]
                assert len(steps) == 7 and all((s["status"], s["exit_code"]) == ("SUCCEEDED", 0) for s in steps)
                assert "98304 retained target(s)" in row["step_logs"]["steps/aggregate_by_zone/process.log"]["text"]
            else:
                assert code == "PROCESS_TIMEOUT"
                assert state["current_step"] == "apply_spatial_relation"
                assert (steps[0]["key"], steps[0]["status"], steps[0]["exit_code"]) == ("apply_spatial_relation", "FAILED", -15)
                assert all(s["status"] == "NOT_STARTED" for s in steps[1:])
                process = row["telemetry"]["document"]["steps"][0]["processes"][-1]
                assert process["timed_out"] and process["exit_code"] == -15
            resources = row["telemetry"]["document"]["resources"]
            samples.append((row["wall_ns"], resources["peak_rss_bytes"]["value"]))
        info[key] = {"state": code, "wall_seconds_range": [min(x[0] for x in samples)/1e9, max(x[0] for x in samples)/1e9],
                     "observed_rss_mib_range": [min(x[1] for x in samples)/2**20, max(x[1] for x in samples)/2**20]}
    return info


def check_csv(batch, name, expected):
    csv_path = path(batch, name)
    if not expected and not csv_path.exists():
        return
    with csv_path.open(newline="", encoding="utf-8") as stream:
        observed = list(csv.DictReader(stream))
    serialized = [{key: "" if value is None else str(value) for key, value in row.items()} for row in expected]
    assert observed == serialized, (batch, name)


def audit_batch(batch, tier, expected_counts):
    summary = load(batch, "summary.json")
    env = load(batch, "environment.json")
    env_command = path(batch, "command.log").read_text().splitlines()
    assert IMAGE in shlex.split(env_command[0])
    assert f"--source-commit {SOURCE}" in env_command[0]
    assert all(digest in "\n".join(env_command[1:]) for digest in env["runner_sha256"].values())
    assert any(line == "/dev/sdb1 ext4" for line in env_command[1:])
    assert env["source_commit"] == SOURCE and env["qgis_version"]["exit_code"] == 0
    assert "QGIS 3.44.15" in env["qgis_version"]["stdout"]
    assert (env["planned_warmups"], env["planned_repeats"], env["probe"]) == (2, 5, False)
    ENVIRONMENTS[batch] = env
    input_meta = load(batch, "inputs.json")[tier]
    manifest_bytes = path(tier, "input-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert sha256(manifest_bytes).hexdigest() == input_meta["input_manifest_sha256"]
    assert input_meta["files_before"] == input_meta["files_after"] == manifest["files"]
    selected = {"p13-01": (input_meta, manifest)}
    if batch == "L-formal":
        road_meta = input_meta["road_position_correction"]
        road_bytes = path(tier, "road-input-manifest.json").read_bytes()
        road = json.loads(road_bytes)
        assert sha256(road_bytes).hexdigest() == road_meta["input_manifest_sha256"]
        assert road_meta["files_before"] == road_meta["files_after"] == road["files"]
        assert road["base_input_manifest_sha256"] == input_meta["input_manifest_sha256"]
        assert road["translation_xy_m"] == [0.0, -264000.0]
        selected[road_meta["input_profile"]] = road_meta, road
    with gzip.open(path(batch, "samples.jsonl.gz"), "rt", encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    counts = Counter(row["status"] for row in rows)
    assert counts == Counter(expected_counts) and summary["sample_counts"] == expected_counts and not summary["probe"]
    assert summary["status"] == ("PASS" if counts["PASS"] == len(rows) else "INCOMPLETE")
    groups = defaultdict(list)
    for row in rows:
        assert row["tier"] == tier
        groups[identity(row)].append(row)
        if row["status"] == "NOT_RUN":
            assert row["reason"] == "budget_exhausted" and "run_id" not in row and not row.get("submission_attempted")
            continue
        assert row["run_id"] not in RUNS and row["run_status"]["run_id"] == row["run_id"] and row["submission_attempted"]
        RUNS.add(row["run_id"])
        assert row["independent_verification_ns"] > 0 and not row.get("status_read_errors")
        assert row["telemetry"]["status"] == "recorded"
        assert row["telemetry"]["document"]["run_id"] == row["run_id"]
        meta, fixture = selected[row.get("input_profile", "p13-01")]
        if "input_manifest_sha256" in row:
            assert row["input_manifest_sha256"] == meta["input_manifest_sha256"]
        input_file = next(x for x in fixture["files"] if x["path"] == row["input"]["path"])
        assert row["input"] == input_file and row["input_bytes"] == input_file["bytes"]
        if row["status"] == "PASS":
            assert (row["run_status"]["execution_status"], row["run_status"]["check_status"]) == ("SUCCEEDED", "PASSED")
            assert row["checks"]["rules"] and all(row["checks"]["rules"].values()) and row["artifacts"]
            assert all((s["status"], s["exit_code"]) == ("SUCCEEDED", 0) for s in row["steps"])
            assert [(s["step_key"], s["exit_code"]) for s in row["telemetry"]["document"]["steps"]] == [
                (s["key"], s["exit_code"]) for s in row["steps"]
            ]
            vector_result(row, fixture["reference"])
    expected_workloads = M_CONT if batch == "M-cont" else L_FIRST if batch == "L-formal" else WORKLOADS
    assert {(key[1], key[2]) for key in groups} == {(workload, cache) for workload in expected_workloads for cache in CACHES}
    by_group = {identity(row): row for row in summary["groups"]}
    assert by_group.keys() == groups.keys()
    steps = defaultdict(list)
    for key, members in groups.items():
        assert len(members) == 7 and {(r["phase"], r["repeat"]) for r in members} == TRIALS
        started = [r for r in members if r["status"] != "NOT_RUN"]
        if key[2] == "process_cold":
            assert len({r["plan_id"] for r in started}) == len(started) and all(not r["service_reused"] for r in started)
        elif started:
            assert len({r["plan_id"] for r in started}) == 1 and not started[0]["service_reused"]
            assert all(r["service_reused"] for r in started[1:]) and all(r["input_preread_ns"] > 0 for r in started)
        group_summary = by_group[key]
        for period, name in (("warmup", "warmup"), ("measurement", "measured")):
            for status in ("PASS", "FAIL", "NOT_RUN"):
                assert group_summary[f"{name}_{status.lower()}"] == sum(r["phase"] == period and r["status"] == status for r in members)
        correct = [r for r in members if r["phase"] == "measurement" and r["status"] == "PASS"]
        assert group_summary["status"] == ("COMPLETE" if len(correct) == 5 else "INSUFFICIENT")
        assert group_summary["probe_pass"] == group_summary["measured_pass_with_status_read_errors"] == 0
        top_fields = ("wall_ns", "input_bytes", "output_bytes")
        for field in top_fields:
            compare_metrics(group_summary, field, [r[field] for r in correct], 0)
        telemetry = [r["telemetry"]["document"] for r in correct]
        for field in ("execute_steps_wall_ns", "finalize_wall_ns"):
            compare_metrics(group_summary, field, [r[field] for r in telemetry if r[field] is not None], 0)
        queue = [r["queue_wall_ns"]["value"] for r in telemetry if r["queue_wall_ns"]["value"] is not None]
        compare_metrics(group_summary, "queue_wall_ns", queue, 0)
        for field in (*RESOURCES, "peak_temp_bytes"):
            readings = [r["peak_temp_bytes"] if field == "peak_temp_bytes" else r["telemetry"]["document"]["resources"][field]
                        for r in correct]
            compare_metrics(group_summary, field, [r["value"] for r in readings if r["value"] is not None],
                            sum(r["status"] == "partial" for r in readings))
        for record in telemetry:
            for step in record["steps"]:
                steps[(*key, step["step_key"], step["tool"]["id"], step["tool"]["version"])].append(step)
    by_step = {(*identity(row), row["step"], row["tool"], row["tool_version"]): row for row in summary["step_groups"]}
    assert by_step.keys() == steps.keys()
    for key, measured in steps.items():
        compare_metrics(by_step[key], "wall_ns", [r["wall_ns"] for r in measured], 0)
        for field in RESOURCES:
            observed = [r["resources"][field] for r in measured]
            compare_metrics(by_step[key], field, [r["value"] for r in observed if r["value"] is not None],
                            sum(r["status"] == "partial" for r in observed))
    check_csv(batch, "summary.csv", summary["groups"])
    check_csv(batch, "step-summary.csv", summary["step_groups"])
    BATCH_GROUPS[batch], SUMMARY_GROUPS[batch] = groups, by_group
    return {
        "records": len(rows), "counts": dict(expected_counts), "groups": len(groups), "step_groups": len(steps),
        "phases": {phase: dict(Counter(row["status"] for row in rows if row["phase"] == phase)) for phase in ("warmup", "measurement")},
        "output_checks_passed": counts["PASS"], "independent_geometry_checks_passed": counts["PASS"],
        "largest_successful_step_medians": [
            {"workload": r["workload"], "cache": r["cache"], "step": r["step"], "seconds": r["wall_ns_median"]/1e9}
            for r in sorted((r for r in summary["step_groups"] if r["wall_ns_median"] is not None),
                            key=lambda r: r["wall_ns_median"], reverse=True)[:3]
        ],
        "failure_hotspots": record_failures(groups) if batch == "L-formal" else {},
    }


audit = {name: audit_batch(name, tier, counts) for name, (tier, counts) in BATCHES.items()}
assert len(RUNS) == 325
assert (ENVIRONMENTS["S"]["runner_sha256"] == ENVIRONMENTS["M"]["runner_sha256"] ==
        ENVIRONMENTS["M-cont"]["runner_sha256"])
same_fields = ("cpu_models", "physical_core_count", "logical_cpu_count", "platform", "os_release", "qgis_process_sha256",
               "qgis_process_binary_sha256", "python", "python_packages", "proj", "pyogrio_gdal", "geos",
               "profile_sha256", "protocol_sha256", "thread_and_cache_settings")
for field in same_fields:
    first = ENVIRONMENTS["S"][field]
    assert all(env[field] == first for env in ENVIRONMENTS.values()), field
assert len({env["runner_sha256"]["_vector_reference.py"] for env in ENVIRONMENTS.values()}) == 1
groups = {key for key, row in SUMMARY_GROUPS["M"].items() if row["status"] == "COMPLETE"} | SUMMARY_GROUPS["M-cont"].keys()
assert len(groups) == 20
remaining = sorted(WORKLOADS-L_FIRST)
assert len(remaining)==7
print(json.dumps({"status": "PASS_ARCHIVED_RECORD_AUDIT", "batches": audit, "unique_actual_run_ids": len(RUNS),
                  "M_complete_groups": len(groups), "L_pending_workloads": remaining, "L_pending_trials": len(remaining)*14,
                  "method_source_commit": SOURCE, "image": IMAGE}, ensure_ascii=False, indent=2))
