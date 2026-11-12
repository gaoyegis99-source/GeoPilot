"""Measure selected classification cells on a verified equal-area projection."""

import math
from collections.abc import Iterator
from dataclasses import dataclass
from os import PathLike

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from rasterio.transform import Affine
from rasterio.windows import Window

from geopilot.data.fingerprint import capture_file_state
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.gis.measurement.projection import select_measurement_crs
from geopilot.gis.raster.threshold import BACKGROUND_CLASS, NODATA_CLASS
from geopilot.gis.raster.window_io import _local_source


_TILE_SIZE = 256
_CELL_BATCH_SIZE = 256
_ABSOLUTE_TOLERANCE_M2 = 1e-6
_RELATIVE_TOLERANCE = 1e-8
_SEGMENTS_PER_EDGE = (2, 4, 8, 16, 32, 64, 128, 256)
_SHARED_BATCH_SEGMENTS = _SEGMENTS_PER_EDGE[2]


def _tiles(width: int, height: int) -> Iterator[Window]:
    for row in range(0, height, _TILE_SIZE):
        for col in range(0, width, _TILE_SIZE):
            yield Window(col, row, min(_TILE_SIZE, width - col), min(_TILE_SIZE, height - row))


def _ring_offsets(along: np.ndarray, end: int) -> tuple[np.ndarray, np.ndarray]:
    zero = np.zeros_like(along)
    span = np.full_like(along, end)
    # Walk the four true affine pixel edges without repeating any corner.
    return np.concatenate((zero, along, span, end - along)), np.concatenate((along, span, end - along, zero))


def _project_points(
    row: np.ndarray, col: np.ndarray, affine: Affine, transformer: Transformer,
) -> tuple[np.ndarray, np.ndarray]:
    x = affine.a * col + affine.b * row + affine.c
    y = affine.d * col + affine.e * row + affine.f
    try:
        return transformer.transform(x, y, errcheck=True)
    except ProjError as error:
        raise ValueError("Could not project the classification cell boundaries into the area CRS") from error


