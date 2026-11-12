"""Measure selected classification cells on a verified equal-area projection."""

import math
from collections.abc import Iterator
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


def _tiles(width: int, height: int) -> Iterator[Window]:
    for row in range(0, height, _TILE_SIZE):
        for col in range(0, width, _TILE_SIZE):
            yield Window(col, row, min(_TILE_SIZE, width - col), min(_TILE_SIZE, height - row))


def _projected_areas(
    rows: np.ndarray, cols: np.ndarray, affine: Affine, transformer: Transformer, segments: int,
) -> np.ndarray:
    along = np.arange(segments, dtype=np.float64) / segments
    zero = np.zeros(segments, dtype=np.float64)
    one = np.ones(segments, dtype=np.float64)
    # Walk the four true affine pixel edges without repeating any corner.
    row = rows[:, None] + np.concatenate((zero, along, one, 1 - along))
    col = cols[:, None] + np.concatenate((along, one, 1 - along, zero))
    x = affine.a * col + affine.b * row + affine.c
    y = affine.d * col + affine.e * row + affine.f
    try:
        x, y = transformer.transform(x, y, errcheck=True)
    except ProjError as error:
        raise ValueError("Could not project the classification cell boundaries into the area CRS") from error
    # Translate each ring to its first vertex before summing cross products.
    x = x - x[:, :1]
    y = y - y[:, :1]
    areas = 0.5 * np.abs(np.sum(x * np.roll(y, -1, axis=1) - y * np.roll(x, -1, axis=1), axis=1))
    if not np.isfinite(areas).all() or np.any(areas <= 0):
        raise ValueError("A projected classification cell has no finite positive area")
    return areas


def _converged_areas(
    rows: np.ndarray, cols: np.ndarray, affine: Affine, transformer: Transformer,
) -> tuple[np.ndarray, int]:
    previous = _projected_areas(rows, cols, affine, transformer, _SEGMENTS_PER_EDGE[0])
    previous_converged = False
    for segments in _SEGMENTS_PER_EDGE[1:]:
        areas = _projected_areas(rows, cols, affine, transformer, segments)
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
    for start in range(0, class_ids.size, _CELL_BATCH_SIZE):
        batch = slice(start, start + _CELL_BATCH_SIZE)
        areas, segments = _converged_areas(rows[batch], cols[batch], affine, transformer)
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
