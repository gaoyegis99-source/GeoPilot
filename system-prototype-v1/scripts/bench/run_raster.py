"""Run the P13-04 raster or remote-sensing tools and complete V1 method."""

import argparse
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import yaml

from validate_benchmark_manifest import MANIFEST, ROOT, TIERS, UniqueLoader, read_yaml

sys.path.insert(0, str(ROOT / "src"))

from _raster_metrics import RUNTIME, Sampler, distribution, environment, file_bytes
from _raster_reference import area_reference, load_fixture, raster_description, require, sha256
from _raster_workloads import MethodWorkload, dem_tools, read_ahead, remote_tools
from generate_raster import generate as generate_dem
from generate_remote_sensing import generate as generate_remote


CACHES = ("process_cold", "warm")
WORKLOADS = ("tools", "method")
FAMILIES = {"raster": ("raster.yaml", generate_dem), "remote_sensing": ("remote-sensing.yaml", generate_remote)}
OPERATIONS = {
    "raster": (
        "window_read_block_boundary", "window_read_source_edge", "window_read_nodata_neighborhood",
        "neighborhood_extent", "slope_degree", "slope_percent", "aspect",
    ),
    "remote_sensing": ("first_ndvi", "second_ndvi", "align_nearest", "change_delta"),
}


def publish(path, document):
    temp = path.with_name(f".{path.name}-{uuid4().hex}")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _invoke(operation, arguments, options, directory):
    error = None
    result = None
    with Sampler(directory) as sampler:
        started = time.monotonic_ns()
        try:
            result = operation(*arguments, **options)
        except Exception as caught:
            error = {"type": type(caught).__name__, "message": str(caught), "traceback": traceback.format_exc()}
        wall_ns = time.monotonic_ns() - started
    return result, {
        "step_worker_pid": os.getpid(), "wall_ns": wall_ns, "resources": sampler.resources(),
        "workspace_size": sampler.document(), "error": error,
    }


def _cold_operation(sender, operation, arguments, options, directory):
    with sender:
        sender.send(_invoke(operation, arguments, options, directory))


def _spawn_operation(operation, arguments, options, directory):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_cold_operation, args=(sender, operation, arguments, options, directory))
    started = time.monotonic_ns()
    result = None
    metrics = {}
    with receiver, sender:
        process.start()
        sender.close()
        try:
            if not receiver.poll(1800):
                raise TimeoutError(f"Cold operation exceeded 1800s: {operation.__name__}")
            result, metrics = receiver.recv()
        except (EOFError, OSError, TimeoutError) as error:
            metrics["error"] = {"type": type(error).__name__, "message": str(error) or "Cold worker sent no result"}
        finally:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join()
    metrics["step_worker_exit_code"] = process.exitcode
    metrics["wall_with_process_startup_ns"] = time.monotonic_ns() - started
    if process.exitcode != 0 and not metrics.get("error"):
        metrics["error"] = {"type": "ChildProcessError", "message": f"Cold worker exited {process.exitcode}"}
    return result, metrics


def measured_operation(trial, directory):
    def measure(name, operation, arguments, options, inputs, pixels, logical_bytes, outputs=()):
        record = {
            "step_key": name, "operation": f"{operation.__module__}.{operation.__name__}",
            "options": {key: str(value) if isinstance(value, Path) else value for key, value in options.items()},
            "inputs": [str(path) for path in inputs], "input_bytes": file_bytes(inputs), "logical_input_bytes": logical_bytes,
            "pixels": pixels, "exit_code": None, "status": "FAIL",
        }
        trial["steps"].append(record)
        if trial["cache"] == "warm":
            record["pre_read_bytes"] = read_ahead(inputs)
        invoke = _spawn_operation if trial["cache"] == "process_cold" else _invoke
        result, observation = invoke(operation, arguments, options, directory)
        record.update(observation)
        if observation["error"] is not None:
            child_exit = observation.get("step_worker_exit_code")
            if child_exit not in {None, 0}:
                record["exit_code"] = child_exit
            raise RuntimeError(f"{name}: {observation['error']['message']}")
        record.update(output_bytes=file_bytes(outputs), exit_code=0)
        if pixels is not None and record["wall_ns"]:
            record["pixels_per_second"] = pixels * 1e9 / record["wall_ns"]
        return result, record
    return measure


