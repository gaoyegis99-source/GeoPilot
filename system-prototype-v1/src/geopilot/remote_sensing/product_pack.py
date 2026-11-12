"""Pack one local Landsat MTL or Sentinel-2 SAFE product for asset registration."""

import argparse
import json
import math
import os
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from types import MappingProxyType

import numpy as np
import rasterio
from rasterio.enums import MaskFlags, Resampling
from rasterio.errors import RasterioError
from rasterio.io import DatasetReader, DatasetWriter
from rasterio.transform import Affine
from rasterio.windows import Window

from geopilot.data.fingerprint import capture_file_state, fingerprint_file
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.data_asset import RasterMetadata
from geopilot.gis.raster.window_io import _check_destination, _local_source
from geopilot.remote_sensing.band_mapper import map_product_bands
from geopilot.remote_sensing.config_models import SensorBand, SensorConfigRef, SensorProductConfig
from geopilot.remote_sensing.config_registry import SensorConfigRegistry, load_sensor_registry
from geopilot.remote_sensing.product_metadata import (
    LANDSAT_PROFILE,
    PRODUCT_METADATA_DOMAIN,
    SENTINEL_PROFILE,
    NativeProductBand,
    ProductMetadata,
    parse_product_metadata,
)


_BLOCK_SIZE = 256
_Path = str | os.PathLike[str]


class ProductPackError(ValueError):
    def __init__(self, field: str, message: str, *, code: str = "PRODUCT_PACK_INVALID") -> None:
        super().__init__(message)
        self.field = field
        self.code = code


@dataclass(frozen=True, slots=True)
class NativeSource:
    """A physical local source and its original name relative to the XML."""

    path: Path
    relative_filename: str
    fingerprint: str

    def portable_record(self) -> dict[str, str]:
        return {"relative_filename": self.relative_filename, "fingerprint": self.fingerprint}


@dataclass(frozen=True, slots=True)
class PackedProductResult:
    path: Path
    metadata: RasterMetadata
    product: ProductMetadata
    configuration_ref: SensorConfigRef
    metadata_source: NativeSource
    native_sources: Mapping[str, NativeSource]


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    source: NativeSource
    declared_path: Path
    state: object
    field: str

    def verify(self) -> None:
        try:
            resolved = _local_source(self.declared_path)
            if resolved == self.source.path and capture_file_state(resolved) == self.state:
                return
        except (OSError, RuntimeError, ValueError):
            pass
        raise ProductPackError(self.field, "A native product file changed during packing", code="PRODUCT_SOURCE_CHANGED")


@dataclass(frozen=True, slots=True)
class _NativeBand:
    spec: NativeProductBand
    snapshot: _SourceSnapshot
    dataset: DatasetReader
    companions: tuple[_SourceSnapshot, ...]
    mask_flags: tuple[MaskFlags, ...]
    mask_policy: str
    scale_to_target: int


def _resolve_local(path: Path, *, field: str, root: Path | None = None) -> Path:
    try:
        resolved = _local_source(path)
        if root is not None and not resolved.is_relative_to(root):
            raise ValueError("The resolved file is outside the product XML directory")
        return resolved
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise ProductPackError(field, f"Expected a readable local product file at {path!s}: {error}") from error


def _snapshot(declared: Path, relative_filename: str, *, field: str, root: Path) -> _SourceSnapshot:
    path = _resolve_local(declared, field=field, root=root)
    try:
        state = capture_file_state(path)
        fingerprint = fingerprint_file(path).digest
    except OSError as error:
        raise ProductPackError(field, f"Unable to fingerprint native product file {relative_filename}: {error}") from error
    return _SourceSnapshot(NativeSource(path, relative_filename, fingerprint), declared, state, field)


def _check_companion_paths(path: Path, *, field: str, root: Path) -> None:
    # GDAL and the file-group fingerprint may both consult siblings of a TIFF or JP2.
    prefixes = (f"{path.stem}.", f"{path.stem}_")
    for sibling in path.parent.iterdir():
        if sibling.name.startswith(prefixes) and sibling.is_symlink():
            _resolve_local(sibling, field=field, root=root)


def _open_native(path: Path, *, driver: str, field: str) -> DatasetReader:
    try:
        dataset = rasterio.open(path, driver=driver, GEOREF_SOURCES="INTERNAL")
    except (OSError, RasterioError, ValueError) as error:
        raise ProductPackError(field, f"Unable to open the declared source using the {driver} driver: {error}") from error
    if dataset.driver != driver:
        dataset.close()
        raise ProductPackError(field, f"The declared source requires the {driver} driver")
    return dataset


