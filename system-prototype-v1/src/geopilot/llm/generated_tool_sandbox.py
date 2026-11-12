"""Review a generated numeric GIS function against a fixed local GeoTIFF."""

import argparse
import hashlib
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from geopilot.execution.process_manager import capture_process
from geopilot.llm._generated_tool_worker import (
    ARGUMENTS, CONTAINER_ENV, CONTAINER_UID, FIELDS, FUNCTION_NAME, MAX_AST_NODES,
    MAX_OPEN_FILES, MAX_OUTPUT_BYTES, MAX_SOURCE_BYTES, MAX_VALUES, SourceRejected,
    runtime_matches, valid_result, validate_source,
)


_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = _ROOT / "tests/method_dev/fixtures/dem-terrain"
_MANIFEST_SHA256 = "e788319f86cae92a4f9a1e215e0cf6a8476593ca7b2f85174bd38500ebc98a93"
_DEM_SHA256 = "e208d5868728a26255248340480fa325b053a079413bdf6c4cc5a0d73a6183e8"
_REFERENCE = ((100, 47, 4700), (130, 27, 2700), (161, 0, 0))
_VALID_COUNT = 47
_DOCKER = Path("/usr/bin/docker")
_SOCKET = Path("/var/run/docker.sock")
_IMAGE = "sha256:c618d0d9d91d0cf473d61e5fc284aa335e87caabd51381b73cd61b2d0f93cb87"
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = "org.geopilot.generated-tool-sandbox"
_MEMORY = 64 * 1024 * 1024
_NANO_CPUS = 500_000_000
_PIDS = 16
_CPU_SECONDS = 4
_WALL_SECONDS = 4
_CONTROL_SECONDS = 5
_MAX_FIXTURE_BYTES = 64 * 1024
_LIMITS = {
    "memory_bytes": _MEMORY, "memory_and_swap_bytes": _MEMORY, "cpus": _NANO_CPUS / 1_000_000_000,
    "pids": _PIDS, "open_files": MAX_OPEN_FILES, "cpu_seconds": _CPU_SECONDS,
    "wall_seconds": _WALL_SECONDS, "output_bytes": MAX_OUTPUT_BYTES,
    "source_bytes": MAX_SOURCE_BYTES, "ast_nodes": MAX_AST_NODES, "input_values": MAX_VALUES,
}
_ENTRYPOINT = "/usr/bin/env"
_COMMAND = ("-i", *(f"{name}={value}" for name, value in CONTAINER_ENV.items()),
            "/usr/bin/python3", "-I", "-S", "-B", "-u", "/sandbox/worker.py")


class _SandboxError(ValueError):
    def __init__(self, code: str, phase: str, message: str):
        super().__init__(message)
        self.code, self.phase = code, phase


def describe_candidate() -> dict:
    """Return a description suitable for a candidate author without sample data."""
    return {
        "schema_version": 1,
        "function": FUNCTION_NAME,
        "inputs": {ARGUMENTS[0]: {"type": "list[number]", "max_items": MAX_VALUES, "nodata": "omitted"},
                   ARGUMENTS[1]: {"type": "number", "comparison": "greater_than_or_equal"}},
        "result": {name: "nonnegative integer" for name in sorted(FIELDS)},
        "constraints": {
            "source_bytes": MAX_SOURCE_BYTES, "ast_nodes": MAX_AST_NODES,
            "syntax": "local numeric assignment, arithmetic, comparison, branching, loops, list and result literals",
            "calls": False, "imports": False, "attributes": False, "indexing": False,
        },
    }


def generate_reference_candidate() -> str:
    """Generate the deterministic local baseline for the advertised contract."""
    return f"""def {FUNCTION_NAME}({', '.join(ARGUMENTS)}):
    valid = 0
    selected = 0
    for value in values:
        valid += 1
        if value >= threshold:
            selected += 1
    return {{"valid_count": valid, "selected_count": selected}}
"""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise OSError
        content = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(content) != before.st_size or any(getattr(before, field) != getattr(after, field) for field in fields):
            raise OSError
        return content


