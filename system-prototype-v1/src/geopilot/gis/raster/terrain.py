"""Calculate terrain values from masked, physically scaled Horn gradients."""

import json
import math
import os
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from numbers import Real
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import rasterio
from rasterio.io import DatasetReader, DatasetWriter
from rasterio.windows import Window

from geopilot.data.fingerprint import capture_file_state
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.data_asset import RasterMetadata
from geopilot.gis.raster.units import resolve_dem_units
from geopilot.gis.raster.window_io import _check_destination, _local_source
from geopilot.gis.validation.crs_validator import inspect_crs


HORN_ALGORITHM_VERSION = 1
HORN_RADIUS_PIXELS = 1
TERRAIN_NODATA = -9999.0
_BLOCK_SIZE = 256
_Path = str | os.PathLike[str]
_Derive = Callable[[np.ma.MaskedArray, np.ma.MaskedArray], np.ma.MaskedArray]


def _validate_z_factor(z_factor: float) -> None:
    if isinstance(z_factor, bool) or not isinstance(z_factor, Real) or not math.isfinite(z_factor) or z_factor <= 0:
        raise ValueError("The terrain z-factor must be positive and finite")


def validate_terrain_unit_report(source: _Path, report_path: _Path, *, band: int, z_factor: float) -> None:
    """Reopen the prepared DEM and verify its unit evidence and confirmed scale."""
    _validate_z_factor(z_factor)
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError("The terrain unit report must contain valid JSON") from error
    if not isinstance(report, dict):
        raise ValueError("Expected a terrain unit report object")
    explicit_unit = None
    if report.get("vertical_source") == "explicit":
        explicit_unit = report.get("vertical_unit")
        if not isinstance(explicit_unit, str):
            raise ValueError("The terrain unit report requires an explicit elevation unit")
    units = resolve_dem_units(read_raster_metadata(source), band=band, vertical_unit=explicit_unit)
    expected = {"band": band, **asdict(units)}
    if (
        json.dumps(report, sort_keys=True) != json.dumps(expected, sort_keys=True)
        or not math.isclose(units.z_factor, z_factor, rel_tol=1e-12)
    ):
        raise ValueError("The terrain unit report does not match the prepared DEM and confirmed z-factor")


def _horn_gradients(
    tile: np.ma.MaskedArray, east_scale: float, north_scale: float,
) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
    values = np.asarray(tile.data, dtype=np.float64)
    invalid = np.ma.getmaskarray(tile) | ~np.isfinite(values)
    mask = np.lib.stride_tricks.sliding_window_view(invalid, (3, 3)).any(axis=(-2, -1))
    z = np.where(invalid, 0.0, values)
    # Raster row indexes increase southward. Subtract the south row from the
    # north row so both derivatives point in positive grid coordinates.
    with np.errstate(over="ignore", invalid="ignore"):
        east = (
            (z[:-2, 2:] - z[:-2, :-2])
            + 2 * (z[1:-1, 2:] - z[1:-1, :-2])
            + (z[2:, 2:] - z[2:, :-2])
        ) * east_scale
        north = (
            (z[:-2, :-2] - z[2:, :-2])
            + 2 * (z[:-2, 1:-1] - z[2:, 1:-1])
            + (z[:-2, 2:] - z[2:, 2:])
        ) * north_scale
    if ((~np.isfinite(east) | ~np.isfinite(north)) & ~mask).any():
        raise ValueError("A valid terrain gradient exceeds the supported floating-point range")
    return np.ma.array(east, mask=mask), np.ma.array(north, mask=mask)


def _derive_tile(east: np.ma.MaskedArray, north: np.ma.MaskedArray, derive: _Derive) -> np.ndarray:
    result = np.ma.asarray(derive(east, north))
    if result.shape != east.shape:
        raise ValueError("The terrain function must return one value per gradient cell")
    mask = np.ma.getmaskarray(result) | np.ma.getmaskarray(east)
    with np.errstate(over="ignore", invalid="ignore"):
        values = np.where(mask, TERRAIN_NODATA, result.data).astype(np.float32)
    if not np.isfinite(values).all() or ((values == TERRAIN_NODATA) & ~mask).any():
        raise ValueError("A valid terrain value cannot be stored in the Float32 output")
    return values


def terrain_tile_values(
    source: DatasetReader, tile: Window, band: int, east_scale: float, north_scale: float, derive: _Derive,
) -> np.ndarray:
    """Read a tile's source neighborhood and return its Float32 terrain values."""
    column, row, width, height = map(int, tile.flatten())
    left, top = max(1, column), max(1, row)
    right, bottom = min(source.width - 1, column + width), min(source.height - 1, row + height)
    values = np.full((height, width), TERRAIN_NODATA, dtype=np.float32)
    if left < right and top < bottom:
        neighborhood = Window(left - 1, top - 1, right - left + 2, bottom - top + 2)
        samples = source.read(band, window=neighborhood, masked=True)
        east, north = _horn_gradients(samples, east_scale, north_scale)
        values[top - row:bottom - row, left - column:right - column] = _derive_tile(east, north, derive)
    return values