def _inspect_native_grid(dataset: DatasetReader, spec: NativeProductBand) -> None:
    field = f"bands.{spec.role}"
    allowed_dtypes = {"uint8", "uint16"} if spec.code == "SCL" else {"uint16"}
    if dataset.count != 1 or dataset.dtypes[0] not in allowed_dtypes:
        raise ProductPackError(f"{field}.dtype", "Expected one native unsigned integer band in its supported DN type")
    if (dataset.scales, dataset.offsets) != ((1.0,), (0.0,)):
        raise ProductPackError(f"{field}.scale", "Expected original DN with identity GDAL scale and offset")
    affine = dataset.transform
    if (
        not dataset.crs or not dataset.crs.is_projected or dataset.crs.linear_units_factor[1] != 1.0
        or not all(map(math.isfinite, affine)) or affine.b != 0 or affine.d != 0
        or not math.isclose(affine.a, spec.native_resolution_m, rel_tol=0, abs_tol=1e-8)
        or not math.isclose(affine.e, -spec.native_resolution_m, rel_tol=0, abs_tol=1e-8)
    ):
        raise ProductPackError(f"{field}.grid", f"Expected a north-up, projected metre grid at {spec.native_resolution_m:g} m")


def _mask_policy(
    dataset: DatasetReader, spec: NativeProductBand, configured: SensorBand, flags: tuple[MaskFlags, ...],
) -> str:
    if MaskFlags.all_valid in flags:
        return "all_valid"
    if flags == (MaskFlags.nodata,):
        if spec.code in {"QA_PIXEL", "QA_RADSAT"} and dataset.nodata == 0:
            raise ProductPackError(f"bands.{spec.role}.nodata", "A NoData-only mask would hide legal QA code 0")
        if dataset.nodata is not None and dataset.nodata == configured.nodata_dn:
            return "configured_fill"
    return "common_mask"


def _companion_paths(dataset: DatasetReader, path: Path, *, field: str, root: Path) -> tuple[Path, ...]:
    companions = []
    for filename in sorted(set(dataset.files)):
        candidate = Path(os.path.abspath(path.parent / filename))
        if candidate == path:
            continue
        if not candidate.is_relative_to(root):
            raise ProductPackError(field, "A GDAL companion is outside the product XML directory")
        _resolve_local(candidate, field=field, root=root)
        companions.append(candidate)
    return tuple(companions)


def _open_bands(
    stack: ExitStack, root: Path, product: ProductMetadata, configuration: SensorProductConfig,
) -> list[_NativeBand]:
    driver = "GTiff" if product.profile == LANDSAT_PROFILE else "JP2OpenJPEG"
    if configuration.band_roles.keys() != {spec.role for spec in product.band_sources.values()}:
        raise ProductPackError("configuration.band_roles", "The configuration must cover the declared native band roles")
    bands = []
    identities = set()
    for spec in product.band_sources.values():
        field = f"bands.{spec.role}"
        configured = configuration.band_roles[spec.role]
        if (configured.code, configured.resolution_m) != (spec.code, spec.native_resolution_m):
            raise ProductPackError(field, "The configuration disagrees with the native band code or resolution")
        relative = PurePosixPath(spec.source_filename)
        if relative.is_absolute() or ".." in relative.parts:
            raise ProductPackError(f"{field}.path", "Expected an exact path relative to the native product XML")
        declared = root / relative
        path = _resolve_local(declared, field=f"{field}.path", root=root)
        _check_companion_paths(path, field=f"{field}.path", root=root)
        snapshot = _snapshot(declared, spec.source_filename, field=f"{field}.path", root=root)
        if snapshot.source.path != path:
            raise ProductPackError(f"{field}.path", "The native band path changed while opening it", code="PRODUCT_SOURCE_CHANGED")
        info = path.stat()
        identity = (info.st_dev, info.st_ino)
        if identity in identities:
            raise ProductPackError(f"{field}.path", "Native band declarations must identify distinct files")
        identities.add(identity)
        dataset = stack.enter_context(_open_native(path, driver=driver, field=f"{field}.driver"))
        _inspect_native_grid(dataset, spec)
        flags = tuple(dataset.mask_flag_enums[0])
        policy = _mask_policy(dataset, spec, configured, flags)
        companions = tuple(
            _snapshot(companion, companion.relative_to(root).as_posix(), field=f"{field}.path", root=root)
            for companion in _companion_paths(dataset, path, field=f"{field}.path", root=root)
        )
        bands.append(_NativeBand(spec, snapshot, dataset, companions, flags, policy, 2 if spec.code == "SCL" else 1))

    reference = bands[0].dataset
    for band in bands:
        grid, scale = band.dataset, band.scale_to_target
        if (
            grid.crs != reference.crs or grid.transform != reference.transform * Affine.scale(scale)
            or (grid.width * scale, grid.height * scale) != (reference.width, reference.height)
        ):
            raise ProductPackError(
                f"bands.{band.spec.role}.grid", "The native band must share the optical footprint and its declared pixel grid",
            )
    return bands


