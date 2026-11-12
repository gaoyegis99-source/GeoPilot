"""Compute downhill aspect clockwise from grid north on the prepared DEM."""

from os import PathLike

import numpy as np

from geopilot.domain.data_asset import RasterMetadata
from geopilot.gis.raster.terrain import write_terrain_raster


def aspect_from_gradients(east: np.ma.MaskedArray, north: np.ma.MaskedArray) -> np.ma.MaskedArray:
    """Return degree values with masked flat or invalid cells."""
    east_value = np.asarray(east.data)
    north_value = np.asarray(north.data)
    angle = np.mod(np.degrees(np.arctan2(-east_value, -north_value)), 360.0)
    mask = np.ma.getmaskarray(east) | np.ma.getmaskarray(north)
    mask |= ~np.isfinite(east_value) | ~np.isfinite(north_value) | ((east_value == 0) & (north_value == 0))
    # Float32 rounding can put a northwest angle on the 360-degree boundary.
    angle = np.where(angle.astype(np.float32) == 360, 0.0, angle)
    return np.ma.array(angle, mask=mask)


def calculate_aspect(
    source: str | PathLike[str], destination: str | PathLike[str], *, band: int, z_factor: float,
) -> RasterMetadata:
    """Write Horn aspect with NoData where the full 3 × 3 neighborhood is unavailable."""
    return write_terrain_raster(
        source, destination, band=band, z_factor=z_factor, output_unit="degree", derive=aspect_from_gradients,
    )
