"""Classify second-minus-first NDVI and measure its original pixel footprints."""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import JsonValue

from geopilot.data.fingerprint import capture_file_state
from geopilot.domain.data_asset import RasterMetadata
from geopilot.gis.raster.area_stats import summarize_threshold_area
from geopilot.gis.raster.threshold import ThresholdInterval, ThresholdSpec, classify_raster
from geopilot.gis.raster.window_io import _check_destination, _local_source
from geopilot.remote_sensing.alignment import AlignmentInputError, _open_referenced_tiff
from geopilot.remote_sensing.change import NDVI_CHANGE_BAND, NDVI_CHANGE_DIRECTION, NDVI_CHANGE_UNIT


_Path = str | os.PathLike[str]
_CLASS_NAMES = {1: "decrease", 2: "stable", 3: "increase"}


class ChangeClassificationError(ValueError):
    code = "NDVI_CHANGE_CLASSIFICATION_INVALID"

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class ChangeClassificationResult:
    metadata: RasterMetadata
    statistics: dict[str, JsonValue]


def _validate_delta(path: Path) -> None:
    field = "inputs.change_raster"
    try:
        raster = _open_referenced_tiff(path, role=field)
    except AlignmentInputError as error:
        raise ChangeClassificationError(error.field, str(error)) from error
    with raster as dataset:
        if (
            dataset.dtypes != ("float64",) or dataset.descriptions != (NDVI_CHANGE_BAND,)
            or dataset.units != (NDVI_CHANGE_UNIT,) or dataset.scales != (1.0,) or dataset.offsets != (0.0,)
            or dataset.nodata is None or not math.isnan(dataset.nodata)
        ):
            raise ChangeClassificationError(
                field, "Expected one Float64 ndvi_change band with unit 1, NaN NoData and identity scaling",
            )
        if not dataset.crs:
            raise ChangeClassificationError(f"{field}.crs", "The NDVI change raster requires a known CRS")
        transform = dataset.transform
        if (
            not all(map(math.isfinite, transform)) or transform.is_degenerate
            or (transform.is_identity and (dataset.gcps[0] or dataset.rpcs is not None))
        ):
            raise ChangeClassificationError(f"{field}.transform", "The NDVI change raster requires an invertible affine grid")


def classify_ndvi_change(
    source: _Path, destination: _Path, *, threshold: int | float, area_crs: str,
) -> ChangeClassificationResult:
    """Publish three change classes only after measuring their equal-area statistics."""
    if type(threshold) not in (int, float) or not 0 <= threshold <= 2:
        raise ChangeClassificationError("parameters.threshold", "Expected a finite NDVI change threshold from 0 to 2")
    limit = float(threshold)
    spec = ThresholdSpec(intervals=[
        ThresholdInterval(class_id=1, lower=None, upper=-limit, include_lower=False, include_upper=False),
        ThresholdInterval(class_id=2, lower=-limit, upper=limit, include_lower=True, include_upper=True),
        ThresholdInterval(class_id=3, lower=limit, upper=None, include_lower=False, include_upper=False),
    ])
    target = Path(destination).expanduser()
    _check_destination(target)
    path = _local_source(source)
    before = capture_file_state(path)
    _validate_delta(path)
    with TemporaryDirectory(prefix=".ndvi-classification-", dir=target.parent) as directory:
        staged = Path(directory) / "classes.tif"
        metadata = classify_raster(path, staged, threshold=spec.model_dump(mode="json"))
        statistics = summarize_threshold_area(staged, area_crs=area_crs)
        if statistics["unselected_pixel_count"]:
            raise ChangeClassificationError("destination", "All valid change pixels must be assigned a change class")
        measured = {entry["class_id"]: entry for entry in statistics["classes"]}
        zero = {"pixel_count": 0, "area_m2": 0.0}
        statistics["classes"] = [
            {"class_id": class_id, "class_name": name, **measured.get(class_id, zero)}
            for class_id, name in _CLASS_NAMES.items()
        ]
        statistics.update(threshold=limit, direction=NDVI_CHANGE_DIRECTION)
        if capture_file_state(path) != before:
            raise ChangeClassificationError("inputs.change_raster", "The NDVI change raster changed during classification")
        _check_destination(target)
        os.link(staged, target)
    return ChangeClassificationResult(metadata, statistics)
