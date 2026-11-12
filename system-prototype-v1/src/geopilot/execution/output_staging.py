"""Own and recover the temporary outputs of a Python invocation.

Tool subprocesses use the staging path helpers without loading the parent adapter.
"""

import json
import logging
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from secrets import token_hex
from shutil import rmtree
from stat import S_ISDIR, S_ISREG
from tempfile import TemporaryDirectory

OUTPUT_STAGING_ENV = "GEOPILOT_INTERNAL_OUTPUT_STAGING"
_STAGE_NAME = re.compile(r"\.python-output-[0-9a-f]{32}")
_MAX_RECEIPT_BYTES = 64 * 1024
_LOGGER = logging.getLogger(__name__)


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _output_targets(workdir: Path, request: Mapping) -> set[Path]:
    from geopilot.execution.step_support import resolve_step_path

    return {
        resolve_step_path(workdir, ref["path"], field="output_targets")
        for ref in request["output_targets"].values()
    }


def _remove_owned_directory(parent_fd: int, name: str, identity: tuple[int, int]) -> bool:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        _LOGGER.exception("Could not inspect output staging directory %s", name)
        return False
    try:
        if not S_ISDIR(metadata.st_mode) or _identity(metadata) != identity:
            _LOGGER.warning("Output staging directory identity changed: %s", name)
            return False
        rmtree(name, dir_fd=parent_fd)
    except OSError:
        _LOGGER.exception("Could not remove owned output staging directory %s", name)
        return False
    return True


def _remove_receipt(receipt_fd: int, name: str, identity: tuple[int, int]) -> None:
    try:
        metadata = os.stat(name, dir_fd=receipt_fd, follow_symlinks=False)
        if S_ISREG(metadata.st_mode) and _identity(metadata) == identity:
            os.unlink(name, dir_fd=receipt_fd)
        else:
            _LOGGER.warning("Output staging receipt identity changed")
    except FileNotFoundError:
        pass
    except OSError:
        _LOGGER.exception("Could not remove output staging receipt")


@contextmanager
def owned_output_staging(
    workdir: Path, request: Mapping, env: Mapping[str, str],
) -> Iterator[Mapping[str, str]]:
    """Allocate and record the staging directories before launching the leased child."""
    from geopilot.execution.step_support import output_staging_receipt_path
    from geopilot.persistence.workspace import _open_path

    targets = _output_targets(workdir, request)
    parents = {target.parent for target in targets}
    receipt_path = output_staging_receipt_path(workdir, request)
    roots = {}
    directories = []
    receipt = []
    receipt_identity = None
    with ExitStack() as handles:
        receipt_parent = handles.enter_context(_open_path(workdir, receipt_path.parent.relative_to(workdir).parts))
        try:
            for parent in sorted(parents):
                parent_fd = handles.enter_context(_open_path(workdir, parent.relative_to(workdir).parts))
                name = ".python-output-" + token_hex(16)
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                identity = _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
                directories.append((parent_fd, name, identity))
                if identity[0] != os.fstat(parent_fd).st_dev:
                    raise OSError(f"Output staging must use the output filesystem: {parent}")
                directory = parent / name
                roots[parent] = str(directory)
                receipt.append({"path": directory.relative_to(workdir).as_posix(), "device": identity[0],
                                "inode": identity[1]})
            fd = os.open(receipt_path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=receipt_parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                receipt_identity = _identity(os.fstat(stream.fileno()))
                json.dump(receipt, stream)
                stream.flush()
                os.fsync(stream.fileno())
            locations = {str(target): roots[target.parent] for target in sorted(targets)}
            yield {**env, OUTPUT_STAGING_ENV: json.dumps(locations)}
        finally:
            removed = [_remove_owned_directory(*directory) for directory in reversed(directories)]
            if receipt_identity is not None and all(removed):
                _remove_receipt(receipt_parent, receipt_path.name, receipt_identity)


def child_output_staging_parent(target: Path) -> Path:
    """Use managed staging only for the exact declared destination."""
    configured = os.environ.get(OUTPUT_STAGING_ENV)
    if configured is None:
        return target.parent
    try:
        location = json.loads(configured).get(str(target.absolute()))
        return target.parent if location is None else Path(location)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Invalid Python output staging context") from error


def temporary_output_directory(
    target: Path, *, prefix: str, ignore_cleanup_errors: bool = False,
) -> TemporaryDirectory:
    return TemporaryDirectory(
        prefix=prefix, dir=child_output_staging_parent(target), ignore_cleanup_errors=ignore_cleanup_errors,
    )


def recover_output_staging(workdir: Path, request: Mapping) -> None:
    """Reclaim a recorded invocation while the caller holds the run's worker lease."""
    from geopilot.execution.step_support import output_staging_receipt_path, resolve_step_path
    from geopilot.persistence.workspace import _open_path

    receipt_path = output_staging_receipt_path(workdir, request)
    with _open_path(workdir, receipt_path.parent.relative_to(workdir).parts) as receipt_parent:
        try:
            fd = os.open(receipt_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=receipt_parent)
        except FileNotFoundError:
            return
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_RECEIPT_BYTES:
                raise ValueError("Invalid output staging receipt file")
            receipt = json.loads(stream.read(_MAX_RECEIPT_BYTES + 1))
        parents = {target.parent for target in _output_targets(workdir, request)}
        if not isinstance(receipt, list) or len(receipt) != len(parents):
            raise ValueError("Output staging receipt must match the declared output parents")
        directories = []
        for record in receipt:
            if (
                not isinstance(record, dict) or set(record) != {"path", "device", "inode"}
                or type(record["device"]) is not int or record["device"] < 0
                or type(record["inode"]) is not int or record["inode"] <= 0
            ):
                raise ValueError("Invalid output staging ownership record")
            path = resolve_step_path(workdir, record["path"], field="output staging receipt")
            if path.parent not in parents or not _STAGE_NAME.fullmatch(path.name):
                raise ValueError("Output staging receipt refers outside the declared output parents")
            parents.remove(path.parent)
            directories.append((path, (record["device"], record["inode"])))
        removed = []
        for path, identity in directories:
            with _open_path(workdir, path.parent.relative_to(workdir).parts) as parent_fd:
                removed.append(_remove_owned_directory(parent_fd, path.name, identity))
        if all(removed):
            _remove_receipt(receipt_parent, receipt_path.name, _identity(metadata))
