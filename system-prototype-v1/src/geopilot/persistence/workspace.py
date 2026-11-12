"""Prepare the local directory layout for a run and its steps."""

import errno
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


_RUN_DIRECTORIES = ("inputs", "steps", "intermediate", "outputs", "previews", "checks")


def _validate_component(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or value in {"", ".", ".."}
        or "/" in value
        or "\\" in value
        or PureWindowsPath(value).drive
        or any(char.isspace() or not char.isprintable() for char in value)
    ):
        raise ValueError(f"{field} must be a single nonempty directory name: {value!r}")


def _mkdir(path: Path, parent_fd: int, *, exist_ok: bool = False) -> None:
    try:
        os.mkdir(path.name, dir_fd=parent_fd)
    except OSError as error:
        if not (exist_ok and isinstance(error, FileExistsError)):
            raise OSError(error.errno, error.strerror, path) from error


@contextmanager
def _open_directory(path: Path, parent_fd: int | None = None) -> Iterator[int]:
    name = path if parent_fd is None else path.name
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as error:
        reason = error.strerror
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            reason = "Expected a real directory without symbolic links"
        raise OSError(error.errno, reason, path) from error
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _open_path(root: Path, parts: tuple[str, ...], *, create: bool = False) -> Iterator[int]:
    # Open each component relative to an already checked parent. O_NOFOLLOW
    # therefore applies to every component inside the canonical workspace root.
    with ExitStack() as directories:
        current = root
        fd = directories.enter_context(_open_directory(root))
        for name in parts:
            current = current / name
            if create:
                _mkdir(current, fd, exist_ok=True)
            fd = directories.enter_context(_open_directory(current, fd))
        yield fd


def _remove_empty(path: Path, parent_fd: int, failure: OSError) -> None:
    try:
        os.rmdir(path.name, dir_fd=parent_fd)
    except OSError as error:
        failure.add_note(f"Could not remove {path}: {error.strerror}")


def _prepare_run(path: Path, parent_fd: int) -> None:
    _mkdir(path, parent_fd)
    try:
        with _open_directory(path, parent_fd) as run_fd:
            created = []
            try:
                for name in _RUN_DIRECTORIES:
                    directory = path / name
                    _mkdir(directory, run_fd)
                    created.append(directory)
            except OSError as error:
                for directory in reversed(created):
                    _remove_empty(directory, run_fd, error)
                raise
    except OSError as error:
        _remove_empty(path, parent_fd, error)
        raise


@dataclass(frozen=True)
class RunWorkspace:
    _root: Path
    _task_id: str
    _run_id: str

    def __post_init__(self) -> None:
        _validate_component(self._task_id, "task_id")
        _validate_component(self._run_id, "run_id")

    @property
    def _parts(self) -> tuple[str, ...]:
        return ("tasks", self._task_id, "runs", self._run_id)

    @property
    def path(self) -> Path:
        return self._root.joinpath(*self._parts)

    @property
    def request_path(self) -> Path:
        return self.path / "request.json"

    @property
    def cancel_path(self) -> Path:
        return self.path / "cancel.flag"

    def create_step(self, step_key: str) -> Path:
        """Claim a new step directory for request.json, result.json and process.log."""
        _validate_component(step_key, "step_key")
        path = self.path / "steps" / step_key
        with _open_path(self._root, (*self._parts, "steps")) as steps_fd:
            _mkdir(path, steps_fd)
        return path


def create_run_workspace(root: str | os.PathLike[str], task_id: str, run_id: str) -> RunWorkspace:
    """Create a new run directory under the canonical workspace root."""
    canonical_root = Path(root).resolve()
    run = RunWorkspace(canonical_root, task_id, run_id)
    canonical_root.mkdir(parents=True, exist_ok=True)
    with _open_path(canonical_root, run._parts[:-1], create=True) as runs_fd:
        _prepare_run(run.path, runs_fd)
    return run