def worker():
    """One worker handles one cold job or a whole warm group."""
    with ExitStack() as stack:
        method = None
        for line in sys.stdin:
            job = json.loads(line)
            directory = Path(job["directory"])
            trial = {**job["identity"], "worker_pid": os.getpid(), "status": "FAIL", "exit_code": None, "steps": []}
            try:
                fixture = load_fixture(Path(job["fixture"]), trial["family"], trial["tier"])
                trial["manifest_sha256"] = fixture.manifest_sha256
                trial["logical_source_input_bytes"] = fixture.reference["raw_input_bytes"]
                trial["source_input_bytes"] = file_bytes(
                    path for role, path in fixture.files.items() if role != "native_quality_reference"
                )
                if trial["cache"] == "warm":
                    trial["pre_read_bytes"] = read_ahead(
                        path for role, path in fixture.files.items() if role != "native_quality_reference"
                    )
                if trial["workload"] == "method":
                    if method is None:
                        method = MethodWorkload(fixture, Path(job["service"]), ROOT, stack)
                        trial["service_initialization_ns"] = method.initialization_ns
                        trial["asset_registration_ns"] = method.registration_ns
                    areas = {int(key): value for key, value in job["area"].items()}
                    method.run(trial, job["profile"], areas, job["timeout"], Sampler)
                    if trial["wall_ns"]:
                        trial["pixels_per_second"] = trial["pixels"] * 1e9 / trial["wall_ns"]
                else:
                    invoke = measured_operation(trial, directory)
                    started = time.monotonic_ns()
                    if fixture.family == "raster":
                        dem_tools(fixture, job["profile"], directory, invoke)
                    else:
                        remote_tools(fixture, job["profile"], directory, invoke, ROOT)
                    trial["suite_wall_including_validation_ns"] = time.monotonic_ns() - started
                    trial["exit_code"] = 0
                if trial.get("status_poll_errors"):
                    trial["error"] = {
                        "type": "StatusObservationFailure",
                        "message": "Execution reached terminal state after an HTTP status-read error",
                    }
                else:
                    trial["status"] = "PASS"
            except Exception as error:
                trial["error"] = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
            finally:
                for step in trial["steps"]:
                    if "correctness" in step and step["exit_code"] == 0:
                        step["status"] = "PASS"
                publish(directory / "trial.json", trial)
    return 0


