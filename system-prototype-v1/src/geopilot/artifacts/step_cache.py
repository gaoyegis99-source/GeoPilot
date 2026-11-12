"""Store and verify a small step result and its source as one local record."""

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from stat import S_ISREG

from geopilot.persistence.workspace import _open_directory, _open_path, _validate_component


_KEY = re.compile(r"[0-9a-f]{64}")
_FORMAT = b"geopilot-step-cache-v1"
MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_RECORD_BYTES = MAX_PAYLOAD_BYTES + 8 * 1024
_MAX_SOURCE_ID_LENGTH = 255


@dataclass(frozen=True, slots=True)
class CachedOutput:
    source_run_id: str
    source_step_key: str
    payload: bytes
    output_sha256: str


def _record_name(key: str) -> str:
    if type(key) is not str or not _KEY.fullmatch(key):
        raise ValueError("Expected a lowercase SHA-256 cache key")
    return f"{key}.cache"


def _validate_source(run_id: str, step_key: str) -> None:
    for field, value in (("source_run_id", run_id), ("source_step_key", step_key)):
        _validate_component(value, field)
        if len(value) > _MAX_SOURCE_ID_LENGTH:
            raise ValueError(f"{field} is too long")


def _encode(key: str, run_id: str, step_key: str, payload: bytes) -> bytes:
    # The version, checksum and JSON header occupy one line each. Everything
    # after the header is the original payload, including its final newline.
    header = json.dumps(
        {"key": key, "source_run_id": run_id, "source_step_key": step_key},
        ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")
    body = header + b"\n" + payload
    return b"\n".join((_FORMAT, sha256(body).hexdigest().encode("ascii"), body))


def _decode(record: bytes, key: str) -> CachedOutput:
    version, checksum, body = record.split(b"\n", 2)
    if version != _FORMAT or checksum != sha256(body).hexdigest().encode("ascii"):
        raise ValueError("Invalid cache record format or checksum")
    header, payload = body.split(b"\n", 1)
    metadata = json.loads(header)
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"key", "source_run_id", "source_step_key"}
        or metadata["key"] != key
        or not 0 < len(payload) <= MAX_PAYLOAD_BYTES
    ):
        raise ValueError("Invalid cache record identity or payload")
    run_id, step_key = metadata["source_run_id"], metadata["source_step_key"]
    _validate_source(run_id, step_key)
    return CachedOutput(run_id, step_key, payload, sha256(payload).hexdigest())


class StepCache:
    """Use a cache directory under an existing, trusted local parent."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).absolute()

    @contextmanager
    def _directory(self, *, create: bool = False) -> Iterator[int]:
        root = self._root
        anchor = Path(root.anchor)
        if root == anchor or ".." in root.parts:
            raise ValueError("Expected a cache directory without parent traversal")
        with _open_path(anchor, root.parts[1:-1]) as parent:
            if create:
                try:
                    os.mkdir(root.name, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            with _open_directory(root, parent) as directory:
                yield directory

    def load(self, key: str) -> CachedOutput | None:
        """Return a verified snapshot of one record, or a cache miss."""
        try:
            name = _record_name(key)
            with self._directory() as directory:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    status = os.fstat(stream.fileno())
                    if not S_ISREG(status.st_mode) or status.st_size > _MAX_RECORD_BYTES:
                        return None
                    record = stream.read(_MAX_RECORD_BYTES + 1)
            if len(record) <= _MAX_RECORD_BYTES:
                return _decode(record, key)
        except (OSError, ValueError, RecursionError):
            return None
        return None

    def save(self, key: str, source_run_id: str, source_step_key: str, payload: bytes) -> None:
        """Publish a complete record, replacing an earlier ordinary record for the key."""
        name = _record_name(key)
        _validate_source(source_run_id, source_step_key)
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_PAYLOAD_BYTES:
            raise ValueError("Expected a nonempty cache payload of at most 64 KiB")
        record = _encode(key, source_run_id, source_step_key, payload)

        with self._directory(create=True) as directory:
            try:
                target = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not S_ISREG(target.st_mode):
                    raise FileExistsError(f"Cache entry is not a regular file: {name}")

            temporary = ".pending-" + token_hex(16)
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(record)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