def _reference_sample():
    import rasterio

    from geopilot.data.fingerprint import capture_file_state
    from geopilot.data.raster_reader import read_raster_metadata
    from geopilot.gis.measurement.area import validate_area

    try:
        manifest = _FIXTURE / "manifest.json"
        source = _FIXTURE / "inputs/dem.tif"
        if manifest.is_symlink() or source.is_symlink() or source.parent.is_symlink() or _FIXTURE.is_symlink():
            raise ValueError
        manifest_bytes = _read_regular(manifest, _MAX_FIXTURE_BYTES)
        if _sha256(manifest_bytes) != _MANIFEST_SHA256:
            raise ValueError
        item = json.loads(manifest_bytes)["inputs"]["dem"]
        if item != {"path": "inputs/dem.tif", "sha256": _DEM_SHA256}:
            raise ValueError
        source_state = capture_file_state(source)
        if len(source_state) != 1 or _sha256(_read_regular(source, _MAX_FIXTURE_BYTES)) != _DEM_SHA256:
            raise ValueError
        with rasterio.Env(GDAL_PAM_ENABLED=False, GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", PROJ_NETWORK="OFF"):
            metadata = read_raster_metadata(source)
            if (
                (metadata.width, metadata.height, metadata.band_count, metadata.crs) != (7, 7, 1, "EPSG:6933")
                or metadata.bounds != (500000, 4400000, 500070, 4400070)
                or metadata.transform != (10, 0, 500000, 0, -10, 4400070)
                or metadata.band_units != ("m",) or metadata.nodata != (-9999,) or metadata.band_dtypes != ("float32",)
            ):
                raise ValueError
            validate_area(metadata)
            with rasterio.open(source, mode="r", driver="GTiff") as dataset:
                if dataset.scales != (1.0,) or dataset.offsets != (0.0,):
                    raise ValueError
                valid = dataset.read(1, masked=True).compressed().tolist()
        if (
            len(valid) != _VALID_COUNT or any(not math.isfinite(value) for value in valid)
            or capture_file_state(source) != source_state or _sha256(_read_regular(source, _MAX_FIXTURE_BYTES)) != _DEM_SHA256
            or _read_regular(manifest, _MAX_FIXTURE_BYTES) != manifest_bytes
        ):
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as error:
        raise _SandboxError("REFERENCE_INVALID", "sample", "The fixed GeoTIFF or its declared grid differs") from error
    a, b, _, d, e, _ = metadata.transform
    sample = {
        "case_id": "equal-area-dem-threshold-v1", "package_manifest_sha256": _MANIFEST_SHA256,
        "geotiff_sha256": _DEM_SHA256, "crs": metadata.crs, "bands": metadata.band_count,
        "grid": [metadata.width, metadata.height], "nodata_count": metadata.width * metadata.height - len(valid),
        "cell_area_m2": abs(a * e - b * d), "rasterio": rasterio.__version__, "gdal": rasterio.__gdal_version__,
    }
    return sample, valid


@dataclass(frozen=True, slots=True)
class _DockerClient:
    workspace: Path

    def call(self, *args: str, timeout=_CONTROL_SECONDS, cancel_requested=None):
        command = [str(_DOCKER), "--host", f"unix://{_SOCKET}", "--config", str(self.workspace), *args]
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.workspace), "LC_ALL": "C"}
        return capture_process(command, cwd=self.workspace, timeout=timeout, env=env, cancel_requested=cancel_requested)

    def check(self) -> str:
        if not _DOCKER.is_file() or not os.access(_DOCKER, os.X_OK) or not _SOCKET.is_socket():
            raise _SandboxError("SANDBOX_UNAVAILABLE", "prepare", "The local Docker runtime is unavailable")
        info = self.call("info", "--format", "{{.ServerVersion}} {{.MemoryLimit}} {{.SwapLimit}}")
        fields = info.stdout.strip().split()
        image = self.call("image", "inspect", _IMAGE)
        if (
            info.exit_code or info.timed_out or len(fields) != 3 or fields[1:] != ["true", "true"]
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]*", fields[0])
            or image.exit_code or image.timed_out
        ):
            raise _SandboxError("SANDBOX_UNAVAILABLE", "prepare", "The pinned image or required local limits are unavailable")
        details = _single_object(image.stdout, phase="prepare")
        if details.get("Id") != _IMAGE or details.get("Os") != "linux" or details.get("Config", {}).get("Volumes"):
            raise _SandboxError("SANDBOX_UNAVAILABLE", "prepare", "The local image does not match the fixed sandbox")
        return fields[0]

    def inspect(self, reference: str):
        result = self.call("container", "inspect", reference)
        if not result.exit_code and not result.timed_out:
            return _single_object(result.stdout, phase="inspect")
        if not result.timed_out and (
            f"No such object: {reference}" in result.stderr or f"No such container: {reference}" in result.stderr
        ):
            return None
        raise _SandboxError("SANDBOX_UNAVAILABLE", "inspect", "The local container state could not be verified")