class WorkerProcess:
    def __init__(self, service):
        self.service = service
        service.mkdir()
        self.log = (service / "worker.log").open("x", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--_worker"], stdin=subprocess.PIPE, stdout=self.log,
                stderr=self.log, text=True, env={**os.environ, **RUNTIME},
            )
        except BaseException:
            self.log.close()
            raise

    def run(self, job, trial_file):
        self.process.stdin.write(json.dumps({**job, "service": str(self.service)}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + job["timeout"] + 240
        while not trial_file.is_file():
            if self.process.poll() is not None:
                raise RuntimeError(f"Worker exited {self.process.returncode}: {self.log.name}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"Worker did not complete within its allotted time: {self.log.name}")
            time.sleep(0.1)
        return json.loads(trial_file.read_text())

    def close(self):
        try:
            self.process.stdin.close()
            self.process.wait(timeout=15)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        finally:
            self.log.close()
        return self.process.returncode


def _group(root, fixture, profile, area, cache, workload, pilot, timeout, sink):
    directory = root / "samples" / fixture.tier / workload / cache
    directory.mkdir(parents=True)
    labels = ("pilot",) if pilot else ("warmup",) * 2 + ("measurement",) * 5
    first = fixture.snapshot()
    trials = []
    warm = WorkerProcess(directory / "service") if cache == "warm" else None
    try:
        for index, label in enumerate(labels, 1):
            sample = directory / f"{index:02d}-{label}"
            sample.mkdir()
            identity = {
                "sample_id": f"{fixture.family}/{fixture.tier}/{workload}/{cache}/{index:02d}",
                "index": index, "family": fixture.family, "tier": fixture.tier,
                "workload": workload, "cache": cache, "kind": label,
            }
            job = {"identity": identity, "directory": str(sample), "fixture": str(fixture.directory),
                   "profile": profile, "area": area, "timeout": timeout}
            process = warm or WorkerProcess(sample / "service")
            fatal = False
            try:
                trial = process.run(job, sample / "trial.json")
                require(all(trial[key] == value for key, value in identity.items()), "Unexpected worker trial identity")
                fatal = bool(trial.get("stop_group"))
            except Exception as error:
                trial = {**identity, "status": "FAIL", "steps": [],
                         "exit_code": process.process.poll(), "error": {"type": type(error).__name__, "message": str(error)}}
                fatal = True
            if warm is None or fatal:
                trial["worker_exit_code"] = process.close()
            if fatal or trial.get("worker_exit_code") not in {None, 0}:
                trial["status"] = "FAIL"
            trial["worker_log"] = process.log.name
            trials.append(trial)
            sink.write(json.dumps(trial, ensure_ascii=False, allow_nan=False) + "\n")
            sink.flush()
            print(f"{trial['sample_id']} {label} {trial['status']} "
                  f"{trial.get('wall_ns', trial.get('suite_wall_including_validation_ns'))}", flush=True)
            if fatal:
                break
    finally:
        if warm is not None and not warm.log.closed:
            warm_exit = warm.close()
        else:
            warm_exit = None
    last = fixture.snapshot()
    require(first == last, "Input fixture changed between the first and last group fingerprints")
    return {"cache": cache, "tier": fixture.tier, "workload": workload, "warm_worker_exit": warm_exit,
            "input_fingerprints_before": first, "input_fingerprints_after": last,
            "expected_trials": len(labels), "observed_trials": len(trials), "interrupted": fatal,
            "trial_failures": sum(row["status"] != "PASS" for row in trials),
            "operations": summarize_trials(trials, ("method",) if workload == "method" else OPERATIONS[fixture.family],
                                           pilot=pilot)}


def summarize_trials(trials, operations, *, pilot=False):
    expected = 1 if pilot else 5
    summaries = {}
    for operation in operations:
        attempts = [row for row in trials if row["kind"] == ("pilot" if pilot else "measurement")]
        rows = [row if operation == "method" else next((step for step in row["steps"] if step["step_key"] == operation), None)
                for row in attempts]
        valid = [row for row in rows if row and row["status"] == "PASS"]
        failed = sum(row is not None and row["status"] != "PASS" for row in rows)
        stats = {"passed": len(valid), "failed": failed, "missing": expected - len(valid) - failed}
        if not pilot and len(valid) >= 3 and len(attempts) == expected:
            for field in ("wall_ns", "pixels_per_second"):
                values = [row[field] for row in valid if row.get(field) is not None]
                if len(values) >= 3:
                    stats[field] = distribution(values)
            rss = [row.get("telemetry", row).get("resources", {}).get("peak_rss_bytes", {}) for row in valid]
            values = [sample["value"] for sample in rss if sample.get("status") == "sampled"]
            stats["sampled_peak_rss_count"] = len(values)
            if len(values) >= 3:
                stats["peak_rss_bytes"] = distribution(values)
        summaries[operation] = stats
    return summaries


def validate_profile(path):
    profile = yaml.load(path.expanduser().read_text(encoding="utf-8"), Loader=UniqueLoader)
    require(isinstance(profile, dict), "Expected a profile mapping")
    family = profile.get("family")
    require(family in FAMILIES, "Expected the P13-01 raster or remote_sensing profile")
    frozen = read_yaml(ROOT, "benchmarks/datasets/" + FAMILIES[family][0])
    require(profile == frozen, "Profile differs from the accepted P13-01 fixture recipe")
    return family, profile


def _raster_inputs(fixture, profile):
    roles = ("dem",) if fixture.family == "raster" else ("first_product", "second_product", "native_quality_reference")
    records = {role: raster_description(fixture.files[role]) for role in roles}
    side = profile["tiers"][fixture.tier]["side_pixels"]
    sizes = {"dem": side, "first_product": side, "second_product": side * 3 + 2, "native_quality_reference": side * 3 // 2 + 1}
    bands = {"dem": 1, "first_product": 4, "second_product": 3, "native_quality_reference": 1}
    for role, data in records.items():
        grid = fixture.grid(role)
        require(data["height"] == data["width"] == sizes[role] and grid.shape == (sizes[role], sizes[role])
                and data["crs"] == grid.crs == profile["crs"] and data["transform"] == list(grid.affine[:6])
                and data["bands"] == bands[role] and data["nodata"] == [profile.get("nodata")] * bands[role]
                and data["dtypes"] == [profile["dtype"]] * data["bands"]
                and data["block_shapes"] == [[profile["block_pixels"]] * 2] * data["bands"]
                and data["compression"] is None, f"Fixture grid differs for {role}")
    return records


def run(args):
    family, profile = validate_profile(args.profile)
    output = args.output or Path(tempfile.mkdtemp(prefix=f"geopilot-p13-04-{family}-")) / "benchmark"
    output = output.expanduser().absolute()
    output.mkdir()
    os.environ.update(RUNTIME)
    report = {
        "schema_version": 1, "family": family, "status": "RUNNING", "mode": "pilot" if args.pilot else "formal",
        "command": [sys.executable, *sys.argv], "output": str(output),
        "benchmark_manifest_sha256": sha256(ROOT / MANIFEST), "profile_sha256": sha256(args.profile.expanduser()),
        "implementation_sha256": {name: sha256(Path(__file__).with_name(name)) for name in (
            "run_raster.py", "_raster_metrics.py", "_raster_reference.py", "_raster_workloads.py",
        )},
        "environment": environment(ROOT, output, args.source_commit),
        "expected_groups": [
            {"workload": workload, "tier": tier, "cache": cache}
            for workload in args.workload for tier in args.tiers for cache in args.cache
        ],
        "inputs": {}, "groups": [],
    }
    report_path = output / "report.json"
    publish(report_path, report)
    fixtures = {}
    try:
        input_root = args.input_root.expanduser().resolve(strict=True) if args.input_root else output / "inputs"
        if args.input_root is None:
            input_root.mkdir()
        with (output / "trials.jsonl").open("x", encoding="utf-8") as sink:
            for workload in args.workload:
                for tier in args.tiers:
                    if tier not in fixtures:
                        started = time.monotonic_ns()
                        source = input_root / tier
                        if args.input_root is None:
                            FAMILIES[family][1](tier, source, ROOT)
                        generation_ns = None if args.input_root else time.monotonic_ns() - started
                        fixture = load_fixture(source, family, tier)
                        snapshot = fixture.snapshot()
                        report["inputs"][tier] = {
                            "directory": str(source), "generation_ns": generation_ns, "first_fingerprints": snapshot,
                            "manifest_sha256": fixture.manifest_sha256, "reference": fixture.reference,
                            "rasters": _raster_inputs(fixture, profile), "last_fingerprints": None,
                        }
                        fixtures[tier] = fixture
                    fixture = fixtures[tier]
                    area = {}
                    if workload == "method":
                        dem = family == "raster"
                        area = area_reference(
                            fixture.grid("dem" if dem else "first_product"), profile["nodata_period_pixels"] if dem else None,
                        )
                    for cache in args.cache:
                        group = _group(output, fixture, profile, area, cache, workload, args.pilot, args.timeout, sink)
                        report["groups"].append(group)
                        publish(report_path, report)
                        require(not group["interrupted"] and group["observed_trials"] == group["expected_trials"],
                                "Benchmark worker stopped without a reliable terminal observation")
        report["status"] = "FAIL" if any(
            group["trial_failures"] or group["warm_worker_exit"] not in {None, 0} for group in report["groups"]
        ) else "PASS"
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = "FAIL"
        report["error"] = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
    finally:
        for tier, fixture in fixtures.items():
            try:
                report["inputs"][tier]["last_fingerprints"] = fixture.snapshot()
            except Exception as error:
                report["status"] = "FAIL"
                report["inputs"][tier]["last_fingerprint_error"] = str(error)
        publish(report_path, report)
    print(f"{report['status']} {report_path}", flush=True)
    return 0 if report["status"] == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--tiers", nargs="+", choices=TIERS, default=TIERS)
    parser.add_argument("--cache", nargs="+", choices=CACHES, default=CACHES)
    parser.add_argument("--workload", nargs="+", choices=WORKLOADS, default=WORKLOADS)
    parser.add_argument("--input-root", type=Path, help="Existing directory holding the P13-01 tier directories")
    parser.add_argument("--output", type=Path, help="New directory for raw trials and reports")
    parser.add_argument("--timeout", type=int, default=1800, help="Maximum seconds per method run")
    parser.add_argument("--source-commit", help="Record a supplied checkout SHA when .git metadata is outside the runtime")
    parser.add_argument("--pilot", action="store_true", help="One non-baseline attempt per group, without summary statistics")
    args = parser.parse_args(argv)
    for key in ("tiers", "cache", "workload"):
        values = getattr(args, key)
        if len(values) != len(set(values)):
            parser.error(f"--{key} values must be distinct")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    try:
        return run(args)
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.exit(1, f"Benchmark could not start: {error}\n")


if __name__ == "__main__":
    raise SystemExit(worker() if sys.argv[1:] == ["--_worker"] else main())
