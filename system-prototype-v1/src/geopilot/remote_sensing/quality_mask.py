"""Combine product quality rules and source validity on the original optical grid."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.io import DatasetReader, DatasetWriter

from geopilot.data.fingerprint import capture_file_state
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.data_asset import RasterMetadata
from geopilot.execution.output_staging import temporary_output_directory
from geopilot.gis.raster.window_io import _check_destination, _local_source
from geopilot.remote_sensing.band_mapper import ProductBandMapping, map_product_bands
from geopilot.remote_sensing.config_models import BitmaskQualityRule, SensorConfigRef
from geopilot.remote_sensing.config_registry import SensorConfigRegistry


QUALITY_MASK_DOMAIN = "GEOPILOT_QUALITY_MASK"
_BAND_NAME = "quality_mask"
_BLOCK_SIZE = 256
_Path = str | os.PathLike[str]


class QualityMaskError(ValueError):
    def __init__(self, field: str, message: str, *, code: str = "QUALITY_MASK_INVALID") -> None:
        super().__init__(message)
        self.field = field
        self.code = code


@dataclass(frozen=True, slots=True)
class QualityMaskResult:
    metadata: RasterMetadata
    configuration_ref: SensorConfigRef
    valid_pixels: int
    invalid_pixels: int
    unknown_class_pixels: dict[str, int]


def _write_tiles(
    dataset: DatasetReader, output: DatasetWriter, mapping: ProductBandMapping,
) -> tuple[int, dict[str, int]]:
    rules = mapping.configuration.quality_rules
    roles = ("red", "nir", *rules)
    bands = [mapping.bands[role] for role in roles]
    indexes = tuple(band.index for band in bands)
    bit_masks: dict[str, tuple[int, int]] = {}
    class_codes: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for role, rule in rules.items():
        dtype = np.dtype(mapping.bands[role].dtype)
        if isinstance(rule, BitmaskQualityRule):
            if rule.bit_width > dtype.itemsize * 8:
                raise QualityMaskError(f"bands.{role}", "Stored quality band is narrower than the configured bit width")
            bit_masks[role] = (2**rule.bit_width - 1, sum(1 << bit for bit in rule.invalid_bits.values()))
        else:
            maximum = np.iinfo(dtype).max
            known = np.array([code for code in rule.classes.values() if code <= maximum], dtype=dtype)
            accepted = np.array([code for code in rule.valid_values if code <= maximum], dtype=dtype)
            class_codes[role] = (known, accepted)

    valid_pixels = 0
    unknown_counts = dict.fromkeys(class_codes, 0)
    for _, tile in output.block_windows(1):
        samples = dataset.read(indexes, window=tile, masked=True)
        values = samples.data
        visible = ~np.ma.getmaskarray(samples)
        for position, band in enumerate(bands):
            if band.nodata_dn is not None:
                visible[position] &= values[position] != band.nodata_dn
        valid = visible.all(axis=0)

        for position, role in enumerate(rules, start=2):
            raw = values[position]
            if role in bit_masks:
                maximum, rejected = bit_masks[role]
                if np.any(visible[position] & (raw > maximum)):
                    raise QualityMaskError(
                        f"bands.{role}", "Unmasked quality code exceeds the configured bit width",
                        code="QUALITY_CODE_OUT_OF_RANGE",
                    )
                valid &= (raw & rejected) == 0
            else:
                known, accepted = class_codes[role]
                unknown_counts[role] += int(np.count_nonzero(visible[position] & ~np.isin(raw, known)))
                valid &= np.isin(raw, accepted)

        valid_pixels += int(np.count_nonzero(valid))
        output.write(valid.astype("uint8"), 1, window=tile)
    return valid_pixels, unknown_counts


def _write_provenance(
    output: DatasetWriter, mapping: ProductBandMapping, valid: int, invalid: int, unknown: dict[str, int],
) -> None:
    quality_bands = {}
    for role in mapping.configuration.quality_rules:
        band = mapping.bands[role]
        quality_bands[role] = {
            "code": band.code, "index": band.index, "source_filename": band.source_filename,
            "native_resolution_m": band.native_resolution_m, "resampling": band.resampling,
        }
    output.update_tags(
        ns=QUALITY_MASK_DOMAIN, SOURCE_PRODUCT_ID=mapping.metadata.product_id,
        CONFIG_ID=mapping.configuration.id, CONFIG_VERSION=str(mapping.configuration.version),
        VALID_PIXELS=str(valid), INVALID_PIXELS=str(invalid),
        UNKNOWN_CLASS_PIXELS=json.dumps(unknown), QUALITY_BANDS=json.dumps(quality_bands),
    )


def write_quality_mask(source: _Path, destination: _Path, *, registry: SensorConfigRegistry) -> QualityMaskResult:
    """Write a same-grid UInt8 mask with stored values 1 (valid) and 0 (invalid)."""
    target = Path(destination).expanduser()
    _check_destination(target)
    path = _local_source(source)
    before = capture_file_state(path)
    mapping = map_product_bands(path, registry=registry)
    expected = read_raster_metadata(path).model_copy(update={
        "band_count": 1, "nodata": (None,), "band_dtypes": ("uint8",), "band_names": (_BAND_NAME,), "band_units": (None,),
    })

    with temporary_output_directory(target, prefix=".quality-mask-", ignore_cleanup_errors=True) as directory:
        staged = Path(directory) / "quality.tif"
        with rasterio.open(path, driver="GTiff") as dataset, rasterio.open(
            staged, "w", driver="GTiff", width=dataset.width, height=dataset.height, count=1, dtype="uint8",
            crs=dataset.crs, transform=dataset.transform, nodata=None, tiled=True,
            blockxsize=_BLOCK_SIZE, blockysize=_BLOCK_SIZE, compress="deflate", BIGTIFF="IF_SAFER",
        ) as output:
            output.set_band_description(1, _BAND_NAME)
            if area_or_point := dataset.tags().get("AREA_OR_POINT"):
                output.update_tags(AREA_OR_POINT=area_or_point)
            valid, unknown = _write_tiles(dataset, output, mapping)
            invalid = dataset.width * dataset.height - valid
            _write_provenance(output, mapping, valid, invalid, unknown)

        metadata = read_raster_metadata(staged)
        if metadata != expected or any(file != staged for file in staged.parent.iterdir()):
            raise QualityMaskError("destination", "The mask must retain the input grid in a single GeoTIFF")
        if capture_file_state(path) != before:
            raise QualityMaskError("source.path", "The product raster changed while generating the mask")
        _check_destination(target)
        os.link(staged, target)
    return QualityMaskResult(metadata, mapping.configuration.ref, valid, invalid, unknown)