def _single_object(stdout, *, phase):
    try:
        items = json.loads(stdout)
        if type(items) is list and len(items) == 1 and type(items[0]) is dict:
            return items[0]
    except (ValueError, RecursionError):
        pass
    raise _SandboxError("SANDBOX_UNAVAILABLE", phase, "The local runtime returned an invalid inspection result")


def _create_arguments(name, token, stage):
    return (
        "create", "--pull=never", f"--name={name}", f"--label={_LABEL}={token}",
        "--network=none", "--ipc=none", "--cgroupns=private", "--cap-drop=ALL", "--security-opt=no-new-privileges=true",
        "--read-only", f"--user={CONTAINER_UID}:{CONTAINER_UID}", "--workdir=/sandbox", "--log-driver=none",
        "--restart=no",
        f"--memory={_MEMORY}", f"--memory-swap={_MEMORY}", f"--cpus={_LIMITS['cpus']}",
        f"--pids-limit={_PIDS}", f"--ulimit=nofile={MAX_OPEN_FILES}:{MAX_OPEN_FILES}",
        f"--ulimit=cpu={_CPU_SECONDS}:{_CPU_SECONDS}", "--ulimit=core=0:0",
        f"--mount=type=bind,src={stage},dst=/sandbox,readonly,bind-propagation=rprivate",
        f"--entrypoint={_ENTRYPOINT}", _IMAGE, *_COMMAND,
    )


def _container_policy(details, stage, token):
    if details is None:
        raise _SandboxError("SANDBOX_POLICY_MISMATCH", "prepare", "The created container is missing")
    config, host = details["Config"], details["HostConfig"]
    mounts = details["Mounts"]
    security = set(host["SecurityOpt"] or ())
    limits = {item["Name"]: (item["Soft"], item["Hard"]) for item in host["Ulimits"]}
    expected = {
        "NetworkMode": "none", "IpcMode": "none", "CgroupnsMode": "private", "ReadonlyRootfs": True, "Privileged": False,
        "Memory": _MEMORY, "MemorySwap": _MEMORY, "NanoCpus": _NANO_CPUS, "PidsLimit": _PIDS,
    }
    if not (
        details["Image"] == _IMAGE and config["Labels"].get(_LABEL) == token
        and config["User"] == f"{CONTAINER_UID}:{CONTAINER_UID}" and config["WorkingDir"] == "/sandbox"
        and config["Entrypoint"] == [_ENTRYPOINT] and config["Cmd"] == list(_COMMAND)
        and all(host.get(key) == value for key, value in expected.items())
        and host["CapDrop"] == ["ALL"] and not host["CapAdd"] and not host["Devices"] and not host["DeviceRequests"]
        and host["PidMode"] != "host" and host["UsernsMode"] != "host" and host["LogConfig"]["Type"] == "none"
        and host["RestartPolicy"]["Name"] == "no"
        and security in ({"no-new-privileges"}, {"no-new-privileges=true"}, {"no-new-privileges:true"})
        and all(limits.get(name) == value for name, value in {
            "nofile": (MAX_OPEN_FILES, MAX_OPEN_FILES), "cpu": (_CPU_SECONDS, _CPU_SECONDS), "core": (0, 0),
        }.items())
        and len(mounts) == 1 and mounts[0]["Type"] == "bind" and mounts[0]["Source"] == str(stage)
        and mounts[0]["Destination"] == "/sandbox" and mounts[0]["RW"] is False
    ):
        raise _SandboxError("SANDBOX_POLICY_MISMATCH", "prepare", "The actual container settings differ from policy")
    return {
        "image": details["Image"], "network": host["NetworkMode"], "ipc": host["IpcMode"],
        "cgroup_namespace": host["CgroupnsMode"],
        "read_only_root": host["ReadonlyRootfs"], "read_only_stage": not mounts[0]["RW"],
        "capabilities_dropped": host["CapDrop"], "no_new_privileges": True, "privileged": host["Privileged"],
        "mount_count": len(mounts), "memory_bytes": host["Memory"], "memory_and_swap_bytes": host["MemorySwap"],
        "cpu_nanocpus": host["NanoCpus"], "pids": host["PidsLimit"], "ulimits": limits,
    }


