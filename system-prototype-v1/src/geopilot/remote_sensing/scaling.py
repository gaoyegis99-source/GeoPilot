"""Convert configured raw red and near-infrared DN to surface reflectance."""

import json
import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio
from pydantic import JsonValue
from rasterio.io import DatasetReader, DatasetWriter

from geopilot.data.fingerprint import capture_file_state
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.data_asset import RasterMetadata
from geopilot.execution.output_staging import temporary_output_directory
from geopilot.gis.raster.window_io import _check_destination, _local_source
from geopilot.remote_sensing.band_mapper import ProductBandMapping, map_product_bands
from geopilot.remote_sensing.config_models import BandMetadataFieldRef, MetadataFieldRef, MultiplyAddScale
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.remote_sensing.product_metadata import ProductMetadata, ProductMetadataError, resolve_metadata_field


REFLECTANCE_DOMAIN = "GEOPILOT_REFLECTANCE"
REFLECTANCE_UNIT = "reflectance"
_ROLES = ("red", "nir")
_BLOCK_SIZE = 256
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?")
_Path = str | os.PathLike[str]


class ReflectanceScalingError(ValueError):
    code = "REFLECTANCE_SCALING_INVALID"

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class MultiplyAddCoefficients:
    scale: float
    offset: float


@dataclass(frozen=True, slots=True)
class QuantificationCoefficients:
    quantification: float
    dn_offset: float


_Coefficients = MultiplyAddCoefficients | QuantificationCoefficients


@dataclass(frozen=True, slots=True)
class ReflectanceResult:
    metadata: RasterMetadata
    evidence: dict[str, JsonValue]


def _read_coefficient(metadata: ProductMetadata, reference: MetadataFieldRef, *, positive: bool = False) -> float:
    try:
        text = resolve_metadata_field(metadata, reference)
    except ProductMetadataError as error:
        raise ReflectanceScalingError(error.field, str(error)) from error
    number = float(text) if _DECIMAL.fullmatch(text) else math.nan
    if not math.isfinite(number) or (positive and number <= 0):
        field = ".".join(("product_metadata", *reference.path))
        if isinstance(reference, BandMetadataFieldRef):
            field += f"[band_id={reference.band_id}]"
        requirement = "a finite positive number" if positive else "a finite number"
        raise ReflectanceScalingError(field, f"Expected {requirement} at {field}")
    return number


def resolve_reflectance_coefficients(mapping: ProductBandMapping) -> dict[str, _Coefficients]:
    """Resolve the original product's required calibration values for red and NIR."""
    result = {}
    metadata = mapping.metadata
    for role in _ROLES:
        spec = mapping.configuration.scale_offset[role]
        if isinstance(spec, MultiplyAddScale):
            result[role] = MultiplyAddCoefficients(
                scale=_read_coefficient(metadata, spec.scale_field, positive=True),
                offset=_read_coefficient(metadata, spec.offset_field),
            )
        else:
            if spec.dn_offset_field.band_id != mapping.bands[role].band_id:
                raise ReflectanceScalingError(
                    f"configuration.scale_offset.{role}.dn_offset_field.band_id",
                    f"The configured offset must use the native band ID for {role}",
                )
            result[role] = QuantificationCoefficients(
                quantification=_read_coefficient(metadata, spec.quantification_field, positive=True),
                dn_offset=_read_coefficient(metadata, spec.dn_offset_field),
            )
    return result


def _calibration_evidence(
    mapping: ProductBandMapping, coefficients: dict[str, _Coefficients],
) -> dict[str, JsonValue]:
    bands = {}
    for output_index, role in enumerate(_ROLES, start=1):
        band = mapping.bands[role]
        spec = mapping.configuration.scale_offset[role]
        bands[role] = {
            "source_index": band.index,
            "source_code": band.code,
            "source_filename": band.source_filename,
            "output_band": output_index,
            **spec.model_dump(mode="json"),
            **asdict(coefficients[role]),
        }
    return {
        "product_id": mapping.metadata.product_id,
        "product_key": mapping.metadata.key.model_dump(mode="json"),
        "granule_id": mapping.metadata.granule_id,
        "configuration": mapping.configuration.ref.model_dump(mode="json"),
        "bands": bands,
    }


