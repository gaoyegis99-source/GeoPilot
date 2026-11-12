"""Recompute scheduler observations from sealed P13-07 and P13-08 evidence."""

import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
from statistics import median
import sys
import tarfile
import xml.etree.ElementTree as ET


EVIDENCE = Path(__file__).resolve().parents[2] / "docs/round2/performance"
NORMAL = "p13-07-normal-status-lock-evidence.tar.gz"
NATIVE = "p13-07-native-status-lock-evidence.tar.gz"
JUNIT = "p13-07-junit.xml"
CONSOLE = "p13-07-pytest.log"
LIFECYCLE = "p13-08-daemon-lifecycle.jsonl"
_SQLITE_LOCK_CALL = re.compile(
    rb"(\d+\.\d+) fcntl\(\d+<[^>]+/project\.sqlite>, F_SETLK, "
    rb"\{l_type=(\w+), l_whence=SEEK_SET, l_start=(\d+), l_len=(\d+)\}\) = 0 <(\d+\.\d+)>"
)
_LOCK_EVENTS = {
    (b"F_WRLCK", 1_073_741_825): "reserved_at_us",
    (b"F_WRLCK", 1_073_741_824): "pending_at_us",
    (b"F_WRLCK", 1_073_741_826): "exclusive_at_us",
    (b"F_RDLCK", 1_073_741_826): "exclusive_release_us",
    (b"F_UNLCK", 1_073_741_824): "reserved_release_us",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def json_value(raw):
    def unique(pairs):
        value = dict(pairs)
        require(len(value) == len(pairs), "Duplicate JSON key")
        return value

    def invalid_constant(constant):
        raise ValueError(f"Invalid JSON constant {constant}")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def records(raw):
    rows = [json_value(line) for line in raw.splitlines()]
    require(rows and all(isinstance(row, dict) for row in rows), "Expected nonempty JSONL object records")
    return rows


def archive_members(raw):
    members = {}
    with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and ".." not in name.parts, f"Unsafe archive member {member.name}")
            if member.isdir():
                continue
            key = name.as_posix()
            require(member.isfile() and key not in members, f"Invalid or duplicate archive member {key}")
            with archive.extractfile(member) as stream:
                members[key] = stream.read()
    manifest = members.pop("SHA256SUMS").decode("ascii")
    sealed = {}
    for line in manifest.splitlines():
        entry = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        require(entry is not None, "Malformed archive SHA256SUMS")
        digest, name = entry.groups()
        name = PurePosixPath(name).as_posix()
        require(name not in sealed, f"Duplicate checksum for {name}")
        sealed[name] = digest
    require(sealed.keys() == members.keys(), "Archive members differ from SHA256SUMS")
    for name, content in members.items():
        require(sha256(content).hexdigest() == sealed[name], f"Archive checksum differs for {name}")
    return members


