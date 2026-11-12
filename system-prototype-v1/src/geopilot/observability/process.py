"""Bounded Linux process-session sampling off the execution thread."""

import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

SAMPLE_INTERVAL = 0.05
_SAMPLE_INTERVAL_NS = round(SAMPLE_INTERVAL * 10**9)
_MAX_PROCESSES = 64
_MAX_THREADS = 256
_PROC = Path("/proc")
_CONTAINER_CLIENTS = {"docker", "podman", "nerdctl", "apptainer", "singularity"}
RESOURCE_FIELDS = ("cpu_ns", "peak_rss_bytes", "disk_read_bytes", "disk_write_bytes")


def metric(status: str, value: int | None = None, reason: str | None = None) -> dict:
    return {"status": status, "value": value, "reason": reason}


@dataclass(frozen=True)
class _ProcessStat:
    command: str
    state: str
    parent: int
    session: int
    start_ticks: int
    cpu_ticks: int
    rss_pages: int


def _read_stat(path: Path) -> _ProcessStat:
    raw = (path / "stat").read_text(encoding="utf-8", errors="replace")
    _, _, rest = raw.rpartition(")")
    fields = rest.split()
    return _ProcessStat(
        raw[raw.index("(") + 1:raw.rindex(")")], fields[0], int(fields[1]), int(fields[3]), int(fields[19]),
        sum(int(fields[index]) for index in (11, 12, 13, 14)), max(0, int(fields[21])),
    )


def _read_io(path: Path) -> tuple[int | None, int | None]:
    try:
        raw = (path / "io").read_text(encoding="ascii")
        fields = dict(line.split(": ", 1) for line in raw.splitlines())
        return int(fields["read_bytes"]), int(fields["write_bytes"])
    except (OSError, KeyError, ValueError):
        return None, None


def _children(path: Path, thread_budget: int) -> tuple[set[int], int, bool]:
    """Discover live descendants, including those forked by non-leader threads."""
    task = path / "task"
    children = set()
    complete = True
    with os.scandir(task) as entries:
        threads = tuple(islice(entries, thread_budget + 1))
    for entry in threads[:thread_budget]:
        try:
            with open(task / entry.name / "children", encoding="ascii") as stream:
                raw = stream.read(8192)
            ids = raw.split()
            children.update(int(value) for value in ids[:_MAX_PROCESSES])
            complete &= len(raw) < 8192 and len(ids) <= _MAX_PROCESSES
            if len(children) > _MAX_PROCESSES:
                return set(sorted(children)[:_MAX_PROCESSES]), min(len(threads), thread_budget), False
        except (OSError, ValueError):
            complete = False
    return children, min(len(threads), thread_budget), complete and len(threads) <= thread_budget


@dataclass(frozen=True)
class _Tree:
    totals: tuple[int | None, ...]
    process_count: int
    complete: bool
    container_client: bool


