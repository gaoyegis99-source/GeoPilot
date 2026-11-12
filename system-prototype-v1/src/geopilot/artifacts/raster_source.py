"""Reopen the exact registered raster and verify its local file group."""

import errno
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import rasterio
from rasterio.errors import RasterioError
from rasterio.io import DatasetReader

from geopilot.artifacts.models import Artifact
from geopilot.data.fingerprint import _sidecars, capture_file_state, fingerprint_file


class ArtifactRasterError(ValueError):
    def __init__(self, message: str, *, code: str, status_code: int, field: str = "artifact") -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.field = field


def _source_state(path: Path) -> object:
    if path.resolve(strict=True) != path or any(companion.is_symlink() for _, companion in _sidecars(path)):
        raise OSError(errno.EINVAL, "The raster file group must use canonical local files")
    return capture_file_state(path)


def _changed() -> ArtifactRasterError:
    return ArtifactRasterError(
        "The raster content differs from the registered artifact.", code="ARTIFACT_CHANGED", status_code=409,
    )


@contextmanager
def open_artifact_raster(
    artifact: Artifact, path: Path, *, source_fingerprint: str | None = None,
) -> Iterator[DatasetReader]:
    """Read a located GeoTIFF while guarding its registered content and sidecars."""
    if artifact.kind != "raster" or artifact.layer is not None:
        raise ArtifactRasterError("The artifact is not a raster.", code="ARTIFACT_NOT_RASTER", status_code=422)
    if source_fingerprint is not None and source_fingerprint != artifact.fingerprint:
        raise _changed()
    try:
        before = _source_state(path)
        if fingerprint_file(path).digest != artifact.fingerprint or _source_state(path) != before:
            raise _changed()
        dataset = rasterio.open(path, mode="r", driver="GTiff")
    except FileNotFoundError:
        raise ArtifactRasterError(
            "The registered artifact file is missing.", code="ARTIFACT_FILE_MISSING", status_code=404,
        ) from None
    except (OSError, RasterioError, RuntimeError) as error:
        if isinstance(error, OSError) and error.errno == errno.EAGAIN:
            raise _changed() from None
        raise ArtifactRasterError(
            "Could not read the registered raster file.", code="ARTIFACT_UNREADABLE", status_code=422,
        ) from None

    with dataset:
        yield dataset
    try:
        if _source_state(path) != before:
            raise _changed()
    except (OSError, RuntimeError):
        raise _changed() from None
