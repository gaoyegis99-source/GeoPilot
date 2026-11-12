"""Fingerprint and snapshot local GIS files with their known companions."""

import errno
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from shutil import copyfileobj


_CHUNK_SIZE = 1024 * 1024
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
_URI_PREFIX = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]+:/")
_SHAPEFILE_STEM = frozenset({
    ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".qpj", ".shp.xml", ".idm", ".ind",
})
_TIFF_STEM = frozenset({".tfw", ".tifw", ".tiffw", ".wld", ".tab", ".xml", ".rpb", "_rpc.txt", ".rrd"})
_TIFF_FILENAME = frozenset({".aux.xml", ".msk", ".ovr", ".msk.ovr", ".xml"})
_GPKG_FILENAME = frozenset({"-wal", "-journal"})


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    digest: str
    size_bytes: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class _FilePart:
    role: str
    path: Path
    size_bytes: int
    modified_ns: int
    identity: tuple[int, int, int]


def _capture(role: str, path: Path, info: os.stat_result) -> _FilePart:
    if not stat.S_ISREG(info.st_mode):
        raise OSError(errno.EINVAL, "Expected a regular local file", os.fspath(path))
    return _FilePart(role, path, info.st_size, info.st_mtime_ns, (info.st_dev, info.st_ino, info.st_ctime_ns))


def _sidecars(path: Path) -> list[tuple[str, Path]]:
    extension = path.suffix.lower()
    if extension == ".shp":
        prefixes = (("stem", path.stem, _SHAPEFILE_STEM),)
    elif extension in {".tif", ".tiff", ".cog"}:
        prefixes = (("stem", path.stem, _TIFF_STEM), ("filename", path.name, _TIFF_FILENAME))
    elif extension == ".gpkg":
        prefixes = (("filename", path.name, _GPKG_FILENAME),)
    else:
        return []

    matched = []
    with os.scandir(path.parent) as entries:
        for entry in entries:
            for scope, prefix, suffixes in prefixes:
                if entry.name.startswith(prefix):
                    suffix = entry.name[len(prefix):]
                    if suffix.isascii() and suffix.lower() in suffixes:
                        matched.append((scope + suffix, path.parent / entry.name))
    return sorted(matched)


def _snapshot(path: Path) -> tuple[_FilePart, ...]:
    main = _capture("main", path, path.stat())
    return (main, *(_capture(role, sidecar, sidecar.stat()) for role, sidecar in _sidecars(path)))


def _changed(path: Path) -> OSError:
    return OSError(errno.EAGAIN, "Source file group changed while fingerprinting", os.fspath(path))


def _update_content(digest, part: _FilePart) -> None:
    # Nonblocking open keeps a concurrently substituted FIFO from blocking the caller.
    fd = os.open(part.path, _READ_FLAGS)
    try:
        if _capture(part.role, part.path, os.fstat(fd)) != part:
            raise _changed(part.path)
        remaining = part.size_bytes
        while remaining:
            chunk = os.read(fd, min(_CHUNK_SIZE, remaining))
            if not chunk:
                raise _changed(part.path)
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1) or _capture(part.role, part.path, os.fstat(fd)) != part:
            raise _changed(part.path)
    finally:
        os.close(fd)


def _local_source(path: str | os.PathLike[str]) -> Path:
    name = os.fspath(path)
    if _URI_PREFIX.match(name) or name.startswith(("//", "\\\\", "/vsicurl/", "/vsicurl_streaming/")):
        raise OSError(errno.EINVAL, "Expected a local filesystem path", name)
    return Path(name)


def capture_file_state(path: str | os.PathLike[str]) -> tuple[_FilePart, ...]:
    """Capture an opaque file group state for comparison across a local read."""
    return _snapshot(_local_source(path))


def fingerprint_file(path: str | os.PathLike[str], *, strategy: str = "content") -> FileFingerprint:
    """Return a SHA-256 digest, total byte count and latest mtime of a local file group."""
    if strategy not in ("content", "metadata"):
        raise ValueError(f"Unknown fingerprint strategy: {strategy!r}")
    source = _local_source(path)

    before = _snapshot(source)
    digest = hashlib.sha256(f"geopilot-file-{strategy}-v1\0".encode("ascii"))
    for part in before:
        digest.update(f"{part.role}:{part.size_bytes}\0".encode("ascii"))
        if strategy == "content":
            _update_content(digest, part)
        else:
            digest.update(f"{part.modified_ns}\0".encode("ascii"))

    if _snapshot(source) != before:
        raise _changed(source)
    return FileFingerprint(
        digest.hexdigest(),
        sum(part.size_bytes for part in before),
        max(part.modified_ns for part in before),
    )


def copy_file_group(source: Path, destination: Path, *, expected_digest: str) -> None:
    """Copy a stable registered file group to an unused local destination."""
    source = _local_source(source)
    destination = _local_source(destination)
    initial = _snapshot(source)
    created = []
    try:
        for part in initial:
            if part.role == "main":
                target = destination
            elif part.role.startswith("stem"):
                target = destination.with_name(destination.stem + part.role.removeprefix("stem"))
            else:
                target = destination.with_name(destination.name + part.role.removeprefix("filename"))
            with os.fdopen(os.open(part.path, _READ_FLAGS), "rb") as reader:
                if _capture(part.role, part.path, os.fstat(reader.fileno())) != part:
                    raise _changed(source)
                with target.open("xb") as writer:
                    created.append(target)
                    copyfileobj(reader, writer, length=_CHUNK_SIZE)
                if _capture(part.role, part.path, os.fstat(reader.fileno())) != part:
                    raise _changed(source)
        if _snapshot(source) != initial or fingerprint_file(destination).digest != expected_digest:
            raise _changed(source)
    except BaseException as error:
        for target in reversed(created):
            try:
                target.unlink(missing_ok=True)
            except OSError as cleanup:
                error.add_note(f"Could not remove incomplete copy {target}: {cleanup}")
        raise
