"""Run one published local script snapshot and keep its independent evidence."""

import errno
import math
import os
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from stat import S_ISDIR, S_ISREG
from typing import Literal, Self, TextIO
from uuid import uuid4

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from geopilot.domain.data_asset import _PositiveInt
from geopilot.domain.errors import ErrorDetail, _RunPath, _StepModel
from geopilot.domain.step_io import ExecutionStatus, _Timestamp
from geopilot.execution.process_manager import ProcessOutcome, capture_process
from geopilot.execution.step_support import resolve_step_path
from geopilot.persistence.workspace import _open_directory, _open_path
from geopilot.scripts.models import ScriptSnapshot, _Identifier
from geopilot.scripts.snapshot import (
    SCRIPT_REQUEST_PATH,
    _read_file,
    _remove_file,
    _validate_run_id,
    _write_readonly,
    load_script_snapshot,
)


SCRIPT_LOG_PATH = "process.log"
SCRIPT_RESULT_PATH = "result.json"


class ScriptOutput(_StepModel):
    path: _RunPath
    size_bytes: StrictInt = Field(ge=0)

    @field_validator("path")
    @classmethod
    def require_output_directory(cls, value: str) -> str:
        if not value.startswith("outputs/"):
            raise ValueError("Script output paths must be inside the outputs directory")
        return value


class ScriptRunResult(_StepModel):
    script_run_id: _Identifier
    script_id: _Identifier
    version: _PositiveInt
    status: Literal[ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED]
    exit_code: StrictInt | None
    timed_out: StrictBool
    started_at: _Timestamp
    finished_at: _Timestamp
    log_path: Literal["process.log"] = SCRIPT_LOG_PATH
    outputs: tuple[ScriptOutput, ...]
    error: ErrorDetail | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.status is ExecutionStatus.SUCCEEDED:
            if self.exit_code != 0 or self.error is not None or self.timed_out:
                raise ValueError("A successful script requires exit code zero and no error or timeout")
        elif self.error is None or self.outputs:
            raise ValueError("A failed script requires an error and an empty output manifest")
        if self.error is not None and self.error.log_path != self.log_path:
            raise ValueError("The result error must refer to the script process log")
        if self.timed_out != (self.error is not None and self.error.code == "PROCESS_TIMEOUT"):
            raise ValueError("The timeout flag must agree with the process error")
        return self


def _error(code: str, field: str, message: str) -> ErrorDetail:
    return ErrorDetail(
        code=code, phase="execute", step_key="script", field=field, message=message,
        suggestion="Review this run's log and prepare a new script snapshot.", log_path=SCRIPT_LOG_PATH,
    )


def _duration(value: float) -> float:
    if type(value) not in (int, float):
        raise ValueError("timeout must be a positive finite number of seconds")
    try:
        seconds = float(value)
    except OverflowError as error:
        raise ValueError("timeout must be a positive finite number of seconds") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("timeout must be a positive finite number of seconds")
    return seconds


def _readable_size(path: str | Path, *, directory: int | None = None) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    try:
        stat = os.fstat(fd)
        if not S_ISREG(stat.st_mode):
            raise ValueError(f"Expected a regular local file: {path}")
        os.read(fd, 1)
        return stat.st_size
    finally:
        os.close(fd)


def _input_problem(workdir: Path, snapshot: ScriptSnapshot) -> ErrorDetail | None:
    for role, reference in snapshot.asset_files.items():
        field = f"asset_files.{role}.path"
        try:
            path = resolve_step_path(workdir, reference.path, field=field)
            _readable_size(path)
        except (OSError, ValueError) as error:
            return _error("SOURCE_UNREADABLE", field, str(error))
    return None


def _raise_io(error: OSError) -> None:
    raise error


def _list_outputs(workdir: Path, directory: int) -> tuple[ScriptOutput, ...]:
    manifest = []
    with _open_directory(workdir / "outputs", directory) as outputs:
        for folder, subdirectories, files, parent in os.fwalk(".", dir_fd=outputs, onerror=_raise_io, follow_symlinks=False):
            for name in subdirectories:
                if not S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                    raise ValueError(f"Expected a local output directory: {PurePosixPath(folder, name)}")
            for name in files:
                path = PurePosixPath("outputs", folder, name).as_posix()
                manifest.append(ScriptOutput(path=path, size_bytes=_readable_size(name, directory=parent)))
    return tuple(sorted(manifest, key=lambda item: item.path))


