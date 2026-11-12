"""Check the published two-date NDVI products and their recorded evidence."""

import json
import math
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

import numpy as np
import rasterio
from pyproj import CRS
from rasterio.errors import RasterioError
from rasterio.io import DatasetReader

from geopilot.application.run_checks import RunCheck, RunCheckContext, RunCheckResult, _file
from geopilot.artifacts.raster_metadata import raster_tiles
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.remote_sensing.alignment import ALIGNMENT_DOMAIN, _grid
from geopilot.remote_sensing.band_mapper import map_product_bands
from geopilot.remote_sensing.change import NDVI_CHANGE_DIRECTION
from geopilot.remote_sensing.config_models import SensorProductConfig
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.remote_sensing.indices import NDVI_MAXIMUM, NDVI_MINIMUM
from geopilot.remote_sensing.scaling import _calibration_evidence, resolve_reflectance_coefficients


_PRODUCTS = {"first_product": "first_configuration", "second_product": "second_configuration"}


def _artifact(context: RunCheckContext, role: str) -> Path:
    return _file(context, context.artifacts[role].path, f"artifacts.{role}")


def _record(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected an object in the published JSON evidence")
    return value


def _configuration_matches(context: RunCheckContext) -> RunCheckResult:
    try:
        configurations = _record(_artifact(context, "sensor_configuration"))
        calibrations = _record(_artifact(context, "calibration_report"))
        verified = {}
        for role, option in _PRODUCTS.items():
            reference = context.asset_files[role]
            path = _file(context, reference.path, f"inputs.{role}")
            confirmed = SensorProductConfig.model_validate(context.plan.critical_options[option])
            mapping = map_product_bands(path, registry=SensorConfigRegistry([confirmed]))
            coefficients = resolve_reflectance_coefficients(mapping)
            source = {"path": reference.path, "fingerprint": fingerprint_file(path).digest}
            calibration = _calibration_evidence(mapping, coefficients)
            expected = {
                "source": source, "product_key": calibration["product_key"], "product_id": calibration["product_id"],
                "granule_id": calibration["granule_id"], "configuration": confirmed.model_dump(mode="json"),
            }
            observed = configurations[role]
            if not isinstance(observed, dict) or any(observed.get(key) != value for key, value in expected.items()):
                return RunCheckResult(False, "The product configuration does not match the staged source.", {"role": role})
            if calibrations[role] != {"source": source, **calibration}:
                return RunCheckResult(False, "The recorded coefficients or bands differ from the product metadata.", {"role": role})
            verified[role] = {"configuration": calibration["configuration"], "source": source, "bands": calibration["bands"]}
    except (LookupError, OSError, RasterioError, RuntimeError, TypeError, ValueError) as error:
        return RunCheckResult(False, str(error))
    return RunCheckResult(True, "Reopened both products and verified configuration, native bands and coefficients.", verified)


def _aligned(context: RunCheckContext) -> Path:
    output = context.steps["align_two_dates"].outputs["aligned_ndvi"]
    return _file(context, output.path, "steps.align_two_dates.aligned_ndvi")


def _same_grid(left: DatasetReader, right: DatasetReader) -> bool:
    return (left.crs, left.transform, left.shape) == (right.crs, right.transform, right.shape)


def _grid_record(path: Path) -> dict:
    return json.loads(json.dumps(asdict(_grid(read_raster_metadata(path))), allow_nan=False))


def _common_pixels_are_aligned(context: RunCheckContext) -> RunCheckResult:
    try:
        first_path = _artifact(context, "first_ndvi")
        second_path = _artifact(context, "second_ndvi")
        aligned_path = _aligned(context)
        report = _record(_artifact(context, "alignment_report"))
        expected = {
            "first_grid": _grid_record(first_path), "second_grid": _grid_record(second_path),
            "target_grid": _grid_record(aligned_path), "resampling": context.plan.critical_options["resampling"],
        }
        if any(report.get(key) != value for key, value in expected.items()):
            return RunCheckResult(False, "Recorded source or target grids disagree with the actual TIFF files.")
        common_pixels = 0
        with rasterio.open(aligned_path) as aligned, rasterio.open(first_path) as first, rasterio.open(second_path) as second:
            expected_warp = not _same_grid(first, second)
            if (
                aligned.count != 2 or not _same_grid(aligned, first)
                or report["warped"] is not expected_warp
                or json.loads(aligned.tags(ns=ALIGNMENT_DOMAIN)["SUMMARY"]) != report
            ):
                return RunCheckResult(False, "The aligned TIFF does not use the reported first-date grid.")
            for tile in raster_tiles(aligned.width, aligned.height):
                bands = aligned.read(window=tile)
                masks = aligned.read_masks(window=tile) > 0
                common = masks[0]
                if (
                    not np.array_equal(common, masks[1]) or not np.array_equal(np.isnan(bands), ~masks)
                    or not np.isfinite(bands[masks]).all()
                    or np.any((bands[masks] < NDVI_MINIMUM) | (bands[masks] > NDVI_MAXIMUM))
                ):
                    return RunCheckResult(False, "Both aligned bands must share the same valid NDVI mask.")
                first_values = first.read(1, window=tile, masked=True)
                if np.any(np.ma.getmaskarray(first_values) & common) or not np.array_equal(
                    first_values.data[common], bands[0][common],
                ):
                    return RunCheckResult(False, "The reference values changed while aligning the second date.")
                common_pixels += int(np.count_nonzero(common))
        total = expected["target_grid"]["width"] * expected["target_grid"]["height"]
        analysis_pixels = report["analysis_pixels"]
        date_counts = (report["first_valid_pixels"], report["second_valid_pixels"])
        passed = (
            0 < common_pixels == report["common_valid_pixels"]
            and all(common_pixels <= count <= analysis_pixels <= total for count in date_counts)
        )
        return RunCheckResult(passed, "Compare the original grids and count the actual common valid pixels.", {
            "common_valid_pixels": common_pixels, "reported_common_valid_pixels": report["common_valid_pixels"],
            "resampling": report["resampling"],
        })
    except (LookupError, OSError, RasterioError, RuntimeError, TypeError, ValueError) as error:
        return RunCheckResult(False, str(error))


def _change_counts(context: RunCheckContext, threshold: float) -> tuple[list[int], int]:
    counts = np.zeros(4, dtype=np.int64)
    with (
        rasterio.open(_aligned(context)) as aligned,
        rasterio.open(_artifact(context, "change_raster")) as delta,
        rasterio.open(_artifact(context, "change_classes")) as classes,
    ):
        if (
            aligned.count != 2 or delta.dtypes != ("float64",) or classes.dtypes != ("uint8",)
            or classes.nodata != 255 or not _same_grid(aligned, delta) or not _same_grid(delta, classes)
        ):
            raise ValueError("The change and classification must use the aligned grid.")
        for tile in raster_tiles(delta.width, delta.height):
            first, second = aligned.read(window=tile)
            valid = aligned.read_masks(1, window=tile) > 0
            change = delta.read(1, window=tile, masked=True)
            category = classes.read(1, window=tile, masked=True)
            if (
                not np.array_equal(valid, ~np.ma.getmaskarray(change))
                or not np.array_equal(valid, ~np.ma.getmaskarray(category))
                or not np.array_equal(change.data[valid], second[valid] - first[valid])
            ):
                raise ValueError("The formal change raster does not match second minus first on the common mask.")
            expected = np.where(change.data[valid] < -threshold, 1, np.where(change.data[valid] > threshold, 3, 2))
            if not np.array_equal(category.data[valid], expected):
                raise ValueError("The change classes differ from the confirmed threshold intervals.")
            counts += np.bincount(expected, minlength=4)
        return [int(value) for value in counts[1:]], delta.width * delta.height


def _change_direction_matches(context: RunCheckContext) -> RunCheckResult:
    try:
        threshold = context.plan.critical_options["change_threshold"]
        counts, total_pixels = _change_counts(context, threshold)
        report = _record(_artifact(context, "area_statistics"))
        areas = [row["area_m2"] for row in report["classes"]]
        expected = {
            "direction": NDVI_CHANGE_DIRECTION, "threshold": threshold, "resampling": "none",
            "area_method": "equal_area", "area_unit": "square_meter",
            "area_crs": CRS.from_user_input(context.plan.critical_options["area_crs"]).to_2d().to_string(),
            "valid_pixel_count": sum(counts), "selected_pixel_count": sum(counts), "unselected_pixel_count": 0,
            "nodata_pixel_count": total_pixels - sum(counts),
        }
        passed = (
            all(report.get(key) == value for key, value in expected.items())
            and [row["class_id"] for row in report["classes"]] == [1, 2, 3]
            and [row["class_name"] for row in report["classes"]] == ["decrease", "stable", "increase"]
            and [row["pixel_count"] for row in report["classes"]] == counts
            and all(math.isfinite(area) and (area > 0 if count else area == 0) for count, area in zip(counts, areas, strict=True))
            and report["total_area_m2"] == math.fsum(areas)
        )
        return RunCheckResult(passed, "Compare the actual pixel differences, classes and equal-area summary.", {
            "direction": NDVI_CHANGE_DIRECTION, "class_counts": counts, "total_area_m2": report["total_area_m2"],
        })
    except (LookupError, OSError, RasterioError, RuntimeError, TypeError, ValueError) as error:
        return RunCheckResult(False, str(error))


def _ndvi_values_in_range(context: RunCheckContext) -> RunCheckResult:
    counts = {}
    try:
        for role in ("first_ndvi", "second_ndvi"):
            count = 0
            with rasterio.open(_artifact(context, role)) as dataset:
                for tile in raster_tiles(dataset.width, dataset.height):
                    values = dataset.read(1, window=tile, masked=True).compressed()
                    count += int(np.count_nonzero(
                        ~np.isfinite(values) | (values < NDVI_MINIMUM) | (values > NDVI_MAXIMUM),
                    ))
            counts[role] = count
    except (LookupError, OSError, RasterioError, ValueError) as error:
        return RunCheckResult(False, str(error))
    return RunCheckResult(
        not any(counts.values()), "Count original NDVI values outside the physical interval [-1, 1].",
        counts, severity="warning",
    )


REMOTE_SENSING_CHECKS: Mapping[str, RunCheck] = {
    "product_configuration_matches": _configuration_matches,
    "common_valid_pixels_aligned": _common_pixels_are_aligned,
    "change_direction_is_second_minus_first": _change_direction_matches,
    "ndvi_values_in_range": _ndvi_values_in_range,
}