def _clean_container(client, reference, token, creation_finished, cleanup):
    cleanup["attempted"] = True
    try:
        details = client.inspect(reference)
        if details is None:
            cleanup["verified"] = creation_finished
            return
        identity = details.get("Id", "")
        if not _CONTAINER_ID.fullmatch(identity) or details["Config"]["Labels"].get(_LABEL) != token:
            return
        for _ in range(2):
            client.call("container", "rm", "--force", "--volumes", identity)
            if client.inspect(identity) is None:
                cleanup["verified"] = True
                return
        cleanup["verified"] = False
    except (OSError, _SandboxError, KeyError, TypeError, AttributeError):
        cleanup["verified"] = False


def _unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate output key")
    return value


def _decode_result(stdout):
    try:
        if len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError
        payload = json.loads(stdout, object_pairs_hook=_unique_object)
        if type(payload) is not dict or payload.keys() != {"runtime", "results"}:
            raise ValueError
        runtime, results = payload["runtime"], payload["results"]
        if not runtime_matches(runtime) or type(results) is not list or len(results) != len(_REFERENCE):
            raise ValueError
        if not all(valid_result(result, _VALID_COUNT) for result in results):
            raise ValueError
        return runtime, results
    except (ValueError, TypeError, RecursionError) as error:
        raise _SandboxError("CANDIDATE_OUTPUT_INVALID", "verify", "Sandbox returned invalid or oversized results") from error


def _run_container(workspace, stage, report, cancel_requested):
    client = _DockerClient(workspace)
    execution, cleanup = report["execution"], report["cleanup"]
    execution["engine"] = client.check()
    token = uuid4().hex
    name = f"geopilot-generated-tool-{token}"
    execution["container_name"] = name
    identity = None
    creation_finished = False
    try:
        created = client.call(*_create_arguments(name, token, stage))
        creation_finished = not (created.timed_out or created.cancelled)
        possible_id = created.stdout.strip()
        if not created.exit_code and not created.timed_out and _CONTAINER_ID.fullmatch(possible_id):
            identity = possible_id
        if identity is None:
            raise _SandboxError("SANDBOX_UNAVAILABLE", "prepare", "The isolated container could not be created")
        execution["container_id"] = identity
        execution["policy"] = _container_policy(client.inspect(identity), stage, token)
        if cancel_requested is not None and cancel_requested():
            raise _SandboxError("SANDBOX_CANCELLED", "execute", "Review was cancelled before container start")
        started = time.monotonic()
        result = client.call("start", "--attach", identity, timeout=_WALL_SECONDS, cancel_requested=cancel_requested)
        execution.update({
            "client_exit_code": result.exit_code, "timed_out": result.timed_out, "cancelled": result.cancelled,
            "elapsed_seconds": round(time.monotonic() - started, 6),
        })
        current = client.inspect(identity)
        if current is None:
            raise _SandboxError("SANDBOX_UNAVAILABLE", "execute", "The local container disappeared during review")
        state = current["State"]
        execution.update({
            "container_exit_code": None if state["Running"] else state["ExitCode"],
            "oom_killed": state["OOMKilled"], "running_before_cleanup": state["Running"],
        })
        if result.timed_out:
            raise _SandboxError("SANDBOX_TIMEOUT", "execute", "The isolated function exceeded its wall time")
        if result.cancelled:
            raise _SandboxError("SANDBOX_CANCELLED", "execute", "Review was cancelled")
        if state["OOMKilled"] or state["ExitCode"] == 5:
            raise _SandboxError("SANDBOX_MEMORY_LIMIT", "execute", "The isolated function exhausted its memory allowance")
        if state["ExitCode"] == 2:
            raise _SandboxError("CANDIDATE_OUTPUT_INVALID", "verify", "The function returned values outside its result contract")
        if state["ExitCode"] == 4:
            raise _SandboxError("SANDBOX_POLICY_MISMATCH", "execute", "The worker runtime did not match the sandbox contract")
        if result.exit_code or state["ExitCode"] or state["Running"]:
            raise _SandboxError("SANDBOX_EXECUTION_FAILED", "execute", "The isolated function did not complete successfully")
        return _decode_result(result.stdout)
    finally:
        _clean_container(client, identity or name, token, creation_finished, cleanup)


def _report():
    return {
        "schema_version": 1, "status": "FAILED", "candidate_sha256": None, "worker_sha256": None,
        "limits": dict(_LIMITS), "sample": None, "reference": [], "execution": {"image": _IMAGE},
        "cleanup": {"attempted": False, "verified": None}, "error": None,
    }


def _record_error(report, code, phase, message, *, line=None):
    report["error"] = {"code": code, "phase": phase, "message": message, "line": line}
    return report