def _cell_areas(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    # Translate each ring to its first vertex before summing cross products.
    x = x - x[:, :1]
    y = y - y[:, :1]
    areas = 0.5 * np.abs(np.sum(x * np.roll(y, -1, axis=1) - y * np.roll(x, -1, axis=1), axis=1))
    if not np.isfinite(areas).all() or np.any(areas <= 0):
        raise ValueError("A projected classification cell has no finite positive area")
    return areas


def _projected_areas(
    rows: np.ndarray, cols: np.ndarray, affine: Affine, transformer: Transformer, segments: int,
) -> np.ndarray:
    row, col = _ring_offsets(np.arange(segments, dtype=np.float64) / segments, 1)
    return _cell_areas(*_project_points(rows[:, None] + row, cols[:, None] + col, affine, transformer))


@dataclass(frozen=True)
class _ProjectionLevel:
    keys: np.ndarray
    inverse: np.ndarray
    reused: np.ndarray
    missing: np.ndarray
    new_rows: np.ndarray
    new_cols: np.ndarray


@dataclass(frozen=True)
class _SharedLevel:
    source: np.ndarray
    destination: np.ndarray
    unshared: np.ndarray
    rows: np.ndarray
    cols: np.ndarray


class _BatchLayout:
    """Keep the small refinement indices for the most recent shape in a tile."""

    def __init__(self, row_offsets: np.ndarray, col_offsets: np.ndarray) -> None:
        self.row_offsets, self.col_offsets = row_offsets, col_offsets
        scale = _SEGMENTS_PER_EDGE[-1]
        # The largest sampled column is the right edge of the last cell.
        self._stride = (int(col_offsets.max()) + 1) * scale + 1
        self._cell_keys = (row_offsets * self._stride + col_offsets) * scale
        self._levels: dict[int, _ProjectionLevel] = {}
        self._shared_levels: dict[int, _SharedLevel] = {}

    def matches(self, row_offsets: np.ndarray, col_offsets: np.ndarray) -> bool:
        return np.array_equal(self.row_offsets, row_offsets) and np.array_equal(self.col_offsets, col_offsets)

    def level(self, segments: int, previous_keys: np.ndarray) -> _ProjectionLevel:
        if segments in self._levels:
            return self._levels[segments]
        scale = _SEGMENTS_PER_EDGE[-1]
        along = np.arange(segments, dtype=np.int64) * (scale // segments)
        row, col = _ring_offsets(along, scale)
        ring_keys = self._cell_keys[:, None] + row * self._stride + col
        keys, inverse = np.unique(ring_keys, return_inverse=True)
        # Each finer dyadic ring contains the vertices sampled at the previous level.
        reused = np.searchsorted(keys, previous_keys)
        missing = np.ones(keys.size, dtype=bool)
        missing[reused] = False
        new_rows, new_cols = np.divmod(keys[missing], self._stride)
        level = _ProjectionLevel(
            keys, inverse.reshape(ring_keys.shape), reused, missing, new_rows / scale, new_cols / scale,
        )
        # Every successful batch needs at least these levels. Dense refinements
        # can be much larger, so retain their indices only during the current batch.
        if segments <= _SEGMENTS_PER_EDGE[2]:
            self._levels[segments] = level
        return level

    def shared_level(self, segments: int) -> _SharedLevel:
        if segments not in self._shared_levels:
            level = self._levels[segments]
            previous_keys = self._levels[_SHARED_BATCH_SEGMENTS].keys
            missing = np.flatnonzero(level.missing)
            # Same layout, one grid row later. A current vertex at local row r
            # was at local row r + 1 in the preceding batch.
            shifted = level.keys[missing] + self._stride * _SEGMENTS_PER_EDGE[-1]
            indexes = np.searchsorted(previous_keys, shifted)
            found = indexes < previous_keys.size
            found[found] = previous_keys[indexes[found]] == shifted[found]
            self._shared_levels[segments] = _SharedLevel(
                indexes[found], missing[found], missing[~found], level.new_rows[~found], level.new_cols[~found],
            )
        return self._shared_levels[segments]


class _BatchProjection:
    """Reuse dyadic boundary vertices while preserving each convergence batch."""

    def __init__(
        self, rows: np.ndarray, cols: np.ndarray, affine: Affine, transformer: Transformer,
        layout: _BatchLayout | None,
    ) -> None:
        self._row_origin, self._col_origin = int(rows.min()), int(cols.min())
        self._layout = layout or _BatchLayout(rows - self._row_origin, cols - self._col_origin)
        self._affine, self._transformer = affine, transformer
        self._keys = np.empty(0, dtype=np.int64)
        self._x, self._y = np.empty(0), np.empty(0)

    def shared_from(self, previous: "_BatchProjection | None") -> tuple[np.ndarray, np.ndarray] | None:
        """Reuse a preceding eight-segment batch whose layout moves down one row."""
        if (
            previous is not None and previous._layout is self._layout
            and previous._affine is self._affine and previous._transformer is self._transformer
            and self._row_origin == previous._row_origin + 1 and self._col_origin == previous._col_origin
        ):
            return previous._x, previous._y
        return None

    def areas(self, segments: int, previous: tuple[np.ndarray, np.ndarray] | None = None) -> np.ndarray:
        level = self._layout.level(segments, self._keys)
        shared = None
        if previous is not None and segments <= _SHARED_BATCH_SEGMENTS:
            shared = self._layout.shared_level(segments)
        rows, cols, missing = (level.new_rows, level.new_cols, level.missing) if shared is None else (
            shared.rows, shared.cols, shared.unshared,
        )
        new_x, new_y = _project_points(
            self._row_origin + rows, self._col_origin + cols, self._affine, self._transformer,
        )
        x, y = np.empty(level.keys.size), np.empty(level.keys.size)
        x[level.reused], y[level.reused] = self._x, self._y
        x[missing], y[missing] = new_x, new_y
        if shared is not None:
            x[shared.destination], y[shared.destination] = previous[0][shared.source], previous[1][shared.source]
        self._keys, self._x, self._y = level.keys, x, y
        return _cell_areas(x[level.inverse], y[level.inverse])


def _converged_areas(
    rows: np.ndarray, cols: np.ndarray, affine: Affine, transformer: Transformer,
    *, layout: _BatchLayout | None = None,
) -> tuple[np.ndarray, int]:
    return _converge_projection(_BatchProjection(rows, cols, affine, transformer, layout))


def _converge_projection(
    projection: _BatchProjection, shared: tuple[np.ndarray, np.ndarray] | None = None,
) -> tuple[np.ndarray, int]:
    previous = projection.areas(_SEGMENTS_PER_EDGE[0], shared)
    previous_converged = False
    for segments in _SEGMENTS_PER_EDGE[1:]:
        areas = projection.areas(segments, shared)
        tolerance = np.maximum(_ABSOLUTE_TOLERANCE_M2, areas * _RELATIVE_TOLERANCE)
        converged = bool(np.all(np.abs(areas - previous) <= tolerance))
        if converged and previous_converged:
            return areas, segments
        previous = areas
        previous_converged = converged
    raise ValueError(f"Projected cell areas did not converge within {_SEGMENTS_PER_EDGE[-1]} segments per edge")


def _sum_projected_tile(
    data: np.ndarray, selected: np.ndarray, tile: Window, affine: Affine, transformer: Transformer, sums: np.ndarray,
) -> int:
    rows, cols = np.nonzero(selected)
    class_ids = data[rows, cols]
    rows += int(tile.row_off)
    cols += int(tile.col_off)
    max_segments = 0
    layout = previous = None
    for start in range(0, class_ids.size, _CELL_BATCH_SIZE):
        batch = slice(start, start + _CELL_BATCH_SIZE)
        batch_rows, batch_cols = rows[batch], cols[batch]
        row_offsets, col_offsets = batch_rows - batch_rows.min(), batch_cols - batch_cols.min()
        if layout is None or not layout.matches(row_offsets, col_offsets):
            layout = _BatchLayout(row_offsets, col_offsets)
        projection = _BatchProjection(batch_rows, batch_cols, affine, transformer, layout)
        areas, segments = _converge_projection(projection, projection.shared_from(previous))
        # Retain one early-converged batch. Finer projections can be much larger.
        previous = projection if segments == _SHARED_BATCH_SEGMENTS else None
        sums += np.bincount(class_ids[batch], weights=areas, minlength=NODATA_CLASS + 1)
        max_segments = max(max_segments, segments)
    return max_segments


def summarize_threshold_area(source: str | PathLike[str], *, area_crs: str) -> dict:
    """Count the valid classes and measure their original cell footprints in square metres."""
    if not isinstance(area_crs, str) or not area_crs.strip():
        raise ValueError("An explicit equal-area CRS is required")
    path = _local_source(source)
    before = capture_file_state(path)
    metadata = read_raster_metadata(path)
    decision = select_measurement_crs(metadata, method="equal_area", target_crs=area_crs)
    source_crs = CRS.from_user_input(metadata.crs).to_2d()
    target_crs = CRS.from_user_input(decision.crs).to_2d()
    same_grid_crs = source_crs.equals(target_crs, ignore_axis_order=True)
    transformer = None if same_grid_crs else Transformer.from_crs(
        source_crs, target_crs, always_xy=True, allow_ballpark=False, only_best=True,
    )

    counts = np.zeros(NODATA_CLASS + 1, dtype=np.int64)
    sums = np.zeros(NODATA_CLASS + 1, dtype=np.float64)
    max_segments = 0
    with rasterio.open(path, driver="GTiff") as dataset:
        if dataset.count != 1 or dataset.dtypes != ("uint8",) or dataset.nodata != NODATA_CLASS:
            raise ValueError("Area statistics require one uint8 classification band with NoData 255")
        affine = dataset.transform
        determinant = abs(affine.determinant)
        if not math.isfinite(determinant) or determinant <= 0:
            raise ValueError("Area statistics require a finite nondegenerate affine grid")

        for tile in _tiles(dataset.width, dataset.height):
            block = dataset.read(1, window=tile, masked=True)
            valid = ~np.ma.getmaskarray(block) & (block.data != NODATA_CLASS)
            counts += np.bincount(block.data[valid], minlength=NODATA_CLASS + 1)
            if transformer is not None:
                selected = valid & (block.data != BACKGROUND_CLASS)
                segments = _sum_projected_tile(block.data, selected, tile, affine, transformer, sums)
                max_segments = max(max_segments, segments)
    if capture_file_state(path) != before:
        raise ValueError("The classification raster changed during area statistics")

    if same_grid_crs:
        sums = counts * determinant
    selected_classes = np.flatnonzero(counts[BACKGROUND_CLASS + 1:NODATA_CLASS]) + 1
    classes = [
        {"class_id": int(class_id), "pixel_count": int(counts[class_id]), "area_m2": float(sums[class_id])}
        for class_id in selected_classes
    ]
    valid_pixels = int(counts.sum())
    unselected_pixels = int(counts[BACKGROUND_CLASS])
    report = {
        "source_crs": metadata.crs,
        "area_crs": target_crs.to_string(),
        "area_method": decision.method,
        "area_unit": decision.unit,
        "cell_area_method": "affine_determinant" if same_grid_crs else "projected_cell_boundary",
        "resampling": "none",
        "valid_pixel_count": valid_pixels,
        "selected_pixel_count": valid_pixels - unselected_pixels,
        "unselected_pixel_count": unselected_pixels,
        "nodata_pixel_count": metadata.width * metadata.height - valid_pixels,
        "classes": classes,
        "total_area_m2": math.fsum(entry["area_m2"] for entry in classes),
    }
    if transformer is not None:
        report["boundary_convergence"] = {
            "absolute_tolerance_m2": _ABSOLUTE_TOLERANCE_M2,
            "relative_tolerance": _RELATIVE_TOLERANCE,
            "max_segments_per_edge_used": max_segments,
        }
    return report