def terrain_gradient_scales(dataset: DatasetReader, band: int, z_factor: float) -> tuple[float, float]:
    """Resolve the two grid coefficients used by the Horn kernel."""
    _validate_z_factor(z_factor)
    if type(band) is not int or not 1 <= band <= dataset.count:
        raise ValueError("The selected elevation band does not exist")
    if dataset.dtypes[band - 1].startswith("complex"):
        raise ValueError("Terrain measurement requires real elevation values")
    affine = dataset.transform
    if affine.a <= 0 or affine.e >= 0 or affine.b != 0 or affine.d != 0:
        raise ValueError("Horn gradients require a north-up, axis-aligned affine grid")
    scale, offset = dataset.scales[band - 1], dataset.offsets[band - 1]
    # The constant band offset cancels in every difference.
    ratio = scale * z_factor / 8
    east_scale, north_scale = ratio / affine.a, ratio / -affine.e
    if not all(map(math.isfinite, (scale, offset, east_scale, north_scale))):
        raise ValueError("The elevation scale and offset must give finite terrain gradients")
    return east_scale, north_scale


def write_terrain_raster(
    source: _Path, destination: _Path, *, band: int, z_factor: float, output_unit: str, derive: _Derive,
) -> RasterMetadata:
    """Apply derive to east/north uphill gradients and write a new raster on the source grid."""
    _validate_z_factor(z_factor)
    if output_unit not in {"degree", "percent"}:
        raise ValueError("Terrain output units must be degree or percent")
    target = Path(destination).expanduser()
    _check_destination(target)
    path = _local_source(source)
    before = capture_file_state(path)
    crs = inspect_crs(read_raster_metadata(path), field="inputs.dem.crs")
    if crs.metres_per_unit is None or crs.is_web_mercator:
        raise ValueError("Terrain measurement requires a suitable projected DEM grid")

    with rasterio.open(path, driver="GTiff") as dataset:
        affine = dataset.transform
        east_scale, north_scale = terrain_gradient_scales(dataset, band, z_factor)
        scale, offset = dataset.scales[band - 1], dataset.offsets[band - 1]

        with TemporaryDirectory(prefix=".terrain-", dir=target.parent, ignore_cleanup_errors=True) as directory:
            staged = Path(directory) / "terrain.tif"
            with rasterio.open(
                staged, "w", driver="GTiff", width=dataset.width, height=dataset.height, count=1, dtype="float32",
                crs=dataset.crs, transform=affine, nodata=TERRAIN_NODATA, tiled=True,
                blockxsize=_BLOCK_SIZE, blockysize=_BLOCK_SIZE, compress="deflate", BIGTIFF="IF_SAFER",
            ) as output:
                output.set_band_unit(1, output_unit)
                output.set_band_description(1, "terrain")
                output.update_tags(
                    ALGORITHM="Horn", ALGORITHM_VERSION=str(HORN_ALGORITHM_VERSION), EDGE_INTERPOLATION="false",
                    RADIUS_PIXELS=str(HORN_RADIUS_PIXELS), Z_FACTOR=repr(float(z_factor)), SOURCE_BAND=str(band),
                    SOURCE_SCALE=repr(scale), SOURCE_OFFSET=repr(offset),
                )
                if area_or_point := dataset.tags().get("AREA_OR_POINT"):
                    output.update_tags(AREA_OR_POINT=area_or_point)
                for _, tile in output.block_windows(1):
                    output.write(terrain_tile_values(dataset, tile, band, east_scale, north_scale, derive), 1, window=tile)
            metadata = read_raster_metadata(staged)
            if any(file != staged for file in staged.parent.iterdir()):
                raise ValueError("The terrain output requires unsupported external GeoTIFF metadata")
            if capture_file_state(path) != before:
                raise ValueError("The prepared DEM changed during terrain calculation")
            os.link(staged, target)
    return metadata


def slope_from_gradients(east: np.ma.MaskedArray, north: np.ma.MaskedArray, *, unit: str) -> np.ma.MaskedArray:
    gradient = np.hypot(east, north)
    if unit == "degree":
        return np.degrees(np.arctan(gradient))
    return 100 * gradient


def write_slope_raster(source: _Path, destination: _Path, *, band: int, z_factor: float, slope_unit: str) -> RasterMetadata:
    """Write degree or percent slope using the magnitude of the Horn gradient."""
    return write_terrain_raster(
        source, destination, band=band, z_factor=z_factor, output_unit=slope_unit,
        derive=partial(slope_from_gradients, unit=slope_unit),
    )
