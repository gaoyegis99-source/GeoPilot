"""Compute NDVI from calibrated reflectance and its matching quality mask."""

import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.io import DatasetReader, DatasetWriter
from rasterio.windows import Window

from geopilot.data.fingerprint import capture_file_state
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.data_asset import RasterMetadata
from geopilot.execution.output_staging import temporary_output_directory
from geopilot.gis.raster.window_io import _check_destination, _local_source
from geopilot.remote_sensing.scaling import REFLECTANCE_UNIT


NDVI_DOMAIN = "GEOPILOT_NDVI"
NDVI_MINIMUM = -1.0
NDVI_MAXIMUM = 1.0
_BAND_NAME = "ndvi"
_UNIT = "1"
_BLOCK_SIZE = 256
_Path = str | os.PathLike[str]


class NdviInputError(ValueError):
    code = "NDVI_INPUT_INVALID"

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class NdviOutlier:
    """A stored range anomaly located by zero-based raster row and column."""

    row: int
    column: int
    value: float


@dataclass(frozen=True, slots=True)
class NdviSummary:
    """Disjoint invalid counts and finite values, including range anomalies."""

    total_pixels: int
    quality_invalid_pixels: int
    reflectance_invalid_pixels: int
    denominator_zero_pixels: int
    arithmetic_invalid_pixels: int
    valid_pixels: int
    valid_fraction: float
    minimum: float | None
    maximum: float | None
    out_of_range_pixels: int
    out_of_range_minimum: float | None
    out_of_range_maximum: float | None
    first_out_of_range: NdviOutlier | None


@dataclass(frozen=True, slots=True)
class NdviResult:
    metadata: RasterMetadata
    summary: NdviSummary


def _validate_inputs(reflectance: DatasetReader, quality: DatasetReader) -> None:
    if (
        reflectance.driver != "GTiff" or reflectance.dtypes != ("float64", "float64")
        or reflectance.descriptions != ("red", "nir") or reflectance.units != (REFLECTANCE_UNIT, REFLECTANCE_UNIT)
        or reflectance.scales != (1.0, 1.0) or reflectance.offsets != (0.0, 0.0)
        or not all(value is not None and math.isnan(value) for value in reflectance.nodatavals)
    ):
        raise NdviInputError(
            "inputs.reflectance", "Expected Float64 red and nir reflectance bands with NaN NoData and identity scaling",
        )
    if (
        quality.driver != "GTiff" or quality.dtypes != ("uint8",) or quality.descriptions != ("quality_mask",)
        or quality.nodata is not None or quality.scales != (1.0,) or quality.offsets != (0.0,)
    ):
        raise NdviInputError(
            "inputs.quality_mask", "Expected a UInt8 quality_mask band with no NoData sentinel and identity scaling",
        )
    if not reflectance.crs:
        raise NdviInputError("inputs.reflectance.crs", "The reflectance grid requires a known CRS")
    if quality.crs != reflectance.crs:
        raise NdviInputError("inputs.quality_mask.crs", "The quality mask must use the reflectance CRS")
    if quality.shape != reflectance.shape:
        raise NdviInputError("inputs.quality_mask.shape", "The quality mask must match the reflectance dimensions")
    if not all(map(math.isfinite, reflectance.transform)) or reflectance.transform.is_degenerate:
        raise NdviInputError("inputs.reflectance.transform", "The reflectance affine transform must be finite and invertible")
    if quality.transform != reflectance.transform:
        raise NdviInputError("inputs.quality_mask.transform", "The quality mask must match the exact reflectance affine transform")


def _first_cell(selected: np.ndarray) -> tuple[int, int]:
    return divmod(int(selected.argmax()), selected.shape[1])


def _quality_valid(samples: np.ma.MaskedArray, tile: Window) -> np.ndarray:
    available = ~np.ma.getmaskarray(samples)
    unsupported = available & (samples.data != 0) & (samples.data != 1)
    if unsupported.any():
        row, column = _first_cell(unsupported)
        raise NdviInputError(
            "inputs.quality_mask.values",
            f"Expected mask code 0 or 1 at row {int(tile.row_off) + row}, column {int(tile.col_off) + column}. "
            f"Observed {int(samples.data[row, column])}.",
        )
    return available & (samples.data == 1)


