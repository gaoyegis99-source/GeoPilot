"""Enumerate and read a selected vector or table layer in a local GeoPackage."""

from pathlib import Path

import pyogrio

from geopilot.data.errors import SourceReadError
from geopilot.data.vector_reader import local_ogr_path, read_layer_metadata
from geopilot.domain.data_asset import TableMetadata, VectorMetadata


_SQLITE_HEADER = b"SQLite format 3\x00"


def _list_verified_layers(local_path: Path) -> tuple[tuple[str, str | None], ...]:
    if local_path.suffix.lower() != ".gpkg":
        raise SourceReadError(f"Expected a local .gpkg file: {local_path}")

    try:
        # Guard driver probing against files with a misleading extension.
        with local_path.open("rb") as stream:
            if stream.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                raise SourceReadError(f"Invalid GeoPackage header: {local_path}")
        layers = tuple(
            (str(name), None if geometry_type is None else str(geometry_type))
            for name, geometry_type in pyogrio.list_layers(local_path)
        )
        driver = pyogrio.read_info(local_path, layer=layers[0][0])["driver"] if layers else None
    except SourceReadError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise SourceReadError(f"Cannot read GeoPackage {local_path}: {error}") from error

    if not layers:
        raise SourceReadError(f"No readable vector or table layers in {local_path}")
    if driver != "GPKG":
        raise SourceReadError(f"Expected the GPKG driver, found {driver}: {local_path}")
    return layers


def list_geopackage_layers(path: str | Path) -> tuple[tuple[str, str | None], ...]:
    """Return available layer names and geometry types from a verified GPKG file."""
    return _list_verified_layers(local_ogr_path(path))


def read_geopackage_metadata(
    path: str | Path, layer: str | None = None
) -> tuple[str, VectorMetadata | TableMetadata]:
    """Return the explicit or only layer name together with its local metadata."""
    local_path = local_ogr_path(path)
    layers = dict(_list_verified_layers(local_path))
    if layer is None:
        if len(layers) != 1:
            names = ", ".join(repr(name) for name in layers)
            raise SourceReadError(
                f"Select one GeoPackage layer from {names}", field="source.layer"
            )
        layer = next(iter(layers))
    elif not isinstance(layer, str) or layer not in layers:
        raise SourceReadError(f"GeoPackage layer {layer!r} does not exist", field="source.layer")

    return layer, read_layer_metadata(local_path, layer)
