"""Collect optional run diagnostics and publish them outside the scheduler."""

import json
import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from queue import Full, Queue
from uuid import uuid4

from geopilot.observability.process import RESOURCE_FIELDS, ProcessProbe, metric

SIDECAR_NAME = ".geopilot-telemetry.json"
_LOGGER = logging.getLogger(__name__)
_PENDING: Queue["RunTelemetry"] = Queue(maxsize=16)
_PUBLISHER_LOCK = threading.Lock()
_publisher: threading.Thread | None = None
_ACTIVE: ContextVar["RunTelemetry | None"] = ContextVar("run_telemetry", default=None)


def _resources(processes: list[dict]) -> dict:
    resources = {}
    for name in RESOURCE_FIELDS:
        observed = [item["resources"][name] for item in processes if item["resources"][name]["value"] is not None]
        if not observed:
            reading = metric("unavailable", reason="no_usable_process_metrics" if processes else "no_subprocess")
        else:
            values = [item["value"] for item in observed]
            value = max(values) if name == "peak_rss_bytes" else sum(values)
            if len(observed) != len(processes):
                reading = metric("partial", value, "missing_process_metrics")
            elif any(item["status"] == "partial" for item in observed):
                reason = "observed_cumulative_lower_bound" if name != "peak_rss_bytes" else "limited_proc_samples"
                reading = metric("partial", value, reason)
            else:
                reading = metric("sampled", value)
        resources[name] = {**reading, "observed_processes": len(observed), "processes": len(processes)}
    return resources


class StepTelemetry:
    def __init__(self, step_key: str) -> None:
        self.step_key = step_key
        self.started_ns = time.monotonic_ns()
        self.finished_ns: int | None = None
        self.tool: dict | None = None
        self.environment: dict = {}
        self.status = "INTERRUPTED"
        self.exit_code: int | None = None
        self.processes: list[ProcessProbe] = []

    def set_tool(self, tool_id: str, version: int, runner: str) -> None:
        self.tool = {"id": tool_id, "version": version, "runner": runner}

    def start_process(self, phase: str) -> ProcessProbe:
        external = (
            self.tool is not None and self.tool["runner"] == "qgis"
            and os.environ.get("GEOPILOT_TELEMETRY_QGIS_SCOPE") == "container"
        )
        probe = ProcessProbe(phase, external_container=external)
        self.processes.append(probe)
        return probe

    def finish(self, result: Mapping) -> None:
        self.finished_ns = time.monotonic_ns()
        self.status = result["status"]
        self.exit_code = result["exit_code"]
        self.environment = dict(result["environment"])

    def document(self, run_finished_ns: int) -> dict:
        processes = [probe.outcome for probe in self.processes]
        return {
            "step_key": self.step_key,
            "wall_ns": (self.finished_ns or run_finished_ns) - self.started_ns,
            "status": self.status,
            "exit_code": self.exit_code,
            "tool": self.tool,
            "runtime_versions": {
                "status": "reported" if self.environment else "unavailable",
                "values": self.environment,
            },
            "processes": processes,
            "resources": _resources(processes),
        }


class RunTelemetry:
    def __init__(self, workdir: Path, run_id: str, created_at: datetime | None) -> None:
        self.workdir = workdir
        self.run_id = run_id
        self.started_ns = time.monotonic_ns()
        self.finished_ns = self.started_ns
        self.steps_finished_ns: int | None = None
        self.execution_status = "INTERRUPTED"
        self.check_status: str | None = None
        self.steps: list[StepTelemetry] = []
        self.queue_wall_ns = metric("unavailable", reason="no_queued_run_timestamp")
        if created_at is not None:
            elapsed = datetime.now(UTC) - created_at
            if elapsed.total_seconds() >= 0:
                self.queue_wall_ns = metric("recorded", elapsed // datetime.resolution * 1000)
            else:
                self.queue_wall_ns = metric("unavailable", reason="utc_clock_moved_backwards")

    def steps_finished(self) -> None:
        self.steps_finished_ns = time.monotonic_ns()

    def complete(self, execution_status: str, check_status: str) -> None:
        self.execution_status = execution_status
        self.check_status = check_status

    def document(self) -> dict:
        steps = [step.document(self.finished_ns) for step in self.steps]
        processes = [process for step in steps for process in step["processes"]]
        exit_code = 0 if self.execution_status == "SUCCEEDED" else None
        if steps and steps[-1]["status"] in {"FAILED", "CANCELLED"}:
            exit_code = steps[-1]["exit_code"]
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "execution_status": self.execution_status,
            "check_status": self.check_status,
            "exit_code": exit_code,
            "wall_ns": self.finished_ns - self.started_ns,
            "wall_scope": "execution_through_finalization",
            "queue_wall_ns": self.queue_wall_ns,
            "queue_clock": "utc",
            "execute_steps_wall_ns": None if self.steps_finished_ns is None else self.steps_finished_ns - self.started_ns,
            "finalize_wall_ns": None if self.steps_finished_ns is None else self.finished_ns - self.steps_finished_ns,
            "steps": steps,
            "resources": _resources(processes),
        }


def _publish(run: RunTelemetry) -> None:
    if run.workdir.resolve(strict=True) != run.workdir:
        raise ValueError("Telemetry requires a canonical run directory")
    directory = os.open(run.workdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".geopilot-telemetry-{uuid4().hex}.tmp"
    try:
        file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        try:
            with os.fdopen(file, "w", encoding="utf-8") as stream:
                json.dump(run.document(), stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
            # Link a complete file atomically without replacing any existing file or symlink.
            os.link(temporary, SIDECAR_NAME, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            os.unlink(temporary, dir_fd=directory)
    finally:
        os.close(directory)


def _publisher_loop() -> None:
    while True:
        run = _PENDING.get()
        try:
            _publish(run)
        except (OSError, RuntimeError, ValueError) as error:
            _LOGGER.warning("Telemetry could not publish run %r: %s", run.run_id, error)
        finally:
            _PENDING.task_done()


def _enqueue(run: RunTelemetry) -> None:
    global _publisher
    if not _PUBLISHER_LOCK.acquire(blocking=False):
        return
    try:
        if _publisher is None or not _publisher.is_alive():
            _publisher = threading.Thread(target=_publisher_loop, name="geopilot-telemetry-publish", daemon=True)
            _publisher.start()
        _PENDING.put_nowait(run)
    except (Full, OSError, RuntimeError):
        # This diagnostic queue must never delay or fail a successful GIS run.
        return
    finally:
        _PUBLISHER_LOCK.release()


@contextmanager
def record_run(workdir: Path, run_id: str, *, created_at: datetime | None = None) -> Iterator[RunTelemetry | None]:
    if os.environ.get("GEOPILOT_TELEMETRY") != "1":
        yield None
        return
    run = RunTelemetry(workdir, run_id, created_at)
    token = _ACTIVE.set(run)
    try:
        yield run
    finally:
        run.finished_ns = time.monotonic_ns()
        _ACTIVE.reset(token)
        _enqueue(run)


def start_step(workdir: Path | str, identifiers: Mapping) -> StepTelemetry | None:
    run = _ACTIVE.get()
    if run is None or run.workdir != Path(workdir) or run.run_id != identifiers.get("run_id"):
        return None
    step_key = identifiers.get("step_key")
    if not isinstance(step_key, str):
        return None
    step = StepTelemetry(step_key)
    run.steps.append(step)
    return step