def _execute_script(
    log: TextIO, directory: int, workdir: Path, snapshot: ScriptSnapshot, timeout: float,
) -> tuple[ProcessOutcome | None, tuple[ScriptOutput, ...], ErrorDetail | None]:
    argv = [sys.executable, "-E", "-B", "-u", "-X", "utf8", snapshot.script_path, "--request", SCRIPT_REQUEST_PATH]
    log.write(f"$ {shlex.join(argv)}\n")
    log.flush()
    problem = _input_problem(workdir, snapshot)
    if problem is not None:
        return None, (), problem
    try:
        outcome = capture_process(argv, cwd=workdir, timeout=timeout)
    except OSError as error:
        return None, (), _error("PROCESS_START_FAILED", "script", str(error))

    log.write(f"stdout:\n{outcome.stdout}\nstderr:\n{outcome.stderr}\nexit_code: {outcome.exit_code}\n")
    if outcome.timed_out:
        return outcome, (), _error("PROCESS_TIMEOUT", "timeout", f"Script exceeded {timeout:g} seconds")
    if outcome.exit_code != 0:
        return outcome, (), _error("PROCESS_FAILED", "script", f"Script exited with code {outcome.exit_code}")
    try:
        outputs = _list_outputs(workdir, directory)
    except (OSError, ValueError) as error:
        return outcome, (), _error("OUTPUT_INVALID", "outputs", str(error))
    return outcome, outputs, None


def _publish_result(directory: int, result: ScriptRunResult) -> None:
    temporary = f".result-{uuid4().hex}.tmp"
    _write_readonly(directory, temporary, (result.model_dump_json(indent=2) + "\n").encode("utf-8"))
    try:
        os.link(temporary, SCRIPT_RESULT_PATH, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    except BaseException as failure:
        _remove_file(directory, temporary, failure)
        raise
    os.unlink(temporary, dir_fd=directory)


def run_script_snapshot(
    workspace_root: str | Path, script_run_id: str, *, timeout: float = 60.0,
) -> ScriptRunResult:
    """Claim and run a published script once in its own working directory."""
    seconds = _duration(timeout)
    root = Path(workspace_root).resolve()
    snapshot = load_script_snapshot(root, script_run_id)
    workdir = root / "scripts" / script_run_id
    with _open_path(root, ("scripts", script_run_id)) as directory:
        try:
            os.stat(SCRIPT_RESULT_PATH, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(errno.EEXIST, "This script run already has a result", workdir / SCRIPT_RESULT_PATH)
        with _open_directory(workdir / "outputs", directory) as outputs:
            if os.listdir(outputs):
                raise FileExistsError(errno.EEXIST, "This script run requires an empty outputs directory", workdir / "outputs")
        log_fd = os.open(
            SCRIPT_LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory,
        )
        started_at = datetime.now(UTC)
        with os.fdopen(log_fd, "w", encoding="utf-8") as log:
            outcome, manifest, error = _execute_script(log, directory, workdir, snapshot, seconds)
            if error is not None:
                log.write(f"{error.code}: {error.message}\n")
            log.flush()
            os.fchmod(log.fileno(), 0o400)
            os.fsync(log.fileno())
        result = ScriptRunResult(
            script_run_id=script_run_id, script_id=snapshot.script_id, version=snapshot.version,
            status=ExecutionStatus.FAILED if error is not None else ExecutionStatus.SUCCEEDED,
            exit_code=outcome.exit_code if outcome is not None else None,
            timed_out=outcome.timed_out if outcome is not None else False,
            started_at=started_at, finished_at=max(started_at, datetime.now(UTC)), outputs=manifest, error=error,
        )
        _publish_result(directory, result)
    return result


def load_script_result(workspace_root: str | Path, script_run_id: str) -> ScriptRunResult:
    """Read the completed result of the exact script run."""
    _validate_run_id(script_run_id)
    root = Path(workspace_root).resolve()
    with _open_path(root, ("scripts", script_run_id)) as directory:
        result = ScriptRunResult.model_validate_json(_read_file(directory, SCRIPT_RESULT_PATH))
    if result.script_run_id != script_run_id:
        raise ValueError("Script result identity differs from its run directory")
    return result