def review_candidate(source: str | bytes, *, cancel_requested: Callable[[], bool] | None = None) -> dict:
    """Run a candidate under the fixed local policy and produce review evidence."""
    report = _report()
    try:
        if type(source) is str and len(source) <= MAX_SOURCE_BYTES:
            source = source.encode("utf-8")
        if type(source) is not bytes or len(source) > MAX_SOURCE_BYTES:
            raise SourceRejected("Supply a bounded UTF-8 Python function")
        report["candidate_sha256"] = _sha256(source)
        validate_source(source)
    except (SourceRejected, UnicodeError) as error:
        report["status"] = "REJECTED"
        return _record_error(
            report, "SOURCE_REJECTED", "static", str(error) if isinstance(error, SourceRejected) else "Expected UTF-8 source",
            line=getattr(error, "line", None),
        )

    try:
        if cancel_requested is not None and cancel_requested():
            raise _SandboxError("SANDBOX_CANCELLED", "prepare", "Review was cancelled before creating the sandbox")
        sample, values = _reference_sample()
        report["sample"] = sample
        worker = _read_regular(Path(__file__).with_name("_generated_tool_worker.py"), _MAX_FIXTURE_BYTES)
        report["worker_sha256"] = _sha256(worker)
        with TemporaryDirectory(prefix="geopilot-generated-tool-", dir="/tmp") as directory:
            workspace = Path(directory)
            stage = workspace / "stage"
            stage.mkdir(mode=0o755)
            files = {
                "candidate.py": source, "worker.py": worker,
                "sample.json": json.dumps({
                    "values": values, "thresholds": [threshold for threshold, _, _ in _REFERENCE],
                }, allow_nan=False).encode("utf-8"),
            }
            for name, payload in files.items():
                path = stage / name
                with path.open("xb") as stream:
                    stream.write(payload)
                path.chmod(0o444)
            runtime, results = _run_container(workspace, stage, report, cancel_requested)
            report["execution"]["worker"] = runtime
        for (threshold, expected_count, expected_area), result in zip(_REFERENCE, results, strict=True):
            actual = {**result, "selected_area_m2": result["selected_count"] * sample["cell_area_m2"]}
            expected = {"valid_count": _VALID_COUNT, "selected_count": expected_count, "selected_area_m2": expected_area}
            report["reference"].append({
                "threshold": threshold, "expected": expected, "actual": actual, "matched": actual == expected,
            })
        if not all(case["matched"] for case in report["reference"]):
            raise _SandboxError("REFERENCE_MISMATCH", "verify", "Candidate counts differ from the independent reference")
        if not report["cleanup"]["verified"]:
            raise _SandboxError("SANDBOX_CLEANUP_FAILED", "cleanup", "Container removal could not be confirmed")
        report["status"] = "REVIEW_REQUIRED"
    except _SandboxError as error:
        _record_error(report, error.code, error.phase, str(error))
    except KeyboardInterrupt:
        _record_error(report, "SANDBOX_CANCELLED", "execute", "Review was interrupted")
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, ImportError, AttributeError):
        _record_error(report, "SANDBOX_UNAVAILABLE", "prepare", "The isolated review environment could not be verified")
    if report["cleanup"]["attempted"] and report["cleanup"]["verified"] is not True:
        if report["error"]["code"] != "SANDBOX_CLEANUP_FAILED":
            report["cleanup"]["preceding_error"] = report["error"]["code"]
        _record_error(report, "SANDBOX_CLEANUP_FAILED", "cleanup", "Container removal could not be confirmed")
    return report


def _read_candidate(path):
    if path.suffix != ".py":
        raise OSError
    return _read_regular(path, MAX_SOURCE_BYTES)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument("--candidate", type=Path, help="regular UTF-8 Python source file to review")
    command.add_argument("--describe", action="store_true", help="emit the fixed source contract")
    command.add_argument("--generate", action="store_true", help="emit a deterministic local candidate")
    args = parser.parse_args()
    if args.generate:
        print(generate_reference_candidate(), end="")
        return 0
    if args.describe:
        report, status = describe_candidate(), 0
    else:
        try:
            source = _read_candidate(args.candidate)
        except OSError:
            report, status = _record_error(
                _report(), "INVALID_CANDIDATE_FILE", "read", "Expected a small regular local .py file",
            ), 2
        else:
            report = review_candidate(source)
            status = 0 if report["status"] == "REVIEW_REQUIRED" else 1
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
