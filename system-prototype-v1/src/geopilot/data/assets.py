"""Register a locally readable GIS source in the asset catalogue."""

import errno
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from geopilot.data.errors import SourceReadError
from geopilot.data.fingerprint import capture_file_state, fingerprint_file
from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.data.vector_reader import read_vector_metadata
from geopilot.domain.data_asset import AssetAvailability, AssetMetadata, DataAsset, FileSource, SourceType
from geopilot.persistence.asset_repository import insert_asset, latest_asset_for_source
from geopilot.persistence.schema import open_database


_VECTOR_SUFFIXES = {".geojson", ".json", ".shp"}
_RASTER_SUFFIXES = {".tif", ".tiff", ".cog"}
_SUPPORTED_SUFFIXES = _VECTOR_SUFFIXES | _RASTER_SUFFIXES | {".gpkg"}


class InputChangedError(RuntimeError):
    code = "INPUT_CHANGED"
    phase = "data"
    field = "source.path"
    suggestion = "Refresh the asset and create a new plan draft."

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _local_source(path: str | Path) -> Path:
    try:
        source = Path(path).expanduser().resolve(strict=True)
        regular_file = source.is_file()
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise SourceReadError(f"Cannot locate local asset source {path}: {error}") from error
    if not regular_file or source.suffix.lower() not in _SUPPORTED_SUFFIXES:
        raise SourceReadError(f"Expected a supported local asset file: {source}")
    return source


def _read_metadata(path: Path, layer: str | None) -> tuple[str | None, AssetMetadata]:
    suffix = path.suffix.lower()
    if suffix == ".gpkg":
        return read_geopackage_metadata(path, layer)
    if suffix in _VECTOR_SUFFIXES:
        return None, read_vector_metadata(path)
    return None, read_raster_metadata(path)


def _source_error(path: Path, error: OSError) -> InputChangedError | SourceReadError:
    if error.errno == errno.EAGAIN:
        return InputChangedError(f"Asset source changed while reading: {path}")
    return SourceReadError(f"Cannot inspect asset source {path}: {error}")


@dataclass(frozen=True, slots=True)
class _InspectedSource:
    source: FileSource
    metadata: AssetMetadata
    fingerprint: str
    state: object


def inspect_local_source(path: str | Path, *, layer: str | None = None) -> _InspectedSource:
    """Read local metadata and compare the file group's content before and after."""
    source_path = _local_source(path)
    if layer is not None and source_path.suffix.lower() != ".gpkg":
        raise SourceReadError("A layer selection is only supported for GeoPackage", field="source.layer")

    try:
        before = capture_file_state(source_path)
        original_fingerprint = fingerprint_file(source_path)
        selected_layer, metadata = _read_metadata(source_path, layer)
        fingerprint = fingerprint_file(source_path)
        after = capture_file_state(source_path)
    except OSError as error:
        raise _source_error(source_path, error) from error
    if after != before or fingerprint.digest != original_fingerprint.digest:
        raise InputChangedError(f"Asset source changed while reading: {source_path}")

    return _InspectedSource(
        source=FileSource(path=source_path, layer=selected_layer, modified_ns=fingerprint.modified_ns),
        metadata=metadata,
        fingerprint=fingerprint.digest,
        state=after,
    )


def register_asset(
    database_path: str | Path, path: str | Path, *, layer: str | None = None
) -> DataAsset:
    """Register a local source, appending a version only when its content changes."""
    inspected = inspect_local_source(path, layer=layer)
    source_path = inspected.source.path
    source_key = json.dumps((SourceType.LOCAL_FILE.value, str(source_path), inspected.source.layer))
    with open_database(database_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            locked_state = capture_file_state(source_path)
        except OSError as error:
            raise _source_error(source_path, error) from error
        if locked_state != inspected.state:
            raise InputChangedError(f"Asset source changed while registering: {source_path}")

        existing = latest_asset_for_source(connection, source_key)
        if existing is not None and existing.fingerprint == inspected.fingerprint:
            return existing

        asset = DataAsset(
            id=existing.id if existing is not None else uuid4().hex,
            version=existing.version + 1 if existing is not None else 1,
            source_type=SourceType.LOCAL_FILE,
            source=inspected.source,
            metadata=inspected.metadata,
            fingerprint=inspected.fingerprint,
            availability=AssetAvailability.AVAILABLE,
            created_at=datetime.now(UTC),
        )
        insert_asset(connection, source_key, asset)
        return asset
