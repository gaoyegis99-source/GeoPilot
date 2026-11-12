"""Map an exact remote-sensing product configuration to stored raw DN bands."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import numpy as np
from rasterio.io import DatasetReader

from geopilot.remote_sensing.config_models import SensorBand, SensorProductConfig
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.remote_sensing.product_metadata import (
    PRODUCT_METADATA_DOMAIN,
    NativeProductBand,
    ProductMetadata,
    _open_product_raster,
    _read_product_metadata,
)


class BandMappingError(ValueError):
    def __init__(self, field: str, message: str, *, code: str = "BAND_MAPPING_INVALID") -> None:
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class MappedBand:
    """The one-based raster index and the optional native calibration band ID."""

    index: int
    code: str
    source_filename: str
    native_resolution_m: float
    nodata_dn: int | None
    dtype: str
    resampling: str | None
    band_id: int | None


@dataclass(frozen=True, slots=True)
class ProductBandMapping:
    metadata: ProductMetadata
    configuration: SensorProductConfig
    bands: Mapping[str, MappedBand]


def _validate_grid(dataset: DatasetReader, resolution_m: float) -> None:
    if not dataset.crs or not dataset.crs.is_projected or dataset.crs.linear_units_factor[1] != 1.0:
        raise BandMappingError("source.crs", "Expected the original projected metre grid of the optical bands")
    if not all(math.isclose(value, resolution_m, rel_tol=0, abs_tol=1e-8) for value in dataset.res):
        raise BandMappingError("source.resolution", f"Expected {resolution_m:g} m raw optical grid, observed {dataset.res!r}")
    if any(nodata == 0 for nodata in dataset.nodatavals):
        raise BandMappingError("source.nodata", "Shared GeoTIFF NoData=0 conflicts with original spectral and quality DN")


def _map_band(
    dataset: DatasetReader, native: NativeProductBand, configured: SensorBand, grid_resolution_m: float,
) -> MappedBand:
    field = f"bands.{native.role}"
    if (configured.code, configured.resolution_m) != (native.code, native.native_resolution_m):
        raise BandMappingError(
            field, f"Configured band {configured.code} at {configured.resolution_m:g} m disagrees with "
            f"the native {native.code} declaration at {native.native_resolution_m:g} m",
        )
    positions = [index for index, name in enumerate(dataset.descriptions, start=1) if name == native.code]
    if not positions:
        raise BandMappingError(field, f"Required GeoTIFF band description {native.code!r} is missing", code="BAND_MISSING")
    if len(positions) != 1:
        raise BandMappingError(
            field, f"GeoTIFF band description {native.code!r} identifies multiple bands {positions}", code="BAND_AMBIGUOUS",
        )
    index = positions[0]
    dtype = np.dtype(dataset.dtypes[index - 1])
    if dtype.kind != "u" or dtype.itemsize < 2:
        raise BandMappingError(field, f"Expected an unsigned integer band that preserves the native DN range, got {dtype}")
    if (dataset.scales[index - 1], dataset.offsets[index - 1]) != (1.0, 0.0):
        raise BandMappingError(field, "Expected original DN with identity GDAL scale and offset")

    resampling = None
    if native.native_resolution_m != grid_resolution_m:
        resampling = dataset.tags(index, ns=PRODUCT_METADATA_DOMAIN).get("RESAMPLING")
        if resampling != "nearest":
            raise BandMappingError(
                f"{field}.resampling", f"Band {native.code} must record RESAMPLING=nearest in {PRODUCT_METADATA_DOMAIN}",
            )
    return MappedBand(
        index, native.code, native.source_filename, native.native_resolution_m, configured.nodata_dn,
        dtype.name, resampling, native.band_id,
    )


def map_product_bands(path: str | Path, *, registry: SensorConfigRegistry) -> ProductBandMapping:
    """Reopen the packed input, match the complete product key and map exact band descriptions."""
    with _open_product_raster(path) as dataset:
        metadata = _read_product_metadata(dataset)
        configuration = registry.lookup(metadata.key)
        native_roles = {native.role: native for native in metadata.band_sources.values()}
        if configuration.band_roles.keys() != native_roles.keys():
            raise BandMappingError("configuration.band_roles", "Configured roles disagree with the product's required bands")
        resolution_m = native_roles["red"].native_resolution_m
        _validate_grid(dataset, resolution_m)
        bands = {
            role: _map_band(dataset, native_roles[role], configured, resolution_m)
            for role, configured in configuration.band_roles.items()
        }
        return ProductBandMapping(metadata, configuration, MappingProxyType(bands))