def _ndvi_values(spectral: np.ndarray, eligible: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    red, nir = spectral
    denominator = np.zeros_like(red)
    numerator = np.zeros_like(red)
    values = np.full(red.shape, np.nan, dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore", under="ignore"):
        np.add(nir, red, out=denominator, where=eligible)
        np.subtract(nir, red, out=numerator, where=eligible)
        denominator_zero = eligible & (denominator == 0.0)
        can_divide = eligible & ~denominator_zero & np.isfinite(denominator) & np.isfinite(numerator)
        np.divide(numerator, denominator, out=values, where=can_divide)
    finite = np.isfinite(values)
    values[~finite] = np.nan
    return values, denominator_zero, finite


def _extend_range(bounds: tuple[float, float] | None, values: np.ndarray) -> tuple[float, float]:
    minimum, maximum = float(values.min()), float(values.max())
    if bounds is None:
        return minimum, maximum
    return min(bounds[0], minimum), max(bounds[1], maximum)


def _write_blocks(reflectance: DatasetReader, quality: DatasetReader, output: DatasetWriter) -> NdviSummary:
    quality_invalid = reflectance_invalid = denominator_zero = arithmetic_invalid = valid = out_of_range = 0
    bounds = outlier_bounds = None
    first_outlier = None
    for _, tile in output.block_windows(1):
        quality_valid = _quality_valid(quality.read(1, window=tile, masked=True), tile)
        spectral = reflectance.read((1, 2), window=tile, masked=True)
        spectral_valid = ~np.ma.getmaskarray(spectral).any(axis=0) & np.isfinite(spectral.data).all(axis=0)
        eligible = quality_valid & spectral_valid
        values, zeros, finite = _ndvi_values(spectral.data, eligible)

        quality_count = int(np.count_nonzero(quality_valid))
        eligible_count = int(np.count_nonzero(eligible))
        zero_count = int(np.count_nonzero(zeros))
        finite_values = values[finite]
        quality_invalid += values.size - quality_count
        reflectance_invalid += quality_count - eligible_count
        denominator_zero += zero_count
        arithmetic_invalid += eligible_count - zero_count - finite_values.size
        valid += finite_values.size
        if finite_values.size:
            bounds = _extend_range(bounds, finite_values)

        outliers = (values < NDVI_MINIMUM) | (values > NDVI_MAXIMUM)
        outlier_values = values[outliers]
        out_of_range += outlier_values.size
        if outlier_values.size:
            outlier_bounds = _extend_range(outlier_bounds, outlier_values)
            local_row, local_column = _first_cell(outliers)
            row = int(tile.row_off) + local_row
            column = int(tile.col_off) + local_column
            if first_outlier is None or (row, column) < (first_outlier.row, first_outlier.column):
                first_outlier = NdviOutlier(row, column, float(values[local_row, local_column]))
        output.write(values, 1, window=tile)

    total = output.width * output.height
    minimum, maximum = bounds if bounds is not None else (None, None)
    outlier_minimum, outlier_maximum = outlier_bounds if outlier_bounds is not None else (None, None)
    return NdviSummary(
        total_pixels=total, quality_invalid_pixels=quality_invalid, reflectance_invalid_pixels=reflectance_invalid,
        denominator_zero_pixels=denominator_zero, arithmetic_invalid_pixels=arithmetic_invalid,
        valid_pixels=valid, valid_fraction=valid / total, minimum=minimum, maximum=maximum,
        out_of_range_pixels=out_of_range, out_of_range_minimum=outlier_minimum, out_of_range_maximum=outlier_maximum,
        first_out_of_range=first_outlier,
    )


def write_ndvi(reflectance: _Path, quality_mask: _Path, destination: _Path) -> NdviResult:
    """Write a single Float64 NDVI band and summary on the unmodified source grid."""
    target = Path(destination).expanduser()
    _check_destination(target)
    reflectance_path = _local_source(reflectance)
    quality_path = _local_source(quality_mask)
    reflectance_state = capture_file_state(reflectance_path)
    quality_state = capture_file_state(quality_path)

    with rasterio.open(reflectance_path, driver="GTiff") as spectral, rasterio.open(quality_path, driver="GTiff") as quality:
        _validate_inputs(spectral, quality)
        source_metadata = read_raster_metadata(reflectance_path)
        with temporary_output_directory(target, prefix=".ndvi-", ignore_cleanup_errors=True) as directory:
            staged = Path(directory) / "ndvi.tif"
            with rasterio.open(
                staged, "w", driver="GTiff", width=spectral.width, height=spectral.height, count=1,
                dtype="float64", crs=spectral.crs, transform=spectral.transform, nodata=np.nan,
                tiled=True, blockxsize=_BLOCK_SIZE, blockysize=_BLOCK_SIZE, compress="deflate", BIGTIFF="IF_SAFER",
            ) as output:
                output.set_band_description(1, _BAND_NAME)
                output.set_band_unit(1, _UNIT)
                if area_or_point := spectral.tags().get("AREA_OR_POINT"):
                    output.update_tags(AREA_OR_POINT=area_or_point)
                summary = _write_blocks(spectral, quality, output)
                output.update_tags(ns=NDVI_DOMAIN, SUMMARY=json.dumps(asdict(summary), allow_nan=False))

            metadata = read_raster_metadata(staged)
            expected = source_metadata.model_copy(update={
                "band_count": 1, "band_names": (_BAND_NAME,), "band_units": (_UNIT,),
                "band_dtypes": ("float64",), "nodata": ("nan",),
            })
            if metadata != expected or any(path != staged for path in staged.parent.iterdir()):
                raise ValueError("NDVI must retain the input grid in a single Float64 GeoTIFF")
            if capture_file_state(reflectance_path) != reflectance_state:
                raise NdviInputError("inputs.reflectance", "The reflectance raster changed while calculating NDVI")
            if capture_file_state(quality_path) != quality_state:
                raise NdviInputError("inputs.quality_mask", "The quality mask changed while calculating NDVI")
            _check_destination(target)
            os.link(staged, target)
    return NdviResult(metadata, summary)
