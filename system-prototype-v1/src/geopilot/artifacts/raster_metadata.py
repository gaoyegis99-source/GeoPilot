"""Describe a published GeoTIFF's grid and valid stored samples."""

import math
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from pydantic import JsonValue
from rasterio.windows import Window

from geopilot.data.raster_reader import read_raster_metadata


_TILE_SIZE = 256
_Bounds = tuple[float, float, float, float]


def raster_tiles(width: int, height: int) -> Iterator[Window]:
    for row in range(0, height, _TILE_SIZE):
        for column in range(0, width, _TILE_SIZE):
            yield Window(column, row, min(_TILE_SIZE, width - column), min(_TILE_SIZE, height - row))


def _valid_bounds(valid: np.ndarray, tile: Window, transform: Affine) -> _Bounds:
    rows, columns = np.nonzero(valid)
    rows += int(tile.row_off)
    columns += int(tile.col_off)
    x = transform.a * columns + transform.b * rows + transform.c
    y = transform.d * columns + transform.e * rows + transform.f
    # The extrema of each cell are the extrema of its affine corner offsets.
    return (
        float(x.min()) + min(0.0, transform.a) + min(0.0, transform.b),
        float(y.min()) + min(0.0, transform.d) + min(0.0, transform.e),
        float(x.max()) + max(0.0, transform.a) + max(0.0, transform.b),
        float(y.max()) + max(0.0, transform.d) + max(0.0, transform.e),
    )


@dataclass(slots=True)
class _BandSummary:
    valid_count: int = 0
    masked_count: int = 0
    nonfinite_count: int = 0
    bounds: _Bounds | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    mean: float | None = None

    def add(self, samples: np.ma.MaskedArray, tile: Window, transform: Affine) -> np.ndarray:
        masked = np.ma.getmaskarray(samples)
        finite = np.isfinite(samples.data)
        valid = ~masked & finite
        count = int(np.count_nonzero(valid))
        masked_count = int(np.count_nonzero(masked))
        self.masked_count += masked_count
        self.nonfinite_count += samples.size - masked_count - count
        if count:
            west, south, east, north = _valid_bounds(valid, tile, transform)
            if self.bounds is not None:
                left, bottom, right, top = self.bounds
                west, south, east, north = min(left, west), min(bottom, south), max(right, east), max(top, north)
            self.bounds = west, south, east, north
            if samples.dtype.kind in "iuf":
                self._add_real_values(samples.data[valid], count)
        self.valid_count += count
        return valid

    def _add_real_values(self, values: np.ndarray, count: int) -> None:
        minimum, maximum = values.min().item(), values.max().item()
        self.minimum = minimum if self.minimum is None else min(self.minimum, minimum)
        self.maximum = maximum if self.maximum is None else max(self.maximum, maximum)
        magnitude = float(max(abs(minimum), abs(maximum)))
        # Normalizing first avoids overflowing a sum of finite Float64 values.
        mean = float(np.mean(values.astype(np.float64) / magnitude)) * magnitude if magnitude else 0.0
        if self.mean is not None:
            total = self.valid_count + count
            if (self.mean < 0) == (mean < 0):
                mean = self.mean + (mean - self.mean) * (count / total)
            else:
                mean = self.mean * (self.valid_count / total) + mean * (count / total)
        self.mean = mean

    def record(self, band: int, dataset: rasterio.DatasetReader) -> dict[str, JsonValue]:
        scale, offset = dataset.scales[band - 1], dataset.offsets[band - 1]
        return {
            "band": band,
            "mask_flags": [flag.name for flag in dataset.mask_flag_enums[band - 1]],
            "scale": scale if math.isfinite(scale) else None,
            "offset": offset if math.isfinite(offset) else None,
            "valid_pixel_count": self.valid_count,
            "masked_pixel_count": self.masked_count,
            "nonfinite_pixel_count": self.nonfinite_count,
            "valid_bounds": list(self.bounds) if self.bounds is not None else None,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean,
        }


def read_raster_artifact_metadata(path: str | Path) -> dict[str, JsonValue]:
    """Keep the registered grid and add statistics of finite, GDAL-valid stored values."""
    metadata = read_raster_metadata(path)
    common_count = 0
    with rasterio.open(Path(path).expanduser(), driver="GTiff") as dataset:
        summaries = [_BandSummary() for _ in dataset.indexes]
        for tile in raster_tiles(dataset.width, dataset.height):
            common = np.ones((int(tile.height), int(tile.width)), dtype=bool)
            for band, summary in zip(dataset.indexes, summaries, strict=True):
                samples = dataset.read(band, window=tile, masked=True)
                common &= summary.add(samples, tile, dataset.transform)
            common_count += int(np.count_nonzero(common))
        statistics = [summary.record(band, dataset) for band, summary in zip(dataset.indexes, summaries, strict=True)]
    return {**metadata.model_dump(mode="json"), "band_statistics": statistics, "common_valid_pixel_count": common_count}
