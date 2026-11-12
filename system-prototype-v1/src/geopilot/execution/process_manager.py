"""Capture a local worker and stop its process group when it overruns."""

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile

from geopilot.execution.worker_lease import worker_lease
from geopilot.observability.process import ProcessProbe


_TERMINATION_GRACE = 2


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    cancelled: bool


def _signal_process(process: subprocess.Popen, *, force: bool) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass


def _stop_process(process: subprocess.Popen) -> None:
    _signal_process(process, force=False)
    if os.name == "posix":
        # Hold the leader's PID until after escalation, including when it
        # exits on TERM while a quiet descendant remains in the group.
        deadline = time.monotonic() + _TERMINATION_GRACE
        while time.monotonic() < deadline:
            if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                break
            time.sleep(0.02)
        _signal_process(process, force=True)
        process.wait()
    else:
        try:
            process.wait(timeout=_TERMINATION_GRACE)
        except subprocess.TimeoutExpired:
            _signal_process(process, force=True)
            process.wait()


def capture_process(
    argv: Sequence[str], *, cwd: Path, timeout: float,
    stdin: str | None = None, env: Mapping[str, str] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    probe: ProcessProbe | None = None,
) -> ProcessOutcome:
    with worker_lease(cwd) as lease, TemporaryFile() as input_file, TemporaryFile() as stdout, TemporaryFile() as stderr:
        if stdin is not None:
            input_file.write(stdin.encode("utf-8"))
            input_file.seek(0)
        with subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL if stdin is None else input_file,
            stdout=stdout, stderr=stderr, start_new_session=os.name == "posix", pass_fds=(lease,),
        ) as process:
            timed_out = cancelled = False
            deadline = time.monotonic() + timeout
            try:
                if probe is not None:
                    probe.attach(process.pid)
                while True:
                    if cancel_requested is not None and cancel_requested():
                        cancelled = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    try:
                        process.wait(timeout=remaining if cancel_requested is None else min(0.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                try:
                    if process.returncode is None:
                        _stop_process(process)
                finally:
                    if probe is not None:
                        probe.finish(process.returncode, cancelled=cancelled, timed_out=timed_out)
            stdout.seek(0)
            stderr.seek(0)
            return ProcessOutcome(
                exit_code=process.returncode, stdout=stdout.read().decode("utf-8", errors="replace"),
                stderr=stderr.read().decode("utf-8", errors="replace"), timed_out=timed_out, cancelled=cancelled,
            )
