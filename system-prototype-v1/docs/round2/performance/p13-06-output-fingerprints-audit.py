"""Compare recorded method, input, correctness, and output fingerprints across actual runs."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def recorded_input_and_parameters(row):
    steps = [(step["key"], step["tool"], step["parameters"]) for step in row["steps"]]
    return canonical([row["method"], row["input_manifest_sha256"], row["input"], row["critical_options"], steps])


def opened_target_layer(row):
    log = row["step_logs"]["steps/apply_spatial_relation/process.log"]
    assert not log["truncated"]
    checks = [json.loads(line.removeprefix("input_check: ")) for line in log["text"].splitlines()
              if line.startswith("input_check: ")]
    check, = (item for item in checks if item["field"] == "inputs.targets.path" and item["passed"])
    return check["observed"]["layer"]


def observe_group(rows):
    identities = {recorded_input_and_parameters(row) for row in rows}
    roles = set(rows[0]["artifacts"])
    assert len(identities) == 1 and roles and all(set(row["artifacts"]) == roles for row in rows)
    stats = {}
    for role in sorted(roles):
        outputs = [row["artifacts"][role] for row in rows]
        fingerprints = Counter(output["fingerprint"] for output in outputs)
        descriptors = {canonical({k: v for k, v in output.items() if k != "fingerprint"}) for output in outputs}
        assert len(descriptors) == 1
        stats[role] = {
            "file_extension": Path(outputs[0]["uri"]).suffix,
            "distinct_fingerprints": len(fingerprints),
            "frequency": sorted(fingerprints.values(), reverse=True),
        }
    return {
        "actual_runs": len(rows), "distinct_recorded_inputs_and_parameters": len(identities),
        "output_byte_counts": sorted({row["output_bytes"] for row in rows}),
        "distinct_independent_correctness_reports": len({canonical(row["correctness"]) for row in rows}),
        "distinct_check_rule_sets": len({canonical(row["checks"]["rules"]) for row in rows}),
        "artifact_fingerprint_distribution": stats,
    }


stage = Path(sys.argv[1])
reports = []
all_ids = set()
large_request_identities = set()
large_target_layers = {}
for marker in sorted(stage.glob("*/*/candidate/COMPLETED")):
    evidence = marker.parent / marker.read_text().strip() / "evidence"
    raw = (evidence / "samples.jsonl").read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    ids = {row["run_id"] for row in rows}
    assert len(rows) == len(ids) == 14 and ids.isdisjoint(all_ids)
    assert all(row["status"] == "PASS" for row in rows)
    all_ids.update(ids)
    groups = {}
    for cache in ("process_cold", "warm"):
        measured = [row for row in rows if row["cache"] == cache and row["phase"] == "measurement"]
        assert len(measured) == 5
        groups[cache] = observe_group(measured)
    observation = {
        "archive": marker.parent.parent.parent.name,
        "run_ids": len(rows), "raw_sha256": sha256(raw).hexdigest(),
        "workload": rows[0]["workload"], "tier": rows[0]["tier"],
        "all_runs": observe_group(rows), "groups": groups,
    }
    if observation["tier"] == "L":
        large_request_identities.update(recorded_input_and_parameters(row) for row in rows)
        layer, = {opened_target_layer(row) for row in rows}
        large_target_layers[observation["workload"]] = layer
        observation["opened_target_layer"] = layer
    reports.append(observation)
assert len(reports) == 6 and len(all_ids) == 84
assert len(large_request_identities) == 1
assert large_target_layers == {"t13-points-intersects": "points", "t13-lines-intersects": "lines"}
print(json.dumps({"actual_runs": len(all_ids), "identical_l_identity_without_layers": True, "workloads": reports}, indent=2))