def _write_tiles(output: DatasetWriter, bands: Sequence[_NativeBand]) -> None:
    for _, tile in output.block_windows(1):
        shape = (int(tile.height), int(tile.width))
        common = np.ones(shape, dtype=bool)
        for index, band in enumerate(bands, start=1):
            scale = band.scale_to_target
            options = {"window": tile}
            if scale != 1:
                options = {
                    "window": Window(tile.col_off // scale, tile.row_off // scale, tile.width // scale, tile.height // scale),
                    "out_shape": shape, "resampling": Resampling.nearest,
                }
            try:
                values = band.dataset.read(1, **options)
                if band.mask_policy == "common_mask":
                    common &= band.dataset.read_masks(1, **options) > 0
            except RasterioError as error:
                raise ProductPackError(f"bands.{band.spec.role}.data", f"Unable to read native DN or mask: {error}") from error
            output.write(values.astype("uint16", copy=False), index, window=tile)
        output.write_mask(common, window=tile)


def _band_record(band: _NativeBand) -> dict:
    dataset = band.dataset
    nodata = dataset.nodata
    if nodata is not None and not math.isfinite(nodata):
        nodata = str(nodata)
    return {
        **band.snapshot.source.portable_record(), "declared_filename": band.spec.declared_filename,
        "driver": dataset.driver, "dtype": dataset.dtypes[0], "crs": dataset.crs.to_string(),
        "width": dataset.width, "height": dataset.height, "transform": tuple(dataset.transform)[:6],
        "resolution": dataset.res, "nodata": nodata, "mask_flags": [flag.name for flag in band.mask_flags],
        "mask_policy": band.mask_policy, "resampling": "nearest" if band.scale_to_target != 1 else "none",
        "companions": [part.source.portable_record() for part in band.companions],
    }


def _write_product(
    staged: Path, metadata: _SourceSnapshot, product: ProductMetadata, configuration: SensorConfigRef,
    bands: Sequence[_NativeBand],
) -> None:
    reference = bands[0].dataset
    provenance = {
        "metadata": metadata.source.portable_record(), "configuration": configuration.model_dump(mode="json"),
        "bands": {band.spec.code: _band_record(band) for band in bands},
    }
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        staged, "w", driver="GTiff", width=reference.width, height=reference.height, count=len(bands), dtype="uint16",
        crs=reference.crs, transform=reference.transform, nodata=None, tiled=True, blockxsize=_BLOCK_SIZE,
        blockysize=_BLOCK_SIZE, compress="deflate", BIGTIFF="IF_SAFER",
    ) as output:
        if area_or_point := reference.tags().get("AREA_OR_POINT"):
            output.update_tags(AREA_OR_POINT=area_or_point)
        tags = {"PROFILE": product.profile, "METADATA_XML": product.xml, "NATIVE_SOURCES": json.dumps(provenance, allow_nan=False)}
        if product.granule_id is not None:
            tags["GRANULE_ID"] = product.granule_id
        output.update_tags(ns=PRODUCT_METADATA_DOMAIN, **tags)
        output.descriptions = tuple(band.spec.code for band in bands)
        for index, band in enumerate(bands, start=1):
            if band.scale_to_target != 1:
                output.update_tags(index, ns=PRODUCT_METADATA_DOMAIN, RESAMPLING="nearest")
        _write_tiles(output, bands)


