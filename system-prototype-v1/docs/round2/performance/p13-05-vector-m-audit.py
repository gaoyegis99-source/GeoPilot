"""Audit the completed M vector comparisons from their raw observations."""
import argparse
from collections import Counter
import gzip
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from statistics import median

WORKLOADS = ("t13-points-intersects", "t13-lines-within", "t14-split_by_area", "road")
SOURCES = {"base": "977ab412d9f6e7558d22a5c431fac0ada90b07be", "candidate": "e874a0f7510ad7d99e0e623f211e0f5afeb411cd"}
SLOTS = [("warmup", n) for n in range(1, 3)] + [("measurement", n) for n in range(1, 6)]
CACHES = ("process_cold", "warm")
METRICS = ("wall_ns", "peak_rss_bytes", "peak_temp_bytes")


def read(path):
    return json.loads(path.read_bytes())


def historical_results(directory):
    expected = {}
    for name in ("M", "M-cont"):
        with gzip.open(directory / f"p13-03-{name}-samples.jsonl.gz", "rt", encoding="utf-8") as stream:
            for row in map(json.loads, stream):
                if row["status"] == "PASS" and row["workload"] in WORKLOADS:
                    expected.setdefault(row["workload"], row["correctness"])
    assert set(expected) == set(WORKLOADS)
    return expected


def result_matches(actual, expected, workload, reference):
    assert actual.keys() == expected.keys()
    if workload.startswith("t13-"):
        for key in ("selected", "clipped"):
            assert actual[key] == expected[key]
        for key in ("retained_line_length_m", "retained_area_m2"):
            assert math.isclose(actual[key], expected[key], abs_tol=1e-6, rel_tol=0)
    elif workload == "road":
        assert {k: v for k, v in actual.items() if k != "buffer"} == {
            k: v for k, v in expected.items() if k != "buffer"
        }
        tolerance = reference["road"]["main_road_buffer"]["absolute_tolerances"]
        error = actual["buffer"]
        for key, limit in (
            ("max_bounds_error_m", "bounds_m"), ("max_area_error_m2", "area_m2"),
            ("max_boundary_hausdorff_m", "boundary_hausdorff_m"),
        ):
            assert 0 <= error[key] <= tolerance[limit]
        assert math.isclose(error["area_m2"], expected["buffer"]["area_m2"],
                            abs_tol=reference["tiles"] * tolerance["area_m2"], rel_tol=0)
    else:
        assert actual == expected