def http_observations(members, group):
    rows = records(members[f"{group}/http.jsonl"])
    summary = json_value(members[f"{group}/summary.json"])
    by_kind = {}
    observed_states = Counter()
    observed_terminals = Counter()
    for row in rows:
        kind, status, wall = row["kind"], row["http"], row["wall_ns"]
        require(kind in {"status", "submission", "artifacts", "steps"}, f"{group}: unknown HTTP record kind")
        require(type(status) is int and 100 <= status <= 599 and type(wall) is int and wall >= 0,
                f"{group}: invalid HTTP status or duration")
        by_kind.setdefault(kind, []).append(row)
        if kind == "status" and status == 200:
            state = (row["execution_status"], row["check_status"])
            observed_states["/".join(state)] += 1
            if state == ("SUCCEEDED", "PASSED"):
                observed_terminals[row["run_id"], row["reader"]] += 1
    requests = {}
    for kind, samples in by_kind.items():
        durations = sorted(row["wall_ns"] for row in samples)
        requests[kind] = {
            "codes": dict(Counter(str(row["http"]) for row in samples)),
            "latency": {f"p{pct}_ms": round(durations[(len(durations) * pct + 99) // 100 - 1] / 1_000_000, 3)
                        for pct in (50, 95, 99, 100)},
        }
    require(requests == summary["requests"] and observed_states == summary["run_status_states"],
            f"{group}: raw HTTP observations differ from recorded summary")
    count, readers = summary["runs_requested"], summary["readers_per_run"]
    require(type(count) is int and count > 0 and type(readers) is int and readers > 0,
            f"{group}: invalid run or reader count")
    runs = summary["runs"]
    run_ids = {row["run_id"] for row in runs}
    require(len(runs) == len(run_ids) == count and requests["submission"]["codes"] == {"202": count}
            and observed_terminals == {(run_id, reader): 1 for run_id in run_ids for reader in range(readers)},
            f"{group}: missing or contradictory terminal observations")
    require(summary["integrity_check"] == "ok" and summary["journal_left_after_stop"] is False
            and summary["artifacts"] == summary["passed_reports"] == count,
            f"{group}: inconsistent persisted success evidence")
    for run in runs:
        require((run["db_execution"], run["db_check"]) == ("SUCCEEDED", "PASSED")
                and run["steps"] == [{"status": "SUCCEEDED", "exit_code": 0}] * 3
                and run["manifest"] is True and run["compatibility_report"] is True
                and len(run["terminal_observations"]) == readers
                and all((terminal["run_id"], terminal["execution_status"], terminal["check_status"]) == (
                    run["run_id"], "SUCCEEDED", "PASSED",
                ) for terminal in run["terminal_observations"]),
                f"{group}: inconsistent run {run['run_id']}")
    require(re.fullmatch(r"[0-9a-f]{40}", summary["source_revision"].split()[0]) is not None,
            f"{group}: missing exact source revision")
    pragmas = summary["pragmas"]
    require(pragmas["journal_mode"] == "delete" and pragmas["synchronous"] == 2 and pragmas["busy_timeout"] == 5000,
            f"{group}: expected default DELETE/FULL/5000ms settings")
    return {"source_revision": summary["source_revision"].split()[0], "sqlite": summary["sqlite"], "pragmas": pragmas,
            "runs": count, "readers_per_run": readers, "reader_interval_s": summary["reader_interval_s"],
            "requests": requests, "diagnostics": summary["diagnostics"]}


def trace_times(lines):
    events = {}
    for line in lines:
        match = _SQLITE_LOCK_CALL.fullmatch(line)
        if match is None:
            continue
        start, operation, offset, length, duration = match.groups()
        key = _LOCK_EVENTS.get((operation, int(offset)))
        if operation == b"F_UNLCK" and length != b"2":
            continue
        if key is not None:
            require(key not in events, f"Repeated SQLite lock event {key}")
            events[key] = int((Decimal(start.decode("ascii")) + Decimal(duration.decode("ascii"))) * 1_000_000)
    return events


def lock_observations(members):
    document = json_value(members["lock-windows.json"])
    trace = members[f"strace/{document['scheduler_trace']}"]
    lines = trace.splitlines()
    windows = document["scheduler_write_windows"]
    groups = {"all_scheduler_lock_upgrades": [], "all_commits": [], "no_observed_journal_write": []}
    previous_end = 0
    for row in windows:
        start, end = row["trace_line_start"], row["trace_line_end"]
        require(type(start) is int and type(end) is int and previous_end < start < end <= len(lines),
                "SQLite lock window has contradictory trace locations")
        previous_end = end
        expected = {key: row[key] for key in _LOCK_EVENTS.values()}
        require(trace_times(lines[start - 1:end]) == expected, "SQLite lock events differ from the raw trace")
        times = list(expected.values())
        require(all(type(value) is int and value > 0 for value in times) and times == sorted(times),
                "SQLite lock timestamps are invalid or out of order")
        reserved, pending, exclusive, exclusive_end, release = times
        durations = {"reserved_us": release - reserved, "read_blocking_us": release - pending,
                     "exclusive_us": exclusive_end - exclusive}
        require(all(value == row[key] for key, value in durations.items()), "SQLite lock duration differs from timestamps")
        groups["all_scheduler_lock_upgrades"].append(durations)
        has_commit = any(event["event"] == "journal_unlink" for event in row["file_ops"])
        groups["all_commits" if has_commit else "no_observed_journal_write"].append(durations)
        if has_commit:
            stage = row["production_stage"]
            if stage.startswith("start_"):
                stage = "start_steps"
            elif stage.startswith("result_"):
                stage = "save_step_results"
            if stage in {"start_steps", "save_step_results", "register_artifacts", "publish_check"}:
                groups.setdefault(stage, []).append(durations)
    result = {}
    for group, rows in groups.items():
        require(rows, f"Missing SQLite lock windows for {group}")
        result[group] = {}
        for key in ("reserved_us", "read_blocking_us", "exclusive_us"):
            values = [row[key] for row in rows]
            result[group][key] = {"n": len(values), "min_ms": min(values) / 1000,
                                  "median_ms": median(values) / 1000, "max_ms": max(values) / 1000}
    require(result == {row["stage"]: {k: v for k, v in row.items() if k != "stage"}
                       for row in document["scheduler_commit_stats"]}, "SQLite lock summaries differ from raw windows")
    failed_calls = sum(line.count(b"project.sqlite>, F_SETLK, ") for name, raw in members.items()
                       if name.startswith("strace/") for line in raw.splitlines() if b") = -1 " in line)
    require(failed_calls == sum(map(len, document["failed_sqlite_kernel_lock_calls"].values())),
            "Failed SQLite kernel lock counts differ from the raw traces")
    return {"trace": document["scheduler_trace"], "windows": result, "failed_sqlite_kernel_lock_calls": failed_calls}


def pressure_observations(junit, console):
    root = ET.fromstring(junit)
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    outcomes = Counter()
    cases = {}
    for suite in suites:
        counts = Counter()
        for case in suite.findall("testcase"):
            key = (case.attrib["classname"], case.attrib["name"])
            markers = [status for status in ("failure", "error", "skipped") if case.find(status) is not None]
            require(key not in cases and len(markers) <= 1, f"Contradictory JUnit result for {key}")
            outcome = markers[0] if markers else "passed"
            cases[key] = outcome
            counts[outcome] += 1
        require(sum(counts.values()) == int(suite.attrib["tests"]) and all(
            counts[status] == int(suite.attrib.get(attribute, "0"))
            for status, attribute in (("failure", "failures"), ("error", "errors"), ("skipped", "skipped"))
        ), "JUnit suite counts differ from its test cases")
        outcomes.update(counts)

    pressure = "tests.stress.scheduler.test_queue_contention"
    required = [(pressure, name) for name in (
        "test_scheduler_resumes_after_real_claim_busy_timeout", "test_scheduler_settles_a_claim_after_timeout_without_reexecution",
        "test_cancellation_burst_and_restart_drain_the_queue_once", "test_shutdown_during_settlement_contention_leaves_recoverable_run",
    )] + [("tests.scheduler.test_cancellation", "test_active_cancel_waits_for_real_gis_worker_exit_and_stops_dispatch")]
    require(all(cases.get(key) == "passed" for key in required), "Required pressure and cancellation cases did not pass")
    text = console.decode("utf-8")
    queue = re.findall(r"^[.s]*(\d+) cancellation requests across (\d+) queued runs in (\d+\.\d+)s$", text, re.M)
    native = re.findall(r"^[.s]*Native GIS cancellation to worker exit: (\d+\.\d+)s$", text, re.M)
    total = re.findall(r"^(\d+) passed, (\d+) skipped(?:, \d+ warnings?)? in (\d+\.\d+)s(?: \([^\n]*\))?$", text, re.M)
    require(len(queue) == len(native) == len(total) == 1, "Missing or repeated pressure console measurement")
    requests, queued, queue_seconds = queue[0]
    passed, skipped, seconds = total[0]
    require(outcomes == {"passed": int(passed), "skipped": int(skipped)} and 0 < int(queued) <= int(requests)
            and 0 < float(queue_seconds) <= float(seconds) and 0 < float(native[0]) <= float(seconds),
            "Pressure console contradicts JUnit or has invalid measurements")
    return {"junit": dict(outcomes), "required_cases": ["::".join(key) for key in required],
            "suite_seconds": float(seconds), "queued_cancel_requests": int(requests), "queued_runs_cancelled": int(queued),
            "queued_cancel_seconds": float(queue_seconds), "native_cancel_to_worker_exit_seconds": float(native[0])}


def lifecycle_observations(raw):
    rows = records(raw)

    def event(scope, name):
        matches = [row for row in scope if row["event"] == name]
        require(len(matches) == 1, f"Expected one lifecycle event {name}")
        return matches[0]

    def milliseconds(start, end):
        elapsed = (datetime.fromisoformat(end["utc"]) - datetime.fromisoformat(start["utc"])).total_seconds()
        require(elapsed >= 0, "Lifecycle timestamps are out of order")
        return round(elapsed * 1000, 3)

    environment = event(rows, "environment")
    image = environment["image_command"][-1]
    require(event(rows, "complete")["exit_code"] == 0 and re.fullmatch(r"sha256:[0-9a-f]{64}", image),
            "Invalid or incomplete lifecycle evidence")
    cases = {}
    for case, timed_out, cancelled in (
        ("native-timeout", True, False), ("docker-timeout", True, False),
        ("docker-cancel", False, True), ("client-kill", False, False),
    ):
        scope = [row for row in rows if row.get("case") == case]
        command, attached, reaped, returned = (event(scope, name) for name in (
            "capture_command", "local_attached", "local_reaped", "capture_returned",
        ))
        outcome = returned["outcome"]
        external = case != "native-timeout"
        exit_code = -9 if external else -15
        process_key = "client_proc_exists" if external else "proc_exists"
        require(attached["pid"] == reaped["pid"] and attached["lease_available"] is False
                and outcome["exit_code"] == reaped["exit_code"] == exit_code and outcome["timed_out"] is timed_out
                and outcome["cancelled"] is cancelled and reaped["lease_available"] is False
                and reaped["proc_exists"] is False and returned["lease_available"] is True and returned[process_key] is False,
                f"{case}: inconsistent local process or lease evidence")
        milliseconds(command, attached)
        milliseconds(attached, reaped)
        milliseconds(reaped, returned)
        result = {"capture_ms": milliseconds(command, returned), "timeout_s": command["timeout_s"],
                  "local_exit_code": exit_code, "local_lease_released": True}
        if external:
            observation = event(scope, "client_stop_observation")
            removal = event(scope, "remove_owned_container")
            container = observation["container_id"]
            require(re.fullmatch(r"[0-9a-f]{64}", container) and observation["container_present"] is True
                    and observation["container_running"] is True and observation["local_lease_available"] is True
                    and removal["exit_code"] == 0 and removal["command"][-1] == container,
                    f"{case}: contradictory external container lifecycle")
            argv = command["argv"]
            name = argv[argv.index("--name") + 1]
            owner = argv[argv.index("--label") + 1].split("=", 1)[1]
            inspections = [row["observed"] for row in scope if row["event"] == "inspect"]
            require(inspections and all(row["id"] == container and row["name"] == "/" + name and row["image"] == image
                                        and row["owner"] == owner and row["running"] is True for row in inspections),
                    f"{case}: container identity or live observation differs")
            missing = [row for row in scope if row["event"] == "inspect_missing"]
            require(len(missing) == 2 and {row["command"][-1] for row in missing} == {container, name}
                    and all(row["exit_code"] == 1 and "No such container" in row["stderr"] for row in missing),
                    f"{case}: missing container removal verification")
            milliseconds(observation, removal)
            for row in missing:
                milliseconds(removal, row)
            result.update(container_running_after_local_exit=True,
                          first_live_observation_after_return_ms=milliseconds(returned, observation),
                          verified_absent_after_return_ms=milliseconds(returned, missing[-1]))
        cases[case] = result
    return {"source_revision": environment["source_commit"], "docker_client_server": environment["docker_client_server"],
            "image": image, "cases": cases}


def summarize(directory):
    evidence = {name: (directory / name).read_bytes() for name in (NORMAL, NATIVE, JUNIT, CONSOLE, LIFECYCLE)}
    normal, native = (archive_members(evidence[name]) for name in (NORMAL, NATIVE))
    for name in ("agent/standard.exit-code", "agent/concurrent.exit-code", "agent/traced.exit-code",
                 "independent/probe.exit", "lock-windows.exit-code"):
        require(normal[name].strip() == b"0", f"Failed scheduler observation {name}")
    for name in ("disk.exit-code", "tmpfs.exit-code"):
        require(native[name].strip() == b"0", f"Failed native observation {name}")
    normal_groups = {group: http_observations(normal, group)
                     for group in ("agent/standard", "agent/concurrent", "agent/traced", "independent")}
    native_groups = {group: http_observations(native, group)
                     for group in ("sda2/standard", "sda2/concurrent", "tmpfs/standard", "tmpfs/concurrent")}
    totals = {}
    for label, groups in (("normal", normal_groups), ("native", native_groups)):
        counts = Counter()
        for group in groups.values():
            counts.update(group["requests"]["status"]["codes"])
        totals[label] = dict(counts)
    return {"schema_version": 1, "evidence_sha256": {name: sha256(raw).hexdigest() for name, raw in evidence.items()},
            "normal_http": normal_groups, "native_http": native_groups, "status_http_totals": totals,
            "sqlite_locks": lock_observations(normal),
            "pressure": pressure_observations(evidence[JUNIT], evidence[CONSOLE]),
            "lifecycle": lifecycle_observations(evidence[LIFECYCLE]),
            "worker_capacity": {"observed_active_scheduler_slots": 1, "validated_parallel_worker_count": None,
                                "scope": "Concurrent clients were measured with one active scheduler slot."}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE, help="directory containing sealed scheduler evidence")
    args = parser.parse_args(argv)
    try:
        result = json.dumps(summarize(args.evidence_dir), indent=2, allow_nan=False)
    except (
        OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError, RecursionError,
        tarfile.TarError, ET.ParseError,
    ) as error:
        print(f"summarize scheduler: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
