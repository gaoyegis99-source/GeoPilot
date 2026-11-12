"""Replay archived raster comparisons and audit raw counts, areas, and resource distributions."""
import argparse
from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from statistics import median
import subprocess
import sys

METRICS = ("wall_ns", "peak_rss_bytes", "peak_temp_bytes")


def read(path):
    return json.loads(path.read_bytes())


def metric(row, name):
    if name == "wall_ns":
        return {"value": row["wall_ns"], "status": "recorded"}
    if name == "peak_rss_bytes":
        return row["telemetry"]["resources"].get(name, {})
    return row.get("workspace_size", {}).get(name, {})


def verify_distribution(rows, cache, reported):
    measured = [row for row in rows if row["cache"] == cache and row["kind"] == "measurement"]
    counts = Counter(row["status"] for row in measured)
    assert reported["counts"] == {state: counts[state] for state in ("PASS", "FAIL", "NOT_RUN")}
    for name in METRICS:
        for state in ("PASS", "FAIL"):
            observations = [metric(row, name) for row in measured if row["status"] == state]
            values = sorted(obs["value"] for obs in observations if obs.get("value") is not None)
            assert reported[name][state] == {
                "samples": len(values), "partial": sum(obs.get("status") == "partial" for obs in observations),
                "median": median(values) if len(values) >= 3 else None,
                "p95_nearest_rank": values[math.ceil(.95 * len(values)) - 1] if len(values) >= 3 else None,
            }


def areas(row):
    verification = row["correctness"]["area_statistics"]
    observed = verification["observed"]
    classes = {str(item["class_id"]): item for item in observed["classes"]}
    expected = verification["expected_area_by_class_m2"]
    assert classes.keys() == expected.keys()
    for key, area in expected.items():
        assert math.isclose(classes[key]["area_m2"], area,
                            rel_tol=verification["relative_tolerance"], abs_tol=verification["absolute_tolerance_m2"])
    assert sum(item["pixel_count"] for item in classes.values()) == observed["selected_pixel_count"]
    return {
        "source_crs": observed["source_crs"], "area_crs": observed["area_crs"],
        "valid": observed["valid_pixel_count"], "selected": observed["selected_pixel_count"],
        "nodata": observed["nodata_pixel_count"], "class_pixels": {key: item["pixel_count"] for key, item in classes.items()},
        "class_area_m2": {key: item["area_m2"] for key, item in classes.items()},
    }


def read_attempt(attempt, side, case, plan, run_ids):
    evidence = attempt / "evidence"
    report = read(evidence / "report.json")
    assert report["environment"]["commit"] == plan["commits"][side]
    assert (attempt / "exit-code").read_text().strip() == ("0" if report["status"] == "PASS" else "1")
    data = (evidence / "trials.jsonl").read_bytes()
    assert data.endswith(b"\n")
    rows = [json.loads(line) for line in data.splitlines()]
    for row in rows:
        assert row["manifest_sha256"] == plan["input_manifest_sha256"][case]
        run_id = row["run_id"]
        assert run_id not in run_ids and row["run_state"]["run_id"] == run_id
        if "telemetry" in row:
            assert row["telemetry"]["run_id"] == run_id
        run_ids.add(run_id)
    return evidence, rows, sha256(data).hexdigest(), report


def completed(case_dir, side, case, plan, run_ids):
    folder = case_dir / side
    name = (folder / "COMPLETED").read_text().strip()
    assert re.fullmatch(r"attempt\.[a-z0-9_]+", name)
    evidence, rows, digest, report = read_attempt(folder / name, side, case, plan, run_ids)
    assert report["status"] == "PASS"
    assert [(row["cache"], row["index"], row["kind"]) for row in rows] == [
        (cache, n, "warmup" if n <= 2 else "measurement") for cache in ("process_cold", "warm") for n in range(1, 8)
    ]
    assert all(row["status"] == "PASS" and successful(row) for row in rows)
    return evidence, rows, digest


def successful(row):
    return (
        (row["run_state"]["execution_status"], row["run_state"]["check_status"]) == ("SUCCEEDED", "PASSED")
        and row["v1_completed_checks"] and row["executed_steps"]
        and all(step["exit_code"] == 0 for step in row["executed_steps"])
    )


def compare(comparator, before, after):
    return subprocess.check_output([
        sys.executable, "-B", str(comparator), "--kind", "raster", "--require-runtime-configuration",
        "--before", str(before), "--after", str(after),
    ])


