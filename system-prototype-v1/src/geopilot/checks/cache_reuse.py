"""Verify a reused output against the persisted source calculation."""

import json
import os
import sqlite3
from hashlib import sha256
from pathlib import Path
from stat import S_ISREG

from geopilot.artifacts.step_cache import MAX_PAYLOAD_BYTES
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import ExecutionStatus, StepRequest, StepReuse
from geopilot.execution.step_support import resolve_step_path
from geopilot.persistence.run_repository import get_run, get_run_step
from geopilot.persistence.schema import open_database
from geopilot.runs.models import CheckStatus


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_reuse_output(workdir: Path, relative: str) -> bytes:
    """Read a bounded, stable regular output in the run workspace."""
    path = resolve_step_path(workdir, relative, field="reuse.output_sha256")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_PAYLOAD_BYTES:
            raise ValueError("Expected a regular reused output file")
        payload = stream.read(MAX_PAYLOAD_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(payload) != before.st_size or not _file_identity(before) == _file_identity(after) == _file_identity(path.lstat()):
            raise ValueError("Reused output changed while being read")
    return payload


def reuse_output_sha256(workdir: Path, relative: str) -> str:
    return sha256(read_reuse_output(workdir, relative)).hexdigest()


def verify_reuse_source(database_path: str | Path, reuse: StepReuse, request: StepRequest) -> bool:
    """Check that an exact tool version produced the original bytes in a passed run."""
    if reuse.source_run_id == request.run_id or len(request.output_targets) != 1:
        return False
    try:
        with open_database(database_path, read_only=True) as connection:
            connection.execute("BEGIN")
            source = get_run(connection, reuse.source_run_id)
            if source is None or (source.execution_status, source.check_status) != (
                ExecutionStatus.SUCCEEDED, CheckStatus.PASSED,
            ):
                return False
            step = get_run_step(connection, source.id, reuse.source_step_key)
            current = get_run(connection, request.run_id)
        if step is None or step.request is None or step.request.tool != request.tool or step.result is None:
            return False
        if current is None or step.request.inputs.keys() != request.inputs.keys() or (
            json.dumps(step.request.parameters, sort_keys=True) != json.dumps(request.parameters, sort_keys=True)
        ):
            return False
        result = step.result
        if result.status is not ExecutionStatus.SUCCEEDED or result.exit_code != 0 or result.reuse is not None:
            return False
        if len(result.outputs) != 1 or len(step.request.output_targets) != 1:
            return False
        role, output = next(iter(result.outputs.items()))
        target = step.request.output_targets.get(role)
        current_target = request.output_targets.get(role)
        if (
            target is None or target.path != output.path or target.layer is not None
            or current_target is None or current_target.layer is not None
            or output.type != "json" or output.layer is not None
        ):
            return False
        workdir = Path(source.workdir)
        current_dir = Path(current.workdir)
        for directory in (workdir, current_dir):
            if not directory.is_absolute() or directory.resolve(strict=True) != directory or not directory.is_dir():
                return False
        for role, reference in request.inputs.items():
            prior = step.request.inputs[role]
            field = f"inputs.{role}"
            if prior.layer != reference.layer or (
                fingerprint_file(resolve_step_path(workdir, prior.path, field=field)).digest
                != fingerprint_file(resolve_step_path(current_dir, reference.path, field=field)).digest
            ):
                return False
        return reuse_output_sha256(workdir, output.path) == reuse.output_sha256
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return False
