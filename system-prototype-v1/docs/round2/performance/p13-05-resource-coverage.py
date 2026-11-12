"""Summarize CPU, storage and observation coverage of sealed P13-05 pairs."""

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath
from statistics import median
import tarfile


ARCHIVES = {
    "vector-m-revised": ("road", "t13-lines-within", "t13-points-intersects", "t14-split_by_area"),
    "vector-l-point": ("t13-points-intersects",),
    "vector-l-line": ("t13-lines-intersects",),
    "vector-l-area": ("t14-split_by_area",),
    "raster-s": ("dem-s",),
    "remote-s-rerun": ("remote-s",),
    "dem-l-pair": ("dem-l",),
}
SIDES = {"before": "base", "after": "candidate"}
STATUSES = ("PASS", "FAIL", "NOT_RUN")
RESOURCE_NAMES = ("cpu_ns", "peak_rss_bytes", "disk_read_bytes", "disk_write_bytes")


def archive_documents(path, cases):
    """Read only the paired summaries, completion pointers and raw rows."""
    documents = {}
    with tarfile.open(path, "r|gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] not in cases:
                continue
            name = parts[-1]
            if name not in {"COMPLETED", "samples.jsonl", "trials.jsonl", "before-after.json", "result.json"}:
                continue
            if len(parts) > 3 and name == "result.json":
                continue
            if not member.isfile() or parts in documents:
                raise ValueError(f"Expected a unique regular archive member: {member.name}")
            with archive.extractfile(member) as stream:
                documents[parts] = stream.read()
    return documents


def phase(row):
    return row["kind"] if "sample_id" in row else row["phase"]


def telemetry(row):
    recorded = row.get("telemetry") or {}
    return (recorded.get("document") or {}) if "sample_id" not in row else recorded


def distribution(values):
    ordered = sorted(values)
    enough = len(ordered) >= 3
    return {
        "minimum": ordered[0] if ordered else None,
        "median": median(ordered) if enough else None,
        "p95_nearest_rank": ordered[math.ceil(.95 * len(ordered)) - 1] if enough else None,
        "maximum": ordered[-1] if ordered else None,
    }


def resources(rows, name):
    readings = [(telemetry(row).get("resources") or {}).get(name, {}) for row in rows]
    coverage = Counter(reading.get("status", "unavailable") for reading in readings)
    values = []
    command_counts = set()
    for reading in readings:
        if "observed_processes" in reading:
            command_counts.add((reading["observed_processes"], reading["processes"]))
        value = reading.get("value")
        if value is not None:
            if type(value) is not int or value < 0 or reading["status"] not in {"sampled", "partial"}:
                raise ValueError(f"Invalid production resource observation: {name}")
            values.append(value)
    return {
        "coverage": dict(sorted(coverage.items())),
        "observed_to_started_commands": sorted(command_counts),
        **distribution(values),
    }


def files(rows, field):
    values = [row[field] for row in rows if field in row]
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError(f"Invalid file bytes: {field}")
    return {"recorded": len(values), **distribution(values)}


def measured_partition(rows, status, raster):
    selected = [row for row in rows if row["status"] == status]
    if not selected:
        return None
    independent = sum(bool(row.get("correctness")) for row in selected)
    terminal = Counter()
    for row in selected:
        state = row.get("run_state" if raster else "run_status") or {}
        terminal[(state.get("execution_status", "UNAVAILABLE"), state.get("check_status", "UNAVAILABLE"))] += 1
    return {
        "rows": len(selected),
        "terminal_outcomes": {"/".join(key): count for key, count in sorted(terminal.items())},
        "independent_verification_recorded": independent,
        "status_observation_failures": sum(
            (row.get("error") or {}).get("type") == "StatusObservationFailure" for row in selected
        ) if raster else 0,
        "resources": {name: resources(selected, name) for name in RESOURCE_NAMES},
        "input_bytes": files(selected, "source_input_bytes" if raster else "input_bytes"),
        "logical_source_input_bytes": files(selected, "logical_source_input_bytes") if raster else None,
        "output_bytes": files(selected, "output_bytes"),
    }


def check_rss(partition, compared):
    rss = partition["resources"]["peak_rss_bytes"] if partition else None
    coverage = rss["coverage"] if rss else {}
    values = {
        "samples": coverage.get("sampled", 0) + coverage.get("partial", 0),
        "partial": coverage.get("partial", 0),
        "median": rss["median"] if rss else None,
        "p95_nearest_rank": rss["p95_nearest_rank"] if rss else None,
    }
    if values != compared:
        raise ValueError("Raw RSS coverage differs from the strict comparison")


def side_groups(documents, case, side, raster):
    attempt = documents[case, side, "COMPLETED"].decode("ascii").strip()
    raw = documents[case, side, attempt, "evidence", "trials.jsonl" if raster else "samples.jsonl"]
    groups = defaultdict(list)
    run_ids = set()
    for line in raw.splitlines():
        row = json.loads(line)
        run_id = row["run_id"]
        if not run_id or run_id in run_ids:
            raise ValueError(f"Missing or duplicate run identity in {case}/{side}")
        run_ids.add(run_id)
        groups[row["cache"]].append(row)
    if set(groups) != {"process_cold", "warm"}:
        raise ValueError(f"Missing cold or warm evidence in {case}/{side}")
    return raw, groups, run_ids


def read_pair(documents, case):
    pairs = [raw for parts, raw in documents.items() if len(parts) == 3 and parts[0] == case
             and (parts[1].startswith("pair.") and parts[2] == "before-after.json"
                  or parts[1].startswith("comparison.") and parts[2] == "result.json")]
    raw_pair, = pairs
    pair = json.loads(raw_pair)
    if pair["status"] != "MATCHED_EVIDENCE" or pair["runtime_configuration"]["level"] != "application_config_and_python_proj":
        raise ValueError(f"Expected a strict formal comparison for {case}")
    return raw_pair, pair


def case_summary(documents, case):
    raster = case in {"dem-s", "dem-m", "dem-l", "remote-s", "remote-l"}
    raw_pair, pair = read_pair(documents, case)
    series = {}
    ids = set()
    for role, side in SIDES.items():
        raw, groups, run_ids = side_groups(documents, case, side, raster)
        if ids & run_ids:
            raise ValueError(f"Reused run identity in {case}")
        ids.update(run_ids)
        by_cache = {}
        for compared in pair["groups"]:
            cache = compared["group"][2]
            rows = groups[cache]
            measured = [row for row in rows if phase(row) == "measurement"]
            warmups = [row for row in rows if phase(row) == "warmup"]
            counts = Counter(row["status"] for row in measured)
            if (
                len(rows) != 7 or len(warmups) != 2 or len(measured) != 5 or cache in by_cache
                or dict(counts) != {state: count for state, count in compared[role]["counts"].items() if count}
            ):
                raise ValueError(f"Raw phase or outcome counts differ from the strict comparison in {case}/{side}/{cache}")
            measurements = {state: measured_partition(measured, state, raster) for state in STATUSES}
            for state in ("PASS", "FAIL"):
                check_rss(measurements[state], compared[role]["peak_rss_bytes"][state])
            by_cache[cache] = {
                "warmups": dict(sorted(Counter(row["status"] for row in warmups).items())),
                "measurements": measurements,
            }
        if by_cache.keys() != groups.keys():
            raise ValueError(f"Comparison groups do not cover {case}/{side}")
        series[side] = {
            "source_commit": pair[f"{role}_source"],
            "raw_rows_sha256": sha256(raw).hexdigest(),
            "groups": by_cache,
        }
    return {
        "strict_comparison_sha256": sha256(raw_pair).hexdigest(),
        "runtime_configuration_sha256": pair["runtime_configuration"]["sha256"],
        "candidate_all_measurements_pass": pair["candidate_all_measurements_pass"],
        "successful_sample_deltas": {group["group"][2]: group["successful_sample_deltas"] for group in pair["groups"]},
        "sides": series,
    }, ids


def sealed_followups(directory, historical, historical_ids, include_dem_m):
    remote_name = "p13-05-remote-l-pair-evidence.tar.gz"
    remote_docs = archive_documents(directory / remote_name, {"remote-l"})
    remote, remote_ids = case_summary(remote_docs, "remote-l")

    t14_name = "p13-05-t14-local-clip-pair-evidence.tar.gz"
    case = "t14-split_by_area"
    frozen = archive_documents(directory / "p13-05-vector-l-area-evidence.tar.gz", {case})
    t14_docs = {parts: raw for parts, raw in frozen.items() if parts[:2] == (case, "base")}
    followup = archive_documents(directory / t14_name, {"candidate", "comparison"})
    marker = followup["candidate", "COMPLETED"]
    attempt = marker.decode("ascii").strip()
    t14_docs[case, "candidate", "COMPLETED"] = marker
    t14_docs[case, "candidate", attempt, "evidence", "samples.jsonl"] = followup["candidate", "evidence", "samples.jsonl"]
    t14_docs[case, "comparison.followup", "result.json"] = followup["comparison", "result.json"]
    t14, t14_ids = case_summary(t14_docs, case)
    _, _, reused = side_groups(t14_docs, case, "base", raster=False)
    expected_base = historical[f"vector-l-area/{case}"]["sides"]["base"]
    if t14["sides"]["base"] != expected_base or (t14_ids & historical_ids) != reused:
        raise ValueError("T14 follow-up does not reuse exactly the sealed original side")
    if remote_ids & (historical_ids | t14_ids):
        raise ValueError("Reused run identity in the remote L pair")

    sides = [side for pair in historical.values() for side in pair["sides"].values()]
    sides.extend((remote["sides"]["base"], remote["sides"]["candidate"], t14["sides"]["candidate"]))
    distinct = historical_ids | remote_ids | t14_ids
    pairs = {
        "remote-l-pair/remote-l": {"archive": remote_name, **remote},
        f"t14-local-clip-pair/{case}": {"archive": t14_name, **t14},
    }
    if include_dem_m:
        dem_name = "p13-05-dem-m-retry-pair-evidence.tar.gz"
        dem_docs = archive_documents(directory / dem_name, {"dem-m"})
        dem, dem_ids = case_summary(dem_docs, "dem-m")
        if distinct & dem_ids:
            raise ValueError("Reused run identity in the DEM M retry pair")
        distinct.update(dem_ids)
        sides.extend(dem["sides"].values())
        pairs["dem-m-retry-pair/dem-m"] = {"archive": dem_name, **dem}
    return {
        "schema_version": 1,
        "historical_pairs": len(historical),
        "distinct_runs": len(distinct),
        "reused_t14_original_runs": len(reused),
        "unique_measurements": measurement_totals(sides),
        "pairs": pairs,
    }


def measurement_totals(sides):
    statuses = Counter()
    terminal = Counter()
    observation_failures = 0
    verified = 0
    coverage = {name: Counter() for name in RESOURCE_NAMES}
    for side in sides:
        for group in side["groups"].values():
            for status, part in group["measurements"].items():
                if part is None:
                    continue
                statuses[status] += part["rows"]
                terminal.update(part["terminal_outcomes"])
                observation_failures += part["status_observation_failures"]
                verified += part["independent_verification_recorded"]
                for name in RESOURCE_NAMES:
                    coverage[name].update(part["resources"][name]["coverage"])
    return {
        "benchmark_outcomes": dict(sorted(statuses.items())),
        "terminal_outcomes": dict(sorted(terminal.items())),
        "status_observation_failures": observation_failures,
        "independent_verification_recorded": verified,
        "resources": {name: dict(sorted(counts.items())) for name, counts in coverage.items()},
    }


def summarize(directory, include_l_followups=False, include_dem_m=False):
    output = {}
    ids = set()
    for name, cases in ARCHIVES.items():
        archive = f"p13-05-{name}-evidence.tar.gz"
        documents = archive_documents(directory / archive, cases)
        for case in cases:
            summary, run_ids = case_summary(documents, case)
            if ids & run_ids:
                raise ValueError(f"Run identity reused across archives: {case}")
            ids.update(run_ids)
            output[f"{name}/{case}"] = {"archive": archive, **summary}
    if include_l_followups or include_dem_m:
        return sealed_followups(directory, output, ids, include_dem_m)
    return {"schema_version": 1, "distinct_runs": len(ids), "pairs": output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_directory", type=Path)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--l-followups", action="store_true", help="Include remote L and the T14 local-clip comparison")
    scope.add_argument("--all-sealed", action="store_true", help="Include the L follow-ups and the DEM M retry pair")
    args = parser.parse_args()
    summary = summarize(args.evidence_directory, include_l_followups=args.l_followups, include_dem_m=args.all_sealed)
    print(json.dumps(summary, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
