"""Compare resource observations from two completed GeoPilot benchmark directories."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import gzip
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from stat import S_ISREG
from statistics import median
import shlex
import sys


METRICS = ("wall_ns", "peak_rss_bytes", "peak_temp_bytes")
STATUSES = {"PASS", "FAIL", "NOT_RUN"}
VECTOR_ENV = (
    "platform", "os_release", "cpu_models", "cpu_affinity", "physical_core_count", "logical_cpu_count",
    "cgroup_limits", "storage_filesystem", "python", "python_packages", "proj", "proj_data", "pyogrio_gdal",
    "geos", "qgis_version", "qgis_process_sha256", "qgis_process_binary_sha256", "protocol_sha256",
    "profile_sha256", "runner_sha256", "thread_and_cache_settings", "cache_state",
    "planned_repeats", "planned_warmups", "poll_seconds", "temp_sample_seconds", "temp_entry_limit",
)
RASTER_ENV = (
    "os", "os_release", "kernel", "cpu_model", "affinity", "physical_cores", "logical_cores",
    "ram", "swap", "versions", "runtime_environment", "os_page_cache",
)
RUNTIME_CONFIGURATION_FILE = "runtime-configuration.json"
RUNTIME_CONFIGURATION_LEVEL = "application_config_and_python_proj"


@dataclass(frozen=True)
class Profile:
    source: str
    environment: dict
    inputs: dict
    groups: dict
    runtime_configuration: dict | None = None


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate field in runtime configuration evidence: {key}")
        result[key] = value
    return result


def _is_sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _relative_file(value, *, basename=False):
    if not isinstance(value, str):
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} and "\x00" not in part for part in parts) and (
        not basename or len(parts) == 1
    )


def _file_digests(files, *, basename=False):
    return isinstance(files, dict) and all(
        _relative_file(name, basename=basename) and _is_sha256(digest) for name, digest in files.items()
    )


def _runtime_snapshot_digest(snapshot):
    if not isinstance(snapshot, dict) or snapshot.keys() != {"schema_version", "config", "python_proj", "sha256"}:
        raise ValueError("Invalid runtime configuration snapshot fields")
    if type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != 1:
        raise ValueError("Unsupported runtime configuration snapshot version")
    config = snapshot["config"]
    families = {"methods", "tools", "runner_bindings", "sensors"}
    if not isinstance(config, dict) or config.keys() != families or any(
        not _file_digests(files, basename=True) for name, files in config.items()
        if name != "sensors" or files is not None
    ):
        raise ValueError("Invalid runtime configuration file fingerprints")
    proj = snapshot["python_proj"]
    proj_fields = {"version", "epsg_version", "network_enabled", "search_roots", "user_files", "auxiliary_databases"}
    if not isinstance(proj, dict) or proj.keys() != proj_fields:
        raise ValueError("Invalid Python PROJ snapshot fields")
    roots, user, auxiliary = proj["search_roots"], proj["user_files"], proj["auxiliary_databases"]
    if (
        any(not isinstance(proj[key], str) or not proj[key].strip() for key in ("version", "epsg_version"))
        or type(proj["network_enabled"]) is not bool
        or not isinstance(roots, list) or not all(_file_digests(root) for root in roots)
        or not any("proj.db" in root for root in roots)
        or (user is not None and not _file_digests(user))
        or not isinstance(auxiliary, list)
        or any(
            not isinstance(entry, dict) or entry.keys() != {"name", "sha256"}
            or not _relative_file(entry["name"], basename=True) or not _is_sha256(entry["sha256"])
            for entry in auxiliary
        )
    ):
        raise ValueError("Invalid Python PROJ fingerprints")
    claimed = snapshot["sha256"]
    payload = {key: value for key, value in snapshot.items() if key != "sha256"}
    # Match geopilot.methods.release_ledger.fingerprint without a runtime dependency on GeoPilot.
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if not _is_sha256(claimed) or sha256(canonical.encode("ascii")).hexdigest() != claimed:
        raise ValueError("Runtime configuration snapshot digest differs from its contents")
    return claimed


def read_runtime_configuration(directory, reports, *, required):
    path = directory / RUNTIME_CONFIGURATION_FILE
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if required:
            raise ValueError(f"Runtime configuration evidence is required in {directory}") from None
        return None
    with os.fdopen(fd, "rb") as stream:
        if not S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Expected a regular runtime configuration file in {directory}")
        receipt = json.load(stream, object_pairs_hook=_unique_json_object)
    if not isinstance(receipt, dict) or receipt.keys() != {"schema_version", "report_files", "before", "after"}:
        raise ValueError("Invalid runtime configuration evidence fields")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        raise ValueError("Unsupported runtime configuration evidence version")
    if receipt["report_files"] != {name: sha256(raw).hexdigest() for name, raw in reports.items()}:
        raise ValueError("Runtime configuration evidence does not match the report files")
    before = _runtime_snapshot_digest(receipt["before"])
    after = _runtime_snapshot_digest(receipt["after"])
    if before != after:
        raise ValueError("Runtime configuration changed between the before and after captures")
    return {"level": RUNTIME_CONFIGURATION_LEVEL, "schema_version": receipt["schema_version"], "sha256": before}


def read_rows(directory, name):
    plain = directory / name
    compressed = plain.with_name(plain.name + ".gz")
    if plain.is_file():
        stream = plain.open(encoding="utf-8")
    else:
        stream = gzip.open(compressed, "rt", encoding="utf-8")
    with stream:
        yield from (json.loads(line) for line in stream)


def stable_inputs(record, before, after, manifest_key):
    fingerprints = record[before]
    files = fingerprints.values() if isinstance(fingerprints, dict) else fingerprints
    if any(not isinstance(file["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", file["sha256"])
           or type(file["bytes"]) is not int or file["bytes"] < 0 for file in files):
        raise ValueError("Invalid file fingerprint")
    manifest = record[manifest_key]
    if (
        not fingerprints or fingerprints != record[after]
        or not isinstance(manifest, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest)
    ):
        raise ValueError("Input fingerprint evidence is incomplete or changed during the run")
    return {"manifest": manifest, "files": fingerprints}


def _metric(name, reading, measured_status):
    if not isinstance(reading, dict):
        raise ValueError(f"Invalid {name} observation")
    if not reading:
        return reading
    state, value = reading.get("status"), reading.get("value")
    if state == "unavailable" and value is None:
        return reading
    if (
        state not in {measured_status, "partial"} or type(value) not in {int, float}
        or not math.isfinite(value) or value < 0
    ):
        raise ValueError(f"Invalid {name} observation")
    return reading


def observation(row, resources, temp, options, *, require_run=False, executed=None):
    state = row["status"]
    if state not in STATUSES:
        raise ValueError(f"Unsupported sample status {state!r}")
    run = row.get("run_status") or row.get("run_state")
    if state == "PASS":
        if not row.get("correctness"):
            raise ValueError("A successful sample lacks independent output verification")
        if (require_run or run is not None) and (
            not run or (run["execution_status"], run["check_status"]) != ("SUCCEEDED", "PASSED")
        ):
            raise ValueError("A successful sample lacks a successful persisted run outcome")
        if require_run and not executed:
            raise ValueError("A successful sample lacks executed step evidence")
        if require_run and (run.get("run_id") != row.get("run_id") or not row.get("run_id")):
            raise ValueError("A successful sample has a different persisted run identity")
    failure = (run or {}).get("error") or row.get("error")
    error_code = failure.get("code") if isinstance(failure, dict) else row.get("reason")
    if (
        state == "FAIL" and require_run and run and error_code is None and run.get("error") is None
        and isinstance(failure, dict) and failure.get("type") == "StatusObservationFailure"
        and (run.get("execution_status"), run.get("check_status")) == ("SUCCEEDED", "PASSED")
        and row.get("run_id") and run.get("run_id") == row["run_id"]
        and row.get("correctness") and row.get("status_poll_errors")
    ):
        error_code = "STATUS_OBSERVATION_FAILED"
    wall = row.get("wall_ns")
    return {
        "status": state,
        "error_code": error_code,
        "options": options,
        "executed": executed,
        "wall_ns": _metric("wall_ns", {"status": "unavailable" if wall is None else "recorded", "value": wall}, "recorded"),
        "peak_rss_bytes": _metric("peak_rss_bytes", resources.get("peak_rss_bytes", {}), "sampled"),
        "peak_temp_bytes": _metric("peak_temp_bytes", temp, "sampled"),
    }


def _executed_steps(steps, key, *, parameters=False):
    recorded = {}
    for step in steps:
        if step["status"] == "NOT_STARTED":
            continue
        name, tool = step[key], step["tool"]
        if name in recorded or not isinstance(tool, dict) or not tool:
            raise ValueError("Missing or duplicate executed step tool evidence")
        fields = {"tool": tool}
        if parameters:
            if not isinstance(step["parameters"], dict):
                raise ValueError("An executed step lacks its actual parameters")
            fields["parameters"] = step["parameters"]
        recorded[name] = fields
    return recorded


def _count_matches(record, fields, message):
    if any(type(record.get(name)) is not int or record[name] != count for name, count in fields.items()):
        raise ValueError(message)


def _keyed(rows, identity, label):
    result = {}
    for row in rows:
        key = identity(row)
        if key in result:
            raise ValueError(f"Duplicate {label} group: {key}")
        result[key] = row
    return result


def record_sample(groups, group, key, item):
    if key in groups[group]:
        raise ValueError(f"Duplicate sample in {group}: {key}")
    groups[group][key] = item


def vector_profile(directory, *, require_runtime_configuration=False):
    reports = {name: (directory / name).read_bytes() for name in ("summary.json", "environment.json")}
    summary, environment = (json.loads(reports[name].decode("utf-8")) for name in reports)
    runtime = read_runtime_configuration(directory, reports, required=require_runtime_configuration)
    if summary["probe"] or environment["probe"]:
        raise ValueError("A vector probe cannot be compared as formal evidence")
    stable = {key: environment[key] for key in VECTOR_ENV}
    command, *mount_lines = (directory / "command.log").read_text(encoding="utf-8").splitlines()
    image = [value for value in shlex.split(command) if re.fullmatch(r"sha256:[0-9a-f]{64}", value)]
    if len(image) != 1:
        raise ValueError("Vector command must record one fixed container image")
    stable["image"] = image[0]
    mounts = [line for line in mount_lines if re.fullmatch(r"/dev/[^\s\[\]]+(?:\[.*\])? \S+", line)]
    if len(mounts) != 1:
        raise ValueError("Vector command must record the benchmark storage device and filesystem")
    stable["storage_mount"] = re.sub(r"\[.*\]", "", mounts[0])
    inputs = {}
    for tier, record in read_json(directory / "inputs.json").items():
        inputs[tier, "p13-01"] = stable_inputs(record, "files_before", "files_after", "input_manifest_sha256")
        if corrected := record.get("road_position_correction"):
            inputs[tier, corrected["input_profile"]] = stable_inputs(
                corrected, "files_before", "files_after", "input_manifest_sha256",
            )
    groups = defaultdict(dict)
    totals = Counter()
    for row in read_rows(directory, "samples.jsonl"):
        tier, workload, cache = (row[key] for key in ("tier", "workload", "cache"))
        profile = row.get("input_profile", "p13-01")
        identity = inputs[tier, profile]
        if row.get("input_manifest_sha256", identity["manifest"]) != identity["manifest"]:
            raise ValueError(f"Sample input manifest differs for {tier}/{workload}/{cache}")
        resources = ((row.get("telemetry") or {}).get("document") or {}).get("resources", {})
        options = {key: row[key] for key in ("method", "critical_options") if key in row}
        executed = _executed_steps(row.get("steps", ()), "key", parameters=True)
        item = observation(row, resources, row.get("peak_temp_bytes", {}), options,
                           require_run=True, executed=executed)
        record_sample(groups, (tier, workload, cache, profile), (row["phase"], row["repeat"]), item)
        totals[item["status"]] += 1
    expected = _keyed(
        summary["groups"], lambda row: (row["tier"], row["workload"], row["cache"], row.get("input_profile", "p13-01")),
        "vector summary",
    )
    if expected.keys() != groups.keys():
        raise ValueError("Vector raw samples do not cover the recorded summary groups")
    _count_matches(summary["sample_counts"], {state: totals[state] for state in STATUSES},
                   "Vector report sample counts differ from raw samples")
    expected_status = "PASS" if not (totals["FAIL"] or totals["NOT_RUN"]) else "INCOMPLETE"
    if summary["status"] != expected_status:
        raise ValueError("Vector report status differs from raw samples")
    for key, samples in groups.items():
        counts = Counter((phase, sample["status"]) for (phase, _), sample in samples.items())
        fields = {
            f"{prefix}_{state.lower()}": counts[phase, state]
            for phase, prefix in (("warmup", "warmup"), ("measurement", "measured")) for state in STATUSES
        }
        _count_matches(expected[key], fields, f"Vector report group counts differ from raw samples for {key}")
    return Profile(environment["source_commit"], stable, inputs, groups, runtime)


def _raster_step_evidence(sample):
    telemetry = (sample.get("telemetry") or {}).get("steps", ())
    if "executed_steps" not in sample:
        return _executed_steps(telemetry, "step_key"), "confirmed_options_only"

    requests = sample["executed_steps"]
    if not isinstance(requests, list):
        raise ValueError("Expected a list of normalized step requests")
    executed = _executed_steps(requests, "step_key", parameters=True)
    if telemetry:
        expected = {
            step["step_key"]: (step["tool"]["id"], step["tool"]["version"], step["status"], step["exit_code"])
            for step in telemetry
            if any(process["phase"] == "execute" and process["exit_code"] is not None for process in step["processes"])
        }
        observed = {
            step["step_key"]: (step["tool"]["id"], step["tool"]["version"], step["status"], step["exit_code"])
            for step in requests
        }
        if len(observed) != len(requests) or list(observed.items()) != list(expected.items()):
            raise ValueError("Normalized step request identity differs from raster execution telemetry")
    elif sample["status"] == "PASS":
        raise ValueError("Successful normalized step requests require raster execution telemetry")
    return executed, "normalized_step_requests"


def raster_profile(directory, *, require_runtime_configuration=False):
    report_bytes = (directory / "report.json").read_bytes()
    report = json.loads(report_bytes.decode("utf-8"))
    runtime = read_runtime_configuration(directory, {"report.json": report_bytes}, required=require_runtime_configuration)
    if report["mode"] != "formal" or report["status"] not in {"PASS", "FAIL"} or report.get("error"):
        raise ValueError("A completed formal raster report is required")
    environment = report["environment"]
    stable = {key: environment[key] for key in RASTER_ENV}
    mount = environment["storage"]["mount_source_and_filesystem"]
    if not mount:
        raise ValueError("Raster report must record the benchmark storage device and filesystem")
    stable["storage_mount"] = re.sub(r"\[.*\]", "", mount)
    stable["cgroup_limits"] = {
        re.sub(r"/docker/[a-f0-9]{64}", "/docker/<container>", name): value
        for name, value in environment["cgroups"]["limits"].items()
    }
    for key in ("family", "benchmark_manifest_sha256", "profile_sha256", "implementation_sha256"):
        stable[key] = report[key]
    inputs = {
        (tier, "p13-01"): stable_inputs(record, "first_fingerprints", "last_fingerprints", "manifest_sha256")
        for tier, record in report["inputs"].items()
    }
    groups = defaultdict(dict)
    raw_trials = defaultdict(list)
    for row in read_rows(directory, "trials.jsonl"):
        tier, workload, cache = (row[key] for key in ("tier", "workload", "cache"))
        if row["family"] != report["family"] or row["manifest_sha256"] != inputs[tier, "p13-01"]["manifest"]:
            raise ValueError(f"Sample input identity differs for {tier}/{workload}/{cache}")
        if row["status"] not in STATUSES:
            raise ValueError(f"Unsupported raster trial status {row['status']!r}")
        raw_trials[tier, workload, cache].append(row)
        phase = row["kind"]
        repeat = row["index"] if phase == "warmup" else row["index"] - 2
        is_method = workload == "method"
        operations = [("method", row)] if is_method else [(step["step_key"], step) for step in row["steps"]]
        for operation, sample in operations:
            resources = (sample.get("telemetry") or sample).get("resources", {})
            keys = ("method", "critical_options") if is_method else ("operation", "options")
            options = {key: sample[key] for key in keys if key in sample}
            executed = None
            if is_method:
                parameters = sample.get("confirmed_parameters")
                if parameters is not None or sample.get("parameters_sha256") is not None or sample["status"] == "PASS":
                    if not isinstance(parameters, dict) or sha256(
                        json.dumps(parameters, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                    ).hexdigest() != sample.get("parameters_sha256"):
                        raise ValueError("Raster confirmed parameters differ from their recorded digest")
                    options["confirmed_parameters"] = parameters
                executed, options["step_parameter_evidence"] = _raster_step_evidence(sample)
            temp = sample.get("workspace_size", {}).get("peak_temp_bytes", {})
            record_sample(groups, (tier, workload, cache, "p13-01", operation), (phase, repeat),
                          observation(sample, resources, temp, options, require_run=is_method, executed=executed))
    def identity(row):
        return (row["tier"], row["workload"], row["cache"])

    reported = _keyed(report["groups"], identity, "raster report")
    expected = _keyed(report["expected_groups"], identity, "expected raster report")
    recorded_operations = {(*key, "p13-01", op) for key, group in reported.items() for op in group["operations"]}
    if recorded_operations != groups.keys() or not (expected.keys() == reported.keys() == raw_trials.keys()):
        raise ValueError("Raster raw samples do not cover the recorded report groups")
    for key, trials in raw_trials.items():
        failures = sum(row["status"] != "PASS" for row in trials)
        _count_matches(reported[key], {"expected_trials": 7, "observed_trials": len(trials), "trial_failures": failures},
                       f"Raster report group counts differ from raw trials for {key}")
    expected_status = "PASS" if all(row["status"] == "PASS" for rows in raw_trials.values() for row in rows) else "FAIL"
    if report["status"] != expected_status:
        raise ValueError("Raster report status differs from raw trials")
    for key, samples in groups.items():
        counts = Counter(sample["status"] for (phase, _), sample in samples.items() if phase == "measurement")
        fields = {"passed": counts["PASS"], "failed": counts["FAIL"], "missing": 5 - counts["PASS"] - counts["FAIL"]}
        _count_matches(reported[key[:3]]["operations"][key[-1]], fields,
                       f"Raster report operation counts differ from raw samples for {key}")
    return Profile(environment["commit"], stable, inputs, groups, runtime)


def summarize(group):
    observed = [sample for (phase, _), sample in group.items() if phase == "measurement"]
    statuses = Counter(sample["status"] for sample in observed)
    errors = Counter(row["error_code"] or "unspecified" for row in observed if row["status"] == "FAIL")
    result = {"counts": {state: statuses[state] for state in sorted(STATUSES)}, "failure_codes": dict(sorted(errors.items()))}
    for metric in METRICS:
        distributions = {}
        for state in ("PASS", "FAIL"):
            samples = [row[metric] for row in observed if row["status"] == state and row[metric].get("value") is not None]
            values = sorted(row["value"] for row in samples)
            distributions[state] = {
                "samples": len(values),
                "partial": sum(row["status"] == "partial" for row in samples),
                "median": median(values) if len(values) >= 3 else None,
                "p95_nearest_rank": values[math.ceil(.95 * len(values)) - 1] if len(values) >= 3 else None,
            }
        result[metric] = distributions
    return result


def configuration(group):
    options = {json.dumps(sample["options"], sort_keys=True, allow_nan=False) for sample in group.values()
               if sample["status"] != "NOT_RUN" and sample["options"]}
    if len(options) != 1:
        raise ValueError("Benchmark group has missing or inconsistent method options")
    return options.pop()


def _execution_configuration(group):
    observed, completed = {}, set()
    for sample in group.values():
        executed = sample["executed"]
        if executed is None:
            continue
        if sample["status"] == "PASS":
            completed.add(tuple(executed))
        for step, actual in executed.items():
            identity = json.dumps(actual, sort_keys=True, allow_nan=False)
            if observed.setdefault(step, identity) != identity:
                raise ValueError(f"Inconsistent executed step configuration for {step}")
    if len(completed) > 1:
        raise ValueError("Inconsistent executed step order across successful samples")
    return observed, next(iter(completed), None)


def compare(before, after):
    if any(not isinstance(profile.source, str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", profile.source)
           for profile in (before, after)):
        raise ValueError("Both benchmarks require an exact source commit")
    old_runtime, new_runtime = before.runtime_configuration, after.runtime_configuration
    if (old_runtime is None) != (new_runtime is None):
        raise ValueError("Different runtime configuration evidence levels")
    if old_runtime != new_runtime:
        raise ValueError("Different recorded runtime configuration fingerprints")
    changed = [key for key in before.environment.keys() | after.environment.keys()
               if before.environment.get(key) != after.environment.get(key)]
    if changed:
        raise ValueError("Different recorded benchmark environment: " + ", ".join(sorted(changed)))
    if before.inputs != after.inputs:
        raise ValueError("The before and after input fingerprints differ")
    if not before.groups or before.groups.keys() != after.groups.keys():
        raise ValueError("The before and after benchmark groups differ")
    planned = {("warmup", 1), ("warmup", 2), *(("measurement", index) for index in range(1, 6))}
    result = []
    for key, old in sorted(before.groups.items()):
        new = after.groups[key]
        if old.keys() != planned or new.keys() != planned:
            raise ValueError(f"Expected two warmups and five measurements for {key}")
        old_configuration = configuration(old)
        if old_configuration != configuration(new):
            raise ValueError(f"Different recorded method options for {key}")
        old_steps, old_complete = _execution_configuration(old)
        new_steps, new_complete = _execution_configuration(new)
        if (
            (old_complete is not None and new_complete is not None and old_complete != new_complete)
            or any(old_steps[step] != new_steps[step] for step in old_steps.keys() & new_steps.keys())
        ):
            raise ValueError(f"Different executed step configuration for {key}")
        previous, current = summarize(old), summarize(new)
        deltas = {}
        for metric in METRICS:
            a, b = previous[metric]["PASS"], current[metric]["PASS"]
            complete = a["samples"] == previous["counts"]["PASS"] == current["counts"]["PASS"] == b["samples"]
            if complete and a["median"] is not None and b["median"] is not None and not (a["partial"] or b["partial"]):
                deltas[metric] = {name: b[name] - a[name] for name in ("median", "p95_nearest_rank")}
        record = {"group": key, "before": previous, "after": current, "successful_sample_deltas": deltas}
        evidence_level = json.loads(old_configuration).get("step_parameter_evidence")
        if evidence_level is not None:
            record["step_parameter_evidence"] = evidence_level
        result.append(record)
    return {
        "status": "MATCHED_EVIDENCE", "before_source": before.source, "after_source": after.source,
        "same_source": before.source == after.source,
        "runtime_configuration": old_runtime if old_runtime is not None else {"level": "unrecorded"},
        "candidate_all_measurements_pass": all(group["after"]["counts"]["PASS"] == 5 for group in result),
        "groups": result,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("vector", "raster"), required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--require-runtime-configuration", action="store_true",
                        help="require a verified runtime-configuration.json in both report directories")
    args = parser.parse_args(argv)
    load = vector_profile if args.kind == "vector" else raster_profile
    try:
        comparison = compare(
            load(args.before, require_runtime_configuration=args.require_runtime_configuration),
            load(args.after, require_runtime_configuration=args.require_runtime_configuration),
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"Incompatible benchmark evidence: {error}", file=sys.stderr)
        return 1
    print(json.dumps(comparison, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
