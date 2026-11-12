"""Evaluate a declared raster band's NoData tag and actual sample validity."""

import math
from collections.abc import Callable

import numpy as np
from pydantic import JsonValue
from rasterio.io import DatasetReader

from geopilot.artifacts.raster_metadata import raster_tiles
from geopilot.domain.tool_contract import RasterNoDataPolicy


def check_band_nodata(
    dataset: DatasetReader, band: int, policy: RasterNoDataPolicy, *, check_cancelled: Callable[[], None],
) -> tuple[bool, dict[str, JsonValue], str]:
    """Use the GDAL band mask and finite stored values without resampling."""
    nodata = dataset.nodatavals[band - 1]
    observed: dict[str, JsonValue] = {
        "band": band,
        "dtype": dataset.dtypes[band - 1],
        "nodata": nodata if nodata is None or math.isfinite(nodata) else str(nodata),
        "mask_flags": [flag.name for flag in dataset.mask_flag_enums[band - 1]],
        "scanned_pixels": 0,
        "valid_pixels": None,
        "first_invalid": None,
    }
    if policy.nodata_declaration == "none" and nodata is not None:
        return False, observed, f"Raster band {band} requires no NoData tag. GDAL reports {observed['nodata']!r}."
    if policy.nodata_declaration == "nan" and (nodata is None or not math.isnan(nodata)):
        return False, observed, f"Raster band {band} requires a NaN NoData tag. GDAL reports {observed['nodata']!r}."
    if policy.mode == "allow":
        return True, observed, f"Raster band {band} allows missing samples."

    reject_invalid = policy.mode == "reject_invalid_samples"
    scanned = valid_count = 0
    first_invalid: dict[str, JsonValue] | None = None
    for tile in raster_tiles(dataset.width, dataset.height):
        check_cancelled()
        samples = dataset.read(band, window=tile, masked=True)
        masked = np.ma.getmaskarray(samples)
        valid = ~masked & np.isfinite(samples.data)
        count = int(np.count_nonzero(valid))
        scanned += samples.size
        valid_count += count
        if count < samples.size and first_invalid is None:
            row, column = np.unravel_index(valid.argmin(), valid.shape)
            first_invalid = {
                "row": int(row + tile.row_off),
                "column": int(column + tile.col_off),
                "reason": "gdal_mask" if masked[row, column] else "nonfinite",
            }
        if (reject_invalid and first_invalid is not None) or (not reject_invalid and count > 0):
            break
    observed.update(scanned_pixels=scanned, valid_pixels=valid_count, first_invalid=first_invalid)

    if reject_invalid and first_invalid is not None:
        reason = "excluded by the GDAL band mask" if first_invalid["reason"] == "gdal_mask" else "nonfinite stored value"
        return False, observed, (
            f"Raster band {band} has an invalid sample at row {first_invalid['row']}, "
            f"column {first_invalid['column']} ({reason})."
        )
    if not reject_invalid and valid_count == 0:
        return False, observed, f"Raster band {band} has no finite samples allowed by its GDAL mask."
    return True, observed, f"Raster band {band} satisfies the declared NoData policy."
