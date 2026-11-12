"""Align an analysis extent and its algorithm neighborhood to a DEM grid."""

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from numbers import Integral, Real

from rasterio.transform import Affine
from rasterio.windows import Window, bounds as window_bounds

from geopilot.domain.data_asset import RasterMetadata


_Bounds = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class ExpandedWindow:
    """Source pixel windows and their west, south, east, north bounds in the grid CRS."""

    requested_bounds: _Bounds
    analysis_window: Window
    analysis_bounds: _Bounds
    expanded_window: Window
    expanded_bounds: _Bounds


def _checked_bounds(bounds: _Bounds) -> _Bounds:
    if len(bounds) != 4 or any(isinstance(value, bool) or not isinstance(value, Real) for value in bounds):
        raise ValueError("Analysis bounds must contain four finite coordinates")
    try:
        west, south, east, north = map(float, bounds)
    except OverflowError as error:
        raise ValueError("Analysis bounds must contain four finite coordinates") from error
    if not all(map(math.isfinite, (west, south, east, north))) or west >= east or south >= north:
        raise ValueError("Analysis bounds must be finite and have positive width and height")
    return west, south, east, north


def _axis_window(size: int, origin: float, pixel_size: float, lower: float, upper: float) -> tuple[int, int]:
    def pixel_edge(index: int) -> float:
        return origin + pixel_size * index

    last_edge = pixel_edge(size)
    if not math.isfinite(last_edge):
        raise ValueError("Source raster grid bounds must be finite")
    if lower < origin or upper > last_edge:
        raise ValueError("Analysis bounds extend beyond the source raster grid")

    # Compare against the grid's actual coordinate edges. Inverse rounding can
    # place an already aligned decimal coordinate on the far side of an edge.
    edges = range(size + 1)
    first = bisect_right(edges, lower, key=pixel_edge) - 1
    stop = bisect_left(edges, upper, key=pixel_edge)
    return first, stop


def expand_analysis_window(grid: RasterMetadata, bounds: _Bounds, radius_pixels: int) -> ExpandedWindow:
    """Cover bounds in the grid CRS, then add the requested pixel radius on each side."""
    if isinstance(radius_pixels, bool) or not isinstance(radius_pixels, Integral) or radius_pixels < 0:
        raise ValueError("Neighborhood radius must be a nonnegative integer number of pixels")
    radius = int(radius_pixels)
    requested = _checked_bounds(bounds)
    west, south, east, north = requested
    transform = Affine(*grid.transform)
    if transform.a <= 0 or transform.e >= 0 or transform.b != 0 or transform.d != 0:
        raise ValueError("DEM neighborhood expansion requires a north-up, axis-aligned affine grid")

    col_start, col_stop = _axis_window(grid.width, transform.c, transform.a, west, east)
    row_start, row_stop = _axis_window(grid.height, -transform.f, -transform.e, -north, -south)
    if (
        col_start < radius or row_start < radius
        or col_stop + radius > grid.width or row_stop + radius > grid.height
    ):
        raise ValueError(f"The {grid.width} × {grid.height} source raster cannot cover the {radius}-pixel neighborhood")

    width, height = col_stop - col_start, row_stop - row_start
    analysis = Window(col_start, row_start, width, height)
    expanded = Window(col_start - radius, row_start - radius, width + 2 * radius, height + 2 * radius)
    return ExpandedWindow(
        requested_bounds=requested,
        analysis_window=analysis,
        analysis_bounds=window_bounds(analysis, transform),
        expanded_window=expanded,
        expanded_bounds=window_bounds(expanded, transform),
    )
