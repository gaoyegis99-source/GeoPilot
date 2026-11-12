"""Read or copy an in-bounds pixel window on a local GeoTIFF grid."""

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from errno import EEXIST
from numbers import Integral, Real
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import ColorInterp, MaskFlags
from rasterio.io import DatasetReader, DatasetWriter
from rasterio.transform import Affine
from rasterio.windows import Window

from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.data_asset import RasterMetadata
from geopilot.execution.output_staging import temporary_output_directory


_Path = str | os.PathLike[str]
_BLOCK_SIZE = 256


@dataclass(frozen=True, slots=True)
class RasterWindow:
    """Masked band-row-column data on the shifted source grid."""

    data: np.ma.MaskedArray
    transform: Affine
    crs: CRS | None
    nodata: tuple[float | None, ...]


def _local_source(source: _Path) -> Path:
    path = Path(source).expanduser().resolve(strict=True)
    if path.as_posix().startswith("/vsi") or not path.is_file():
        raise ValueError("Expected a regular local GeoTIFF or COG")
    return path


def _pixel_integer(value: object) -> int:
    if not isinstance(value, (bool, np.bool_)):
        if isinstance(value, Integral):
            return int(value)
        if isinstance(value, Real) and math.isfinite(value) and value == int(value):
            return int(value)
    raise ValueError("Window offsets and sizes must be finite integers")


def _checked_window(window: Window, dataset: DatasetReader) -> Window:
    if not isinstance(window, Window):
        raise TypeError("Expected a rasterio Window")
    column, row, width, height = map(_pixel_integer, window.flatten())
    if column < 0 or row < 0 or width <= 0 or height <= 0:
        raise ValueError("Window offsets must be nonnegative and sizes must be positive")
    if column + width > dataset.width or row + height > dataset.height:
        raise ValueError(f"Window must fit within the {dataset.width} × {dataset.height} raster")
    return Window(column, row, width, height)


def read_raster_window(
    source: _Path, window: Window, *, bands: Sequence[int] | None = None,
) -> RasterWindow:
    """Read only the requested window, with optional ordered one-based band indexes."""
    with rasterio.open(_local_source(source), driver="GTiff") as dataset:
        window = _checked_window(window, dataset)
        indexes = dataset.indexes if bands is None else tuple(bands)
        if not indexes or any(
            isinstance(band, (bool, np.bool_)) or not isinstance(band, Integral) or not 1 <= band <= dataset.count
            for band in indexes
        ):
            raise ValueError(f"Band selection must contain one-based integer indexes from 1 to {dataset.count}")
        indexes = tuple(map(int, indexes))
        return RasterWindow(
            data=dataset.read(indexes, window=window, masked=True),
            transform=dataset.window_transform(window),
            crs=dataset.crs,
            nodata=tuple(dataset.nodatavals[band - 1] for band in indexes),
        )


def _shared_mask(source: DatasetReader) -> bool:
    flags = source.mask_flag_enums
    shared = tuple(MaskFlags.per_dataset in band and MaskFlags.alpha not in band for band in flags)
    if any(not band for band in flags) or (any(shared) and not all(shared)):
        raise ValueError("GeoTIFF window copies require a shared explicit mask or native band masks")
    return all(shared)


def _copy_blocks(source: DatasetReader, output: DatasetWriter, window: Window, shared_mask: bool) -> None:
    output.descriptions = source.descriptions
    output.units = source.units
    output.scales = source.scales
    output.offsets = source.offsets
    output.colorinterp = source.colorinterp
    for band, interpretation in enumerate(source.colorinterp, start=1):
        if interpretation == ColorInterp.palette:
            output.write_colormap(band, source.colormap(band))
    if area_or_point := source.tags().get("AREA_OR_POINT"):
        output.update_tags(AREA_OR_POINT=area_or_point)

    for _, tile in output.block_windows(1):
        source_tile = Window(window.col_off + tile.col_off, window.row_off + tile.row_off, tile.width, tile.height)
        output.write(source.read(window=source_tile), window=tile)
        if shared_mask:
            output.write_mask(source.read_masks(1, window=source_tile), window=tile)


def _check_destination(destination: Path) -> None:
    # Companion files at the final name could override the new raster's metadata or masks.
    for path in (destination, *(Path(f"{destination}{suffix}") for suffix in (".msk", ".aux.xml", ".ovr"))):
        if path.exists() or path.is_symlink():
            raise FileExistsError(EEXIST, "Raster destination or companion already exists", path)


def copy_raster_window(source: _Path, destination: _Path, window: Window) -> RasterMetadata:
    """Copy all bands in bounded tiles and publish a new, self-contained GeoTIFF."""
    target = Path(destination).expanduser()
    _check_destination(target)
    with rasterio.open(_local_source(source), driver="GTiff") as dataset:
        window = _checked_window(window, dataset)
        dtypes = set(dataset.dtypes)
        nodata_values = {"nan" if value is not None and math.isnan(value) else value for value in dataset.nodatavals}
        if len(dtypes) != 1 or len(nodata_values) != 1:
            raise ValueError("GeoTIFF window copies require a shared storage dtype and NoData value")
        shared_mask = _shared_mask(dataset)
        with temporary_output_directory(target, prefix=".raster-window-", ignore_cleanup_errors=True) as directory:
            staged = Path(directory) / "window.tif"
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
                staged, "w", driver="GTiff", width=window.width, height=window.height, count=dataset.count,
                dtype=dataset.dtypes[0], crs=dataset.crs, transform=dataset.window_transform(window),
                nodata=dataset.nodata, tiled=True, blockxsize=_BLOCK_SIZE, blockysize=_BLOCK_SIZE,
                compress="deflate", BIGTIFF="IF_SAFER",
            ) as output:
                _copy_blocks(dataset, output, window, shared_mask)
            metadata = read_raster_metadata(staged)
            if any(path != staged for path in staged.parent.iterdir()):
                raise ValueError("The window copy requires unsupported external GeoTIFF metadata")
            os.link(staged, target)
    return metadata
