"""Keep a lock inherited by a local worker for reliable startup recovery."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from stat import S_ISDIR, S_ISREG


_WORKER_LOCK = ".worker.lock"


@contextmanager
def exclusive_local_lock(path: Path) -> Iterator[int]:
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"Expected a local lock file at {path}")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        # The worker may still hold the same file description after a service exit.
        os.close(fd)


def worker_lease(workdir: Path) -> AbstractContextManager[int]:
    return exclusive_local_lock(workdir / _WORKER_LOCK)


def managed_workers_stopped(workdir: Path) -> bool:
    """Return true when the run directory has no inherited worker lease."""
    try:
        if not S_ISDIR(workdir.lstat().st_mode):
            return False
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        with worker_lease(workdir):
            return True
    except OSError:
        return False