def audit_remote(case_dir, plan, comparator, run_ids):
    candidate, candidate_rows, candidate_digest = completed(case_dir, "candidate", "remote-s", plan, run_ids)
    base_dir = case_dir / "base"
    attempt, = base_dir.glob("attempt.*")
    base, rows, digest, report = read_attempt(attempt, "base", "remote-s", plan, run_ids)
    assert report["status"] == "FAIL" and not (base_dir / "COMPLETED").exists()
    assert not (base / "runtime-configuration.json").exists() and not list(case_dir.glob("pair.*"))
    assert [(row["cache"], row["index"], row["status"]) for row in rows] == [
        ("process_cold", n, status) for n, status in enumerate(("PASS", "FAIL", "FAIL", "PASS", "FAIL"), 1)
    ]
    assert all(successful(row) and len(row["executed_steps"]) == 8 for row in rows[:-1] + candidate_rows)
    failed = [row for row in rows if row["status"] == "FAIL"]
    assert [row["error"]["type"] for row in failed] == ["StatusObservationFailure"] * 2 + ["RuntimeError"]
    polls = [row["status_poll_errors"] for row in failed]
    assert [len(errors) for errors in polls] == [2, 2, 5]
    assert all(err["http_status"] == 500 and err["body"]["detail"]["code"] == "STORAGE_FAILED"
               for errors in polls for err in errors)
    last = rows[-1]
    assert last["stop_group"] and last["cancellation"]["http_status"] == 202
    assert json.loads(last["cancellation"]["body"])["cancel_requested"]
    assert (last["run_state"]["execution_status"], last["run_state"]["check_status"]) == ("RUNNING", "NOT_CHECKED")
    persisted = read(base / "persisted-terminal.json")
    run, step = persisted["run"], persisted["step"]
    assert persisted["quick_check"] == "ok" and persisted["sidecars"] == []
    assert run["id"] == last["run_id"] and (run["execution_status"], run["check_status"]) == ("FAILED", "NOT_CHECKED")
    assert run["current_step"] == step["step_key"] == "align_two_dates" and step["status"] == "FAILED"
    assert run["error_json"] == step["result_json"]["error"]
    assert run["error_json"]["code"] == "RUN_INTERRUPTED" and "database is locked" in run["error_json"]["message"]
    signatures = [areas(row) for row in rows[:-1] + candidate_rows]
    assert all(item == signatures[0] for item in signatures)
    replay = compare(comparator, candidate, candidate)
    assert replay == (candidate.parent / "self-comparison.json").read_bytes()
    groups = json.loads(replay)["groups"]
    for group in groups:
        verify_distribution(candidate_rows, group["group"][2], group["after"])
    return {
        "status": "INCOMPLETE_BASE", "before_trials_sha256": digest, "after_trials_sha256": candidate_digest,
        "base_observed_trials": len(rows), "base_verified_terminal_successes": len(rows) - 1,
        "base_status_get_errors": sum(map(len, polls)), "candidate_successes": len(candidate_rows),
        "base_last_persisted_run": run, "base_last_db_sha256": persisted["sha256"],
        "successful_area_signature": signatures[0],
        "candidate_distributions": {group["group"][2]: group["after"] for group in groups},
    }


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("archive", type=Path)
parser.add_argument("comparator", type=Path)
args = parser.parse_args()
plan = read(args.archive / "plan.json")
assert sha256(args.comparator.read_bytes()).hexdigest() == plan["comparator"]
run_ids = set()
case_dir = args.archive / "dem-s"
base, candidate = (completed(case_dir, side, "dem-s", plan, run_ids) for side in ("base", "candidate"))
comparison, = case_dir.glob("pair.*/before-after.json")
replay = compare(args.comparator, base[0], candidate[0])
assert replay == comparison.read_bytes()
report = json.loads(replay)
for group in report["groups"]:
    for side, (_, rows, _) in zip(("before", "after"), (base, candidate), strict=True):
        verify_distribution(rows, group["group"][2], group[side])
signatures = [areas(row) for _, rows, _ in (base, candidate) for row in rows]
assert all(item == signatures[0] for item in signatures)
results = {"dem-s": {
    "before_trials_sha256": base[2], "after_trials_sha256": candidate[2],
    "successful_area_signature": signatures[0], "comparison": report,
}}
results["remote-s"] = audit_remote(args.archive / "remote-s", plan, args.comparator, run_ids)
print(json.dumps({"status": "PASS_RASTER_AUDIT", "runs": len(run_ids), "cases": results}, indent=2))