def _verify_product(
    staged: Path, product: ProductMetadata, bands: Sequence[_NativeBand], registry: SensorConfigRegistry,
) -> RasterMetadata:
    reference = bands[0].dataset
    names = tuple(band.spec.code for band in bands)
    with rasterio.open(staged, driver="GTiff") as raster:
        if (
            (raster.crs, raster.transform, raster.shape) != (reference.crs, reference.transform, reference.shape)
            or raster.descriptions != names or raster.dtypes != ("uint16",) * len(names)
            or raster.nodatavals != (None,) * len(names)
            or not all(MaskFlags.per_dataset in flags for flags in raster.mask_flag_enums)
        ):
            raise ProductPackError("destination", "The packed TIFF does not retain the optical grid, DN bands and common mask")
    if any(path != staged for path in staged.parent.iterdir()):
        raise ProductPackError("destination", "The packed product must be a single self-contained GeoTIFF")
    if map_product_bands(staged, registry=registry).metadata != product:
        raise ProductPackError("destination", "The packed TIFF does not preserve the original product metadata")
    return read_raster_metadata(staged)


def _verify_native_band(band: _NativeBand, *, root: Path) -> None:
    band.snapshot.verify()
    for companion in band.companions:
        companion.verify()

    # A JP2's file-group hash does not include its optional GDAL mask. Reopen
    # the source to detect companion additions and changes to the active mask.
    path, field = band.snapshot.source.path, band.snapshot.field
    try:
        _check_companion_paths(path, field=field, root=root)
        with _open_native(path, driver=band.dataset.driver, field=field) as current:
            flags = tuple(current.mask_flag_enums[0])
            paths = _companion_paths(current, path, field=field, root=root)
    except ProductPackError as error:
        raise ProductPackError(field, "Native product companions changed during packing", code="PRODUCT_SOURCE_CHANGED") from error
    if flags != band.mask_flags or paths != tuple(companion.declared_path for companion in band.companions):
        raise ProductPackError(field, "Native product companions changed during packing", code="PRODUCT_SOURCE_CHANGED")


def pack_native_product(
    metadata_xml_path: _Path, destination: _Path, *, profile: str,
    registry: SensorConfigRegistry, granule_id: str | None = None,
) -> PackedProductResult:
    """Publish one new raw-DN GeoTIFF from the exact native files declared in local XML."""
    target = Path(destination).expanduser().absolute()
    if target.suffix.lower() not in {".tif", ".tiff"}:
        raise ProductPackError("destination", "Expected a .tif or .tiff destination")
    _check_destination(target)

    declared_xml = Path(metadata_xml_path).expanduser().absolute()
    xml_path = _resolve_local(declared_xml, field="metadata_xml")
    root = xml_path.parent
    metadata = _snapshot(declared_xml, xml_path.name, field="metadata_xml", root=root)
    try:
        xml = xml_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ProductPackError("metadata_xml", f"Unable to read the native UTF-8 product XML: {error}") from error
    product = parse_product_metadata(xml, profile=profile, granule_id=granule_id)
    configuration = registry.lookup(product.key)

    with ExitStack() as stack:
        bands = _open_bands(stack, root, product, configuration)
        directory = Path(stack.enter_context(TemporaryDirectory(prefix=".product-pack-", dir=target.parent)))
        staged = directory / "product.tif"
        _write_product(staged, metadata, product, configuration.ref, bands)
        raster_metadata = _verify_product(staged, product, bands, registry)
        metadata.verify()
        for band in bands:
            _verify_native_band(band, root=root)
        _check_destination(target)
        os.link(staged, target)

    return PackedProductResult(
        target, raster_metadata, product, configuration.ref, metadata.source,
        MappingProxyType({band.spec.code: band.snapshot.source for band in bands}),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata_xml", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--profile", required=True, choices=(LANDSAT_PROFILE, SENTINEL_PROFILE))
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--granule-id")
    args = parser.parse_args(argv)
    try:
        result = pack_native_product(
            args.metadata_xml, args.destination, profile=args.profile, granule_id=args.granule_id,
            registry=load_sensor_registry(args.config_dir),
        )
    except (OSError, ValueError, RuntimeError, LookupError, RasterioError) as error:
        code = getattr(error, "code", "PRODUCT_PACK_FAILED")
        field = getattr(error, "field", "product")
        parser.exit(2, f"{code} [{field}] {error}\n")
    print(json.dumps({
        "path": os.fspath(result.path), "product_id": result.product.product_id, "granule_id": result.product.granule_id,
        "configuration": result.configuration_ref.model_dump(mode="json"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