def _scale_tiles(
    dataset: DatasetReader, output: DatasetWriter, mapping: ProductBandMapping, coefficients: dict[str, _Coefficients],
) -> None:
    for _, tile in output.block_windows(1):
        for output_index, role in enumerate(_ROLES, start=1):
            band = mapping.bands[role]
            samples = dataset.read(band.index, window=tile, masked=True)
            valid = ~np.ma.getmaskarray(samples)
            if band.nodata_dn is not None:
                valid &= samples.data != band.nodata_dn
            values = samples.data[valid].astype(np.float64)
            calibration = coefficients[role]
            with np.errstate(over="ignore", invalid="ignore"):
                if isinstance(calibration, MultiplyAddCoefficients):
                    values *= calibration.scale
                    values += calibration.offset
                else:
                    # The original DN offset precedes division, including at the zero-reflectance boundary.
                    values += calibration.dn_offset
                    values /= calibration.quantification
            if not np.isfinite(values).all():
                raise ReflectanceScalingError(f"bands.{role}", f"The {role} calibration produced nonfinite reflectance")
            reflectance = np.full(samples.shape, np.nan, dtype=np.float64)
            reflectance[valid] = values
            output.write(reflectance, output_index, window=tile)


def scale_product_reflectance(source: _Path, destination: _Path, *, registry: SensorConfigRegistry) -> ReflectanceResult:
    """Publish a calibrated two-band Float64 GeoTIFF on the original grid."""
    target = Path(destination).expanduser()
    _check_destination(target)
    path = _local_source(source)
    before = capture_file_state(path)
    mapping = map_product_bands(path, registry=registry)
    coefficients = resolve_reflectance_coefficients(mapping)
    evidence = _calibration_evidence(mapping, coefficients)
    source_metadata = read_raster_metadata(path)
    with temporary_output_directory(target, prefix=".reflectance-", ignore_cleanup_errors=True) as directory:
        staged = Path(directory) / "reflectance.tif"
        with rasterio.open(path, driver="GTiff") as dataset, rasterio.open(
            staged, "w", driver="GTiff", width=dataset.width, height=dataset.height, count=len(_ROLES),
            dtype="float64", crs=dataset.crs, transform=dataset.transform, nodata=np.nan,
            tiled=True, blockxsize=_BLOCK_SIZE, blockysize=_BLOCK_SIZE, compress="deflate", BIGTIFF="IF_SAFER",
        ) as output:
            output.descriptions = _ROLES
            output.units = (REFLECTANCE_UNIT,) * len(_ROLES)
            if area_or_point := dataset.tags().get("AREA_OR_POINT"):
                output.update_tags(AREA_OR_POINT=area_or_point)
            output.update_tags(ns=REFLECTANCE_DOMAIN, CALIBRATION=json.dumps(evidence, allow_nan=False))
            _scale_tiles(dataset, output, mapping, coefficients)

        metadata = read_raster_metadata(staged)
        expected = source_metadata.model_copy(update={
            "band_count": len(_ROLES), "nodata": ("nan",) * len(_ROLES),
            "band_dtypes": ("float64",) * len(_ROLES), "band_names": _ROLES,
            "band_units": (REFLECTANCE_UNIT,) * len(_ROLES),
        })
        if metadata != expected or any(file != staged for file in staged.parent.iterdir()):
            raise ReflectanceScalingError("destination", "Expected a single reflectance GeoTIFF on the original grid")
        if capture_file_state(path) != before:
            raise ReflectanceScalingError("source", "The raw product changed during reflectance scaling")
        _check_destination(target)
        os.link(staged, target)
    return ReflectanceResult(metadata, evidence)
