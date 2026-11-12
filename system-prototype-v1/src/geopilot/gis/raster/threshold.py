"""Classify the valid values of a terrain raster on its original pixel grid."""

import json
import math
import os
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Self

import numpy as np
import rasterio
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt, model_validator
from rasterio.io import DatasetReader, DatasetWriter

from geopilot.data.fingerprint import capture_file_state
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.data_asset import RasterMetadata
from geopilot.gis.raster.window_io import _check_destination, _local_source


BACKGROUND_CLASS = 0
NODATA_CLASS = 255
_BLOCK_SIZE = 256
_BAND_NAME = "threshold_class"
_BAND_UNIT = "class"
_Path = str | os.PathLike[str]
_Number = StrictInt | FiniteFloat


class ThresholdInterval(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", revalidate_instances="always")

    class_id: Annotated[int, Field(ge=1, le=254)]
    lower: _Number | None
    upper: _Number | None
    include_lower: bool
    include_upper: bool

    @model_validator(mode="after")
    def nonempty_interval(self) -> Self:
        if self.lower is not None and self.upper is not None:
            if self.lower > self.upper or (self.lower == self.upper and not (self.include_lower and self.include_upper)):
                raise ValueError("A threshold interval must contain at least one value")
        return self


class ThresholdSpec(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", revalidate_instances="always")

    intervals: Annotated[list[ThresholdInterval], Field(min_length=1)]

    @model_validator(mode="after")
    def disjoint_intervals(self) -> Self:
        ordered = sorted(self.intervals, key=lambda item: (
            -math.inf if item.lower is None else item.lower, not item.include_lower,
        ))
        for left, right in pairwise(ordered):
            if (left.upper is None or right.lower is None or left.upper > right.lower
                    or (left.upper == right.lower and left.include_upper and right.include_lower)):
                raise ValueError("Threshold intervals must not overlap")
        return self


def _cutoff(bound: int | float, inclusive: bool, *, lower: bool, integer: bool) -> tuple[int | float, bool]:
    if integer:
        if lower:
            return (math.ceil(bound) if inclusive else math.floor(bound) + 1), True
        return (math.floor(bound) if inclusive else math.ceil(bound) - 1), True

    try:
        cutoff = float(bound)
    except OverflowError:
        cutoff = math.inf if bound > 0 else -math.inf
    # If an integer bound rounds between representable values, retain the intended side of that bound.
    if cutoff != bound:
        inclusive = cutoff > bound if lower else cutoff < bound
    return cutoff, inclusive


def threshold_classifier(dataset: DatasetReader, spec: ThresholdSpec) -> Callable[[np.ma.MaskedArray], np.ndarray]:
    """Build the tile classifier for the selected storage type and band scaling."""
    scale, offset = dataset.scales[0], dataset.offsets[0]
    scaled = (scale, offset) != (1.0, 0.0)
    integer = np.dtype(dataset.dtypes[0]).kind in "iu" and not scaled
    limits = [(
        interval.class_id,
        None if interval.lower is None else _cutoff(interval.lower, interval.include_lower, lower=True, integer=integer),
        None if interval.upper is None else _cutoff(interval.upper, interval.include_upper, lower=False, integer=integer),
    ) for interval in spec.intervals]

    def classify(samples: np.ma.MaskedArray) -> np.ndarray:
        values = samples.data if integer else samples.data.astype(np.float64, copy=False)
        if scaled:
            with np.errstate(over="ignore", invalid="ignore"):
                values = values * scale + offset
        valid = ~np.ma.getmaskarray(samples) & np.isfinite(values)
        classes = np.full(values.shape, BACKGROUND_CLASS, dtype="uint8")
        for class_id, lower, upper in limits:
            selected = valid.copy()
            if lower is not None:
                bound, inclusive = lower
                selected &= values >= bound if inclusive else values > bound
            if upper is not None:
                bound, inclusive = upper
                selected &= values <= bound if inclusive else values < bound
            classes[selected] = class_id
        classes[~valid] = NODATA_CLASS
        return classes

    return classify


def _classify_tiles(dataset: DatasetReader, output: DatasetWriter, spec: ThresholdSpec) -> None:
    classify = threshold_classifier(dataset, spec)
    for _, tile in output.block_windows(1):
        samples = dataset.read(1, window=tile, masked=True)
        output.write(classify(samples), 1, window=tile)


def _classification_tags(dataset: DatasetReader, spec: ThresholdSpec) -> dict[str, str]:
    tags = {
        "THRESHOLD_INTERVALS": json.dumps(spec.model_dump(mode="json")["intervals"], allow_nan=False),
        "BACKGROUND_CLASS": str(BACKGROUND_CLASS), "NODATA_CLASS": str(NODATA_CLASS), "RESAMPLING": "none",
        "SOURCE_SCALE": repr(dataset.scales[0]), "SOURCE_OFFSET": repr(dataset.offsets[0]),
    }
    if source_unit := dataset.units[0]:
        tags["SOURCE_UNIT"] = source_unit
    if area_or_point := dataset.tags().get("AREA_OR_POINT"):
        tags["AREA_OR_POINT"] = area_or_point
    return tags


def classify_raster(source: _Path, destination: _Path, *, threshold: dict) -> RasterMetadata:
    """Write threshold classes with valid background 0 and NoData 255 as a new GeoTIFF."""
    spec = ThresholdSpec.model_validate(threshold)
    target = Path(destination).expanduser()
    _check_destination(target)
    path = _local_source(source)
    before = capture_file_state(path)
    source_metadata = read_raster_metadata(path)
    with rasterio.open(path, driver="GTiff") as dataset:
        if dataset.count != 1 or np.dtype(dataset.dtypes[0]).kind not in "iuf":
            raise ValueError("Threshold classification requires one real-valued raster band")
        if not all(map(math.isfinite, (dataset.scales[0], dataset.offsets[0]))):
            raise ValueError("Raster scale and offset must be finite")

        with TemporaryDirectory(prefix=".raster-threshold-", dir=target.parent, ignore_cleanup_errors=True) as directory:
            staged = Path(directory) / "classes.tif"
            with rasterio.open(
                staged, "w", driver="GTiff", width=dataset.width, height=dataset.height, count=1, dtype="uint8",
                crs=dataset.crs, transform=dataset.transform, nodata=NODATA_CLASS, tiled=True,
                blockxsize=_BLOCK_SIZE, blockysize=_BLOCK_SIZE, compress="deflate", BIGTIFF="IF_SAFER",
            ) as output:
                output.set_band_description(1, _BAND_NAME)
                output.set_band_unit(1, _BAND_UNIT)
                output.update_tags(**_classification_tags(dataset, spec))
                _classify_tiles(dataset, output, spec)
            metadata = read_raster_metadata(staged)
            expected = source_metadata.model_copy(update={
                "nodata": (NODATA_CLASS,), "band_dtypes": ("uint8",), "band_names": (_BAND_NAME,), "band_units": (_BAND_UNIT,),
            })
            if metadata != expected or any(file != staged for file in staged.parent.iterdir()):
                raise ValueError("The classification output must retain the input raster grid in a single GeoTIFF")
            if capture_file_state(path) != before:
                raise ValueError("The terrain raster changed during classification")
            _check_destination(target)
            os.link(staged, target)
    return metadata
