"""Extract local OGR layer metadata without retaining an open dataset."""

import math
from pathlib import Path

import pyogrio
from pyogrio.errors import DataLayerError
from pyogrio.util import vsi_path
from pyproj import CRS
from pyproj.exceptions import CRSError

from geopilot.data.errors import SourceReadError
from geopilot.domain.data_asset import FieldMetadata, TableMetadata, VectorMetadata


_VECTOR_DRIVERS = {".geojson": "GeoJSON", ".json": "GeoJSON", ".shp": "ESRI Shapefile"}
_LAYER_DRIVERS = {**_VECTOR_DRIVERS, ".gpkg": "GPKG"}
_UNDEFINED_CRS = {"undefined cartesian srs", "undefined geographic srs"}


def local_ogr_path(path: str | Path) -> Path:
    """Resolve a regular local file before Pyogrio interprets its name."""
    try:
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_file():
            raise SourceReadError(f"Local vector source is not a regular file: {resolved}")
    except (OSError, TypeError, ValueError) as error:
        raise SourceReadError(f"Cannot open local vector source {path}: {error}") from error
    if resolved.as_posix().startswith("/vsi") or vsi_path(resolved) != str(resolved):
        raise SourceReadError(f"Vector source uses GDAL virtual path syntax: {resolved}")
    return resolved


def _source_crs(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        crs = CRS.from_user_input(value)
    except CRSError:
        return None
    return None if crs.name.casefold() in _UNDEFINED_CRS else value


def _read_metadata(
    source: str | Path, layer: str | None, drivers: dict[str, str]
) -> VectorMetadata | TableMetadata:
    path = local_ogr_path(source)
    expected_driver = drivers.get(path.suffix.lower())
    if expected_driver is None:
        raise SourceReadError(f"Unsupported local vector file extension: {path.suffix!r}")

    try:
        info = pyogrio.read_info(path, layer=layer, force_feature_count=True, force_total_bounds=True)
        if info["driver"] != expected_driver:
            raise SourceReadError(f"Expected {expected_driver}, found {info['driver']}: {path}")

        field_names = [str(name) for name in info["fields"]]
        fields = ()
        if field_names:
            attributes = pyogrio.read_dataframe(path, layer=layer, columns=field_names, read_geometry=False)
            fields = tuple(
                FieldMetadata(name=name, dtype=str(dtype), missing_count=int(attributes[name].isna().sum()))
                for name, dtype in zip(field_names, info["dtypes"], strict=True)
            )

        count = int(info["features"])
        if info["geometry_type"] is None:
            return TableMetadata(format=expected_driver, row_count=count, fields=fields)

        crs = _source_crs(info["crs"])
        raw_bounds = info["total_bounds"]
        bounds = None
        if raw_bounds is not None and all(math.isfinite(value) for value in raw_bounds):
            bounds = tuple(float(value) for value in raw_bounds)
        return VectorMetadata(
            format=expected_driver,
            geometry_type=info["geometry_type"],
            crs=crs,
            bounds=bounds,
            feature_count=count,
            fields=fields,
            issues=("CRS_UNKNOWN",) if crs is None else (),
        )
    except SourceReadError:
        raise
    except DataLayerError as error:
        raise SourceReadError(f"Cannot read layer {layer!r} from {path}: {error}", field="source.layer") from error
    except (OSError, ValueError, RuntimeError) as error:
        raise SourceReadError(f"Cannot read vector metadata from {path}: {error}") from error


def read_layer_metadata(path: str | Path, layer: str | None = None) -> VectorMetadata | TableMetadata:
    """Read a supported OGR layer, including an explicitly selected GeoPackage layer."""
    return _read_metadata(path, layer, _LAYER_DRIVERS)


def read_vector_metadata(path: str | Path) -> VectorMetadata:
    """Read a local GeoJSON or Shapefile layer."""
    metadata = _read_metadata(path, None, _VECTOR_DRIVERS)
    if isinstance(metadata, TableMetadata):
        raise SourceReadError(f"Expected a spatial vector layer: {path}")
    return metadata
