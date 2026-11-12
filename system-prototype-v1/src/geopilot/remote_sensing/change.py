"""Write second-period minus first-period NDVI on their common valid grid."""

import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import rasterio
from rasterio.io import DatasetReader, DatasetWriter
from rasterio.windows import Window

from geopilot.data.fingerprint import capture_file_state
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.data_asset import RasterMetadata
from geopilot.execution.output_staging import temporary_output_directory
from geopilot.gis.raster.window_io import _check_destination, _local_source
from geopilot.remote_sensing.alignment import ALIGNED_NDVI_BANDS, AlignmentInputError, _open_referenced_tiff
from geopilot.remote_sensing.indices import NDVI_MAXIMUM, NDVI_MINIMUM


NDVI_CHANGE_DOMAIN = "GEOPILOT_NDVI_CHANGE"
NDVI_CHANGE_BAND = "ndvi_change"
NDVI_CHANGE_UNIT = "1"
NDVI_CHANGE_DIRECTION: Literal["second_minus_first"] = "second_minus_first"
_BLOCK_SIZE = 256
_Path = str | os.PathLike[str]


class NdviChangeInputError(ValueError):
    code = "NDVI_CHANGE_INPUT_INVALID"

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class NdviChangeSummary:
    direction: Literal["second_minus_first"]
    total_pixels: int
    common_valid_pixels: int
    nodata_pixels: int
    positive_pixels: int
    negative_pixels: int
    zero_pixels: int
    minimum: float | None
    maximum: float | None


@dataclass(frozen=True, slots=True)
class NdviChangeResult:
    metadata: RasterMetadata
    summary: NdviChangeSummary


def _validate_aligned(dataset: DatasetReader) -> None:
    if (
        dataset.driver != "GTiff" or dataset.descriptions != ALIGNED_NDVI_BANDS
        or dataset.dtypes != ("float64", "float64") or dataset.units != ("1", "1")
        or dataset.scales != (1.0, 1.0) or dataset.offsets != (0.0, 0.0)
        or not all(value is not None and math.isnan(value) for value in dataset.nodatavals)
    ):
        raise NdviChangeInputError(
            "inputs.aligned", "Expected Float64 first_ndvi and second_ndvi bands with unit 1, NaN NoData and identity scaling",
        )
    if not dataset.crs:
        raise NdviChangeInputError("inputs.aligned.crs", "The aligned raster requires a known CRS")
    transform = dataset.transform
    if (
        not all(map(math.isfinite, transform)) or transform.is_degenerate
        or (transform.is_identity and (dataset.gcps[0] or dataset.rpcs is not None))
    ):
        raise NdviChangeInputError("inputs.aligned.transform", "The aligned raster requires a finite, invertible affine grid")


def _require_valid_cells(invalid: np.ndarray, tile: Window, *, field: str, message: str) -> None:
    if invalid.any():
        row, column = divmod(int(invalid.argmax()), invalid.shape[1])
        raise NdviChangeInputError(field, f"{message} at row {int(tile.row_off) + row}, column {int(tile.col_off) + column}")


def _read_common(dataset: DatasetReader, tile: Window) -> tuple[np.ndarray, np.ndarray]:
    samples = dataset.read(window=tile)
    valid = dataset.read_masks(window=tile) != 0
    mismatch = (valid[0] != valid[1]) | (np.isnan(samples) != ~valid).any(axis=0)
    _require_valid_cells(
        mismatch, tile, field="inputs.aligned.mask", message="The two GDAL masks and stored NaN NoData must agree",
    )
    common = valid[0]
    outside = common & ((samples < NDVI_MINIMUM) | (samples > NDVI_MAXIMUM)).any(axis=0)
    _require_valid_cells(outside, tile, field="inputs.aligned.values", message="Expected common NDVI within [-1, 1]")
    return samples, common


def _write_blocks(dataset: DatasetReader, output: DatasetWriter) -> NdviChangeSummary:
    common_count = positive = negative = 0
    minimum = maximum = None
    for _, tile in output.block_windows(1):
        samples, common = _read_common(dataset, tile)
        first, second = samples
        difference = np.full(common.shape, np.nan, dtype="float64")
        np.subtract(second, first, out=difference, where=common)
        output.write(difference, 1, window=tile)

        values = difference[common]
        common_count += values.size
        positive += int(np.count_nonzero(values > 0))
        negative += int(np.count_nonzero(values < 0))
        if values.size:
            block_minimum, block_maximum = float(values.min()), float(values.max())
            minimum = block_minimum if minimum is None else min(minimum, block_minimum)
            maximum = block_maximum if maximum is None else max(maximum, block_maximum)

    total = output.width * output.height
    return NdviChangeSummary(
        direction=NDVI_CHANGE_DIRECTION, total_pixels=total, common_valid_pixels=common_count,
        nodata_pixels=total - common_count, positive_pixels=positive, negative_pixels=negative,
        zero_pixels=common_count - positive - negative, minimum=minimum, maximum=maximum,
    )


def write_ndvi_change(aligned: _Path, destination: _Path) -> NdviChangeResult:
    """Publish a same-grid Float64 difference raster and its measured summary."""
    target = Path(destination).expanduser()
    _check_destination(target)
    source = _local_source(aligned)
    before = capture_file_state(source)
    try:
        raster = _open_referenced_tiff(source, role="inputs.aligned")
    except AlignmentInputError as error:
        raise NdviChangeInputError(error.field, str(error)) from error

    with raster as dataset:
        _validate_aligned(dataset)
        expected = read_raster_metadata(source).model_copy(update={
            "band_count": 1, "band_names": (NDVI_CHANGE_BAND,), "band_units": (NDVI_CHANGE_UNIT,),
            "band_dtypes": ("float64",), "nodata": ("nan",),
        })
        with temporary_output_directory(target, prefix=".ndvi-change-") as directory:
            staged = Path(directory) / "change.tif"
            with rasterio.open(
                staged, "w", driver="GTiff", width=dataset.width, height=dataset.height, count=1,
                dtype="float64", crs=dataset.crs, transform=dataset.transform, nodata=np.nan, tiled=True,
                blockxsize=_BLOCK_SIZE, blockysize=_BLOCK_SIZE, compress="deflate", BIGTIFF="IF_SAFER",
            ) as output:
                output.set_band_description(1, NDVI_CHANGE_BAND)
                output.set_band_unit(1, NDVI_CHANGE_UNIT)
                if area_or_point := dataset.tags().get("AREA_OR_POINT"):
                    output.update_tags(AREA_OR_POINT=area_or_point)
                summary = _write_blocks(dataset, output)
                output.update_tags(ns=NDVI_CHANGE_DOMAIN, SUMMARY=json.dumps(asdict(summary), allow_nan=False))

            metadata = read_raster_metadata(staged)
            if metadata != expected or any(path != staged for path in staged.parent.iterdir()):
                raise ValueError("NDVI change must retain the aligned grid in a single Float64 GeoTIFF")
            if capture_file_state(source) != before:
                raise NdviChangeInputError("inputs.aligned", "The aligned NDVI raster changed during the calculation")
            _check_destination(target)
            os.link(staged, target)
    return NdviChangeResult(metadata, summary)
