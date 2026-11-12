"""Run a fixed GeoPilot benchmark with pre- and post-run configuration evidence."""

import argparse
from hashlib import sha256
from inspect import signature
import json
import os
from pathlib import Path
import signal
from stat import S_ISREG
import subprocess
import sys
from tempfile import NamedTemporaryFile


ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS = {
    "raster": ("run_raster.py", ("report.json",), ("trials.jsonl",)),
    "vector": ("run_vector.py", ("summary.json", "environment.json"), ("inputs.json", "samples.jsonl")),
}
RECEIPT = "runtime-configuration.json"


def _require_readonly(path):
    if not os.statvfs(path).f_flag & os.ST_RDONLY:
        raise ValueError(f"Benchmark configuration must be on a read-only mount: {path}")


def _capture(config):
    _require_readonly(config)
    sys.path.insert(0, str(ROOT / "src"))
    from _runtime_config import runtime_config_snapshot
    from geopilot.api.server import create_local_app

    if signature(create_local_app).parameters["config"].default != config:
        raise ValueError("Benchmark configuration differs from the loaded application's source tree")
    snapshot = runtime_config_snapshot(config)
    for family, files in snapshot["config"].items():
        if files is not None:
            _require_readonly(config / family)
            for name in files:
                _require_readonly(config / family / name)
    _require_readonly(config)
    print(json.dumps(snapshot, allow_nan=False))


def _snapshot(config):
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--_capture", str(config)],
        stdout=subprocess.PIPE, check=True,
    )
    return json.loads(result.stdout)


def _run_benchmark(command):
    interrupted = None
    pending = None
    child = None

    def forward(signum, _frame):
        nonlocal interrupted, pending
        interrupted = signum
        if child is None:
            pending = signum
        else:
            send_to_child(signum)

    def send_to_child(signum):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    watched = (signal.SIGINT, signal.SIGTERM)
    previous = {signum: signal.signal(signum, forward) for signum in watched}
    try:
        child = subprocess.Popen(command, start_new_session=True)
        if pending is not None:
            send_to_child(pending)
        returncode = child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if interrupted is not None:
        return 128 + interrupted
    return returncode if returncode >= 0 else 128 - returncode


def _read_report(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Expected a regular report file: {path}")
        return stream.read()


def _raster_finished(report, returncode):
    if report["mode"] != "formal" or report.get("error") is not None:
        return False
    groups = report["groups"]
    expected = report["expected_groups"]
    fields = ("tier", "workload", "cache")
    return (
        bool(groups)
        and [tuple(group[key] for key in fields) for group in groups]
        == [tuple(group[key] for key in fields) for group in expected]
        and all(group["observed_trials"] == group["expected_trials"] == 7 and group["interrupted"] is False
                and "last_fingerprint_error" not in report["inputs"][group["tier"]] for group in groups)
        and report["status"] == ("FAIL" if returncode else "PASS")
    )


def _vector_finished(report, environment, returncode):
    if report["probe"] is not False or environment["probe"] is not False:
        return False
    groups = report["groups"]
    counts = report["sample_counts"]
    if not groups or not report["finished_utc"] or counts["NOT_RUN"] != 0:
        return False
    phases = (("warmup", 2), ("measured", 5))
    return (
        all(group[f"{phase}_not_run"] == 0 and group[f"{phase}_pass"] + group[f"{phase}_fail"] == planned
            for group in groups for phase, planned in phases)
        and all(counts[state] == sum(group[f"{phase}_{state.lower()}"] for group in groups for phase, _ in phases)
                for state in ("PASS", "FAIL"))
        and report["status"] == ("INCOMPLETE" if returncode else "PASS")
        and bool(counts["FAIL"]) == bool(returncode)
    )


def _final_report_files(output, kind, returncode):
    _, report_names, companions = BENCHMARKS[kind]
    if not all(S_ISREG((output / name).lstat().st_mode) for name in companions):
        raise ValueError("The benchmark is missing a regular raw report file")
    files = {name: _read_report(output / name) for name in report_names}
    documents = [json.loads(raw.decode("utf-8")) for raw in files.values()]
    finished = _raster_finished if kind == "raster" else _vector_finished
    if not finished(*documents, returncode):
        raise ValueError("The benchmark has no complete formal report")
    return {name: sha256(raw).hexdigest() for name, raw in files.items()}


def _publish(output, document):
    temp = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output, prefix=f".{RECEIPT}.", delete=False) as stream:
            temp = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, output / RECEIPT)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--kind", choices=BENCHMARKS, required=True)
    parser.add_argument("--config", type=Path, required=True, help="the fixed benchmark source tree's config directory")
    parser.add_argument("--output", type=Path, required=True, help="new benchmark output directory")
    parser.add_argument("options", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    config = args.config.absolute()
    if config != ROOT / "config":
        parser.error("--config must be the benchmark's own source tree config directory")
    if args.options[:1] != ["--"]:
        parser.error("pass benchmark options after --")
    options = args.options[1:]
    reserved = ("--output", "--config", "--_worker")
    if any(option == "--" or any(name.startswith(option.split("=", 1)[0]) for name in reserved)
           for option in options if option.startswith("--")):
        parser.error("the wrapper owns configuration, output and worker selection")
    if args.kind == "raster" and os.environ.get("PROJ_NETWORK") != "OFF":
        parser.error("set PROJ_NETWORK=OFF in the outer raster runtime, matching its worker environment")
    output = args.output.absolute()
    if os.path.lexists(output):
        parser.error("--output must be new")
    benchmark, _, _ = BENCHMARKS[args.kind]
    command = [sys.executable, "-B", str(Path(__file__).resolve().with_name(benchmark)),
               *options, "--output", str(output)]
    try:
        before = _snapshot(config)
        returncode = _run_benchmark(command)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"runtime configuration: benchmark did not start: {error}", file=sys.stderr)
        return 2
    if returncode not in (0, 1):
        print(f"runtime configuration: benchmark exited {returncode}; no receipt", file=sys.stderr)
        return returncode
    try:
        after = _snapshot(config)
        if before != after:
            raise ValueError("runtime configuration changed between captures")
        reports = _final_report_files(output, args.kind, returncode)
        _publish(output, {"schema_version": 1, "report_files": reports, "before": before, "after": after})
    except (KeyError, OSError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"runtime configuration: benchmark exited {returncode}; no receipt: {error}", file=sys.stderr)
    return returncode


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--_capture":
        try:
            _capture(Path(sys.argv[2]))
        except Exception as error:
            print(f"runtime configuration capture: {type(error).__name__}: {error}", file=sys.stderr)
            raise SystemExit(2) from None
    else:
        raise SystemExit(main())
