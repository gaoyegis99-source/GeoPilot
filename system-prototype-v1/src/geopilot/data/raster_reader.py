"""Read metadata from a local GeoTIFF or Cloud Optimized GeoTIFF."""

import math
from os import PathLike
from pathlib import Path
from stat import S_ISREG

import rasterio
from rasterio.crs import CRS
from rasterio.errors import RasterioError

from geopilot.data.errors import SourceReadError
from geopilot.domain.data_asset import RasterMetadata


def _crs_text(crs: CRS | None) -> str | None:
    if not crs:
        return None
    epsg = crs.to_epsg(confidence_threshold=100)
    return f"EPSG:{epsg}" if epsg is not None else crs.to_wkt()


def _nodata_value(value: int | float | None) -> int | float | str | None:
    if value is None or math.isfinite(value):
        return value
    return str(value)


def _optional_text(value: str | None) -> str | None:
    return value if value and value.strip() else None


def read_raster_metadata(path: str | PathLike[str]) -> RasterMetadata:
    """Inspect a local TIFF with the GDAL GTiff driver and close it before returning."""
    try:
        local_path = Path(path).expanduser().resolve()
        if local_path.as_posix().startswith("/vsi"):
            raise ValueError("GDAL virtual filesystem paths are outside the local source contract")
        if not S_ISREG(local_path.stat().st_mode):
            raise ValueError("Expected a regular local file")

        # A COG is also read by GTiff. Select the driver before opening the file.
        with rasterio.open(local_path, mode="r", driver="GTiff") as dataset:
            crs = _crs_text(dataset.crs)
            left, bottom, right, top = dataset.bounds
            return RasterMetadata(
                format=dataset.driver,
                crs=crs,
                bounds=(min(left, right), min(bottom, top), max(left, right), max(bottom, top)),
                issues=("CRS_UNKNOWN",) if crs is None else (),
                width=dataset.width,
                height=dataset.height,
                band_count=dataset.count,
                transform=tuple(dataset.transform)[:6],
                resolution=dataset.res,
                nodata=tuple(_nodata_value(value) for value in dataset.nodatavals),
                band_dtypes=dataset.dtypes,
                band_names=tuple(_optional_text(value) for value in dataset.descriptions),
                band_units=tuple(_optional_text(value) for value in dataset.units),
            )
    except (OSError, RasterioError, RuntimeError, TypeError, ValueError) as error:
        raise SourceReadError(f"Unable to read GeoTIFF or COG metadata from {path!s}: {error}") from error