def side_results(directory, side, workload, expected, run_ids):
    marker = (directory / side / "COMPLETED").read_text().strip()
    assert re.fullmatch(r"attempt\.[A-Za-z0-9]+", marker)
    attempt = directory / side / marker
    evidence = attempt / "evidence"
    assert (attempt / "logging-exit").read_text().strip() == "0"
    assert read(evidence / "environment.json")["source_commit"] == SOURCES[side]
    report = read(evidence / "summary.json")
    receipt = read(evidence / "runtime-configuration.json")
    assert receipt["schema_version"] == 1 and receipt["before"] == receipt["after"]
    assert receipt["report_files"] == {
        name: sha256((evidence / name).read_bytes()).hexdigest() for name in ("summary.json", "environment.json")
    }, f"Receipt does not match the actual report bytes for {workload}/{side}"
    content = {key: value for key, value in receipt["before"].items() if key != "sha256"}
    digest = sha256(json.dumps(content, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest == receipt["before"]["sha256"]
    files = read(evidence / "inputs.json")["M"]
    manifest_bytes = (evidence / "input-M/manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert sha256(manifest_bytes).hexdigest() == files["input_manifest_sha256"]
    assert files["files_before"] == files["files_after"] == manifest["files"]
    raw = (evidence / "samples.jsonl").read_bytes()
    assert raw.endswith(b"\n")
    rows = [json.loads(line) for line in raw.splitlines()]
    assert [(r["cache"], r["phase"], r["repeat"]) for r in rows] == [
        (cache, phase, repeat) for cache in CACHES for phase, repeat in SLOTS
    ]
    counts = Counter(row["status"] for row in rows)
    assert not counts["NOT_RUN"] and report["sample_counts"] == {name: counts[name] for name in ("PASS", "FAIL", "NOT_RUN")}
    assert (attempt / "container-exit").read_text().strip() == ("1" if counts["FAIL"] else "0")
    for row in rows:
        run_id = row["run_id"]
        assert run_id not in run_ids and row["run_status"]["run_id"] == run_id
        run_ids.add(run_id)
        assert (row["tier"], row["workload"], row["input_manifest_sha256"]) == (
            "M", workload, files["input_manifest_sha256"]
        )
        assert row["telemetry"]["status"] == "recorded"
        telemetry = row["telemetry"]["document"]
        assert telemetry["run_id"] == run_id
        state = row["run_status"]
        if row["status"] == "PASS":
            assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "PASSED")
            assert row["checks"]["rules"] and all(row["checks"]["rules"].values())
            assert all((step["status"], step["exit_code"]) == ("SUCCEEDED", 0) for step in row["steps"])
            result_matches(row["correctness"], expected, workload, manifest["reference"])
        else:
            assert row["status"] == "FAIL" and state["execution_status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}
    return evidence, digest, files["input_manifest_sha256"], sha256(raw).hexdigest(), rows


def metric(row, name):
    if name == "wall_ns":
        return {"value": row["wall_ns"], "status": "recorded"}
    if name == "peak_temp_bytes":
        return row["peak_temp_bytes"]
    return row["telemetry"]["document"]["resources"][name]


def group_result(rows, cache, reported):
    measured = [row for row in rows if row["cache"] == cache and row["phase"] == "measurement"]
    for status in ("PASS", "FAIL", "NOT_RUN"):
        assert reported["counts"][status] == sum(row["status"] == status for row in measured)
    for name in METRICS:
        for status in ("PASS", "FAIL"):
            sampled = [metric(row, name) for row in measured if row["status"] == status]
            values = sorted(reading["value"] for reading in sampled if reading["value"] is not None)
            assert reported[name][status] == {
                "samples": len(values),
                "partial": sum(reading["status"] == "partial" and reading["value"] is not None for reading in sampled),
                "median": median(values) if len(values) >= 3 else None,
                "p95_nearest_rank": values[math.ceil(.95 * len(values)) - 1] if len(values) >= 3 else None,
            }


def step_medians(rows, cache):
    observed = {}
    for row in rows:
        if (row["cache"], row["phase"], row["status"]) != (cache, "measurement", "PASS"):
            continue
        for step in row["telemetry"]["document"]["steps"]:
            observed.setdefault(step["step_key"], []).append(step["wall_ns"])
    return {key: median(times) for key, times in observed.items() if len(times) >= 3}


def verify_deltas(group):
    expected = {}
    before, after = group["before"], group["after"]
    for name in METRICS:
        old, new = before[name]["PASS"], after[name]["PASS"]
        full_coverage = old["samples"] == new["samples"] == before["counts"]["PASS"] == after["counts"]["PASS"]
        if full_coverage and old["median"] is not None and new["median"] is not None and not (old["partial"] or new["partial"]):
            expected[name] = {key: new[key] - old[key] for key in ("median", "p95_nearest_rank")}
    assert expected == group["successful_sample_deltas"], f"Incorrect deltas for {group['group']}"


def compare(directory, workload, expected, run_ids):
    base = side_results(directory, "base", workload, expected, run_ids)
    candidate = side_results(directory, "candidate", workload, expected, run_ids)
    assert base[1:3] == candidate[1:3], "Runtime configuration or input bytes differ"
    paths = [base[0], candidate[0]]
    matches = [path / "result.json" for path in sorted(directory.glob("comparison.*"))
               if (path / "result.json").is_file()
               and [directory / Path(value) for value in (path / "evidence-paths").read_text().splitlines()] == paths]
    assert matches, "The completed attempts have no paired comparison"
    document = read(matches[0])
    assert all(read(path) == document for path in matches[1:])
    assert document["status"] == "MATCHED_EVIDENCE"
    assert (document["before_source"], document["after_source"]) == tuple(SOURCES.values())
    assert document["runtime_configuration"]["sha256"] == base[1]
    assert {tuple(group["group"]) for group in document["groups"]} == {
        ("M", workload, cache, "p13-01") for cache in CACHES
    }
    medians = {}
    for group in document["groups"]:
        cache = group["group"][2]
        group_result(base[-1], cache, group["before"])
        group_result(candidate[-1], cache, group["after"])
        verify_deltas(group)
        medians[cache] = {"base": step_medians(base[-1], cache), "candidate": step_medians(candidate[-1], cache)}
    assert document["candidate_all_measurements_pass"] == all(
        group["after"]["counts"]["PASS"] == 5 for group in document["groups"]
    )
    return {
        "workload": workload, "input_manifest_sha256": base[2], "runtime_configuration_sha256": base[1],
        "base_sample_sha256": base[3], "candidate_sample_sha256": candidate[3],
        "comparison": matches[0].relative_to(directory.parent).as_posix(), "groups": document["groups"],
        "successful_step_wall_medians_ns": medians,
        "candidate_all_measurements_pass": document["candidate_all_measurements_pass"],
    }


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path)
parser.add_argument("historical_evidence", type=Path)
parser.add_argument("--workload", choices=WORKLOADS, action="append")
args = parser.parse_args()
expected = historical_results(args.historical_evidence)
run_ids = set()
workloads = args.workload or WORKLOADS
assert len(set(workloads)) == len(workloads)
results = [compare(args.root.resolve() / name, name, expected[name], run_ids) for name in workloads]
print(json.dumps({"status": "PASS_VECTOR_M_AUDIT", "records": len(run_ids), "workloads": results}, indent=2))