def _sample_tree(leader: int, root: Path, ticks_per_second: int, page_size: int) -> _Tree:
    pending = deque([(leader, os.getpid())])
    visited = set()
    processes = cpu_ticks = rss_pages = 0
    reads = writes = None
    budget = _MAX_THREADS
    complete = True
    while pending and len(visited) < _MAX_PROCESSES:
        pid, parent = pending.popleft()
        if pid in visited:
            continue
        visited.add(pid)
        path = root if pid == leader else _PROC / str(pid)
        try:
            info = _read_stat(path)
            if info.session != leader or info.parent != parent or info.state in {"Z", "X", "x"}:
                complete = False
                continue
            if info.command in _CONTAINER_CLIENTS:
                return _Tree((None,) * 4, processes, False, True)
            read, written = _read_io(path)
            children, threads, enumerated = _children(path, budget)
            budget -= threads
            # Reject a PID reused while reading its counters or children.
            if _read_stat(path).start_ticks != info.start_ticks:
                complete = False
                continue
        except (OSError, IndexError, ValueError):
            complete = False
            continue
        pending.extend((child, pid) for child in sorted(children))
        complete &= enumerated and read is not None and written is not None
        processes += 1
        cpu_ticks += info.cpu_ticks
        rss_pages += info.rss_pages
        if read is not None:
            reads = (reads or 0) + read
        if written is not None:
            writes = (writes or 0) + written
    totals = (cpu_ticks * 10**9 // ticks_per_second, rss_pages * page_size, reads, writes)
    return _Tree(totals, processes, complete and not pending, False)


@dataclass(frozen=True)
class _Observations:
    samples: int = 0
    first_ns: int | None = None
    last_ns: int | None = None
    peak_processes: int = 0
    complete: bool = True
    maxima: tuple[int | None, ...] = (None,) * 4

    def add(self, tree: _Tree, elapsed_ns: int) -> "_Observations":
        maxima = tuple(
            current if old is None else old if current is None else max(old, current)
            for old, current in zip(self.maxima, tree.totals, strict=True)
        )
        return _Observations(
            self.samples + 1, elapsed_ns if self.first_ns is None else self.first_ns, elapsed_ns,
            max(self.peak_processes, tree.process_count), self.complete and tree.complete, maxima,
        )


class ProcessProbe:
    """An optional per-command handle. The runner neither reads /proc nor joins the sampler."""

    def __init__(self, phase: str, *, external_container: bool = False) -> None:
        self.phase = phase
        self._started = time.monotonic_ns()
        self._stopped = threading.Event()
        self._observations = _Observations()
        self._reason = "external_container" if external_container else None
        self._outcome: dict | None = None

    def attach(self, pid: int) -> None:
        if self._reason is not None:
            return
        if sys.platform != "linux":
            self._reason = "linux_proc_required"
            return
        try:
            fd = _open_process_directory(pid)
        except OSError:
            self._reason = "proc_directory_unavailable"
            return
        try:
            thread = threading.Thread(target=self._sample, args=(pid, fd), name="geopilot-process-sample", daemon=True)
            thread.start()
        except (OSError, RuntimeError):
            os.close(fd)
            self._reason = "sampler_start_failed"

    def _sample(self, pid: int, fd: int) -> None:
        try:
            ticks = os.sysconf("SC_CLK_TCK")
            page_size = os.sysconf("SC_PAGE_SIZE")
            # This magic link keeps referring to the opened proc directory.
            # After exit its counters disappear even if the numeric PID is reused.
            root = _PROC / "self" / "fd" / str(fd)
            while not self._stopped.is_set():
                tree = _sample_tree(pid, root, ticks, page_size)
                if tree.container_client:
                    self._reason = "external_container"
                    return
                if self._stopped.is_set():
                    break
                if tree.process_count:
                    self._observations = self._observations.add(tree, time.monotonic_ns() - self._started)
                self._stopped.wait(SAMPLE_INTERVAL)
        except (OSError, ValueError, RuntimeError):
            self._reason = "proc_sampling_failed"
        finally:
            os.close(fd)

    def finish(self, exit_code: int | None, *, cancelled: bool = False, timed_out: bool = False) -> None:
        if self._outcome is not None:
            return
        self._stopped.set()
        observations = self._observations
        reason = self._reason
        elapsed = time.monotonic_ns() - self._started
        sampled_lifetime = (
            observations.samples >= 2 and observations.complete
            and observations.first_ns is not None and observations.last_ns is not None
            and observations.first_ns <= 2 * _SAMPLE_INTERVAL_NS
            and elapsed - observations.last_ns <= 2 * _SAMPLE_INTERVAL_NS
        )
        resources = {}
        for name, value in zip(RESOURCE_FIELDS, observations.maxima, strict=True):
            if reason == "external_container" or value is None:
                resources[name] = metric("unavailable", reason=reason or (
                    "process_not_started" if exit_code is None else "no_usable_proc_sample"
                ))
            elif name != "peak_rss_bytes":
                resources[name] = metric("partial", value, "observed_cumulative_lower_bound")
            elif not sampled_lifetime or reason is not None:
                resources[name] = metric("partial", value, "limited_proc_samples")
            else:
                resources[name] = metric("sampled", value)
        self._outcome = {
            "phase": self.phase, "wall_ns": elapsed,
            "exit_code": exit_code, "cancelled": cancelled, "timed_out": timed_out,
            "resource_scope": "external_container" if reason == "external_container" else "local_process_session",
            "sample_interval_ns": _SAMPLE_INTERVAL_NS, "sample_count": observations.samples,
            "first_sample_ns": observations.first_ns, "last_sample_ns": observations.last_ns,
            "peak_sampled_processes": observations.peak_processes, "resources": resources,
        }

    @property
    def outcome(self) -> dict:
        if self._outcome is None:
            self.finish(None)
        return self._outcome


def _open_process_directory(pid: int) -> int:
    # Popen still owns the unreaped child here, so the directory cannot
    # already belong to a subsequent holder of its PID.
    return os.open(_PROC / str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
