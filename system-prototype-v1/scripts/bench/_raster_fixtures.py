"""Shared windowed raster fixture I/O for the P13 benchmark generators."""

import argparse
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import geopandas as gpd
import numpy as np
import pyogrio
import rasterio
from shapely.geometry import box

from validate_benchmark_manifest import ROOT, TIERS, require


@contextmanager
def new_output(output: Path):
    """Reserve a new directory and publish its manifest after its data files."""
    output.mkdir()
    published = []
    try:
        with TemporaryDirectory(prefix=".geopilot-raster-", dir=output) as temporary:
            staging = Path(temporary)
            yield staging
            manifest = staging / "manifest.json"
            require(manifest.is_file(), "Generation did not create a manifest")
            paths = sorted(path for path in staging.iterdir() if path != manifest)
            for source in [*paths, manifest]:
                target = output / source.name
                os.link(source, target)
                published.append(target)
    except BaseException:
        for path in reversed(published):
            path.unlink()
        if not any(output.iterdir()):
            output.rmdir()
        raise


def window_shape(window) -> tuple[int, int]:
    return int(window.height), int(window.width)


def tile_indices(window) -> tuple[np.ndarray, np.ndarray]:
    height, width = window_shape(window)
    return np.arange(height) + int(window.row_off), np.arange(width) + int(window.col_off)


def raster_environment():
    """Use a bounded GDAL cache for writes and their full-window verification."""
    return rasterio.Env(GDAL_CACHEMAX=32 * 1024 * 1024, GDAL_TIFF_INTERNAL_MASK=True, GDAL_NUM_THREADS="1")


def open_tiff(path, *, side, crs, transform, bands, dtype, block, nodata=None):
    """Write uncompressed tiles so analytic repetition cannot collapse benchmark input I/O."""
    return rasterio.open(
        path, "w", driver="GTiff", width=side, height=side, count=bands,
        crs=crs, transform=transform, dtype=dtype, nodata=nodata, tiled=True,
        blockxsize=block, blockysize=block,
    )


def checked_grid(dataset, *, side, crs, transform, bands, dtype, block, nodata):
    require(
        (dataset.shape, dataset.crs.to_string(), dataset.transform, dataset.count)
        == ((side, side), crs, transform, bands)
        and dataset.dtypes == (dtype,) * bands and dataset.nodatavals == (nodata,) * bands
        and dataset.is_tiled and dataset.block_shapes == [(block, block)] * bands and dataset.compression is None,
        f"Reopened grid differs from the recipe: {dataset.name}",
    )


def write_extent(path: Path, crs: str, bounds: tuple[float, float, float, float]) -> None:
    layer = "analysis_extent"
    previous = pyogrio.get_gdal_config_option("OGR_CURRENT_DATE")
    pyogrio.set_gdal_config_options({"OGR_CURRENT_DATE": "2000-01-01T00:00:00.000Z"})
    try:
        frame = gpd.GeoDataFrame({"source_feature_id": ["analysis-extent"]}, geometry=[box(*bounds)], crs=crs)
        pyogrio.write_dataframe(frame, path, layer=layer, driver="GPKG")
    finally:
        pyogrio.set_gdal_config_options({"OGR_CURRENT_DATE": previous})
    reopened = pyogrio.read_dataframe(path, layer=layer)
    require(
        reopened.source_feature_id.tolist() == ["analysis-extent"]
        and reopened.crs.to_string() == crs
        and len(reopened) == 1 and reopened.geometry.iloc[0].equals(box(*bounds)),
        "Reopened analysis extent differs from the recipe",
    )


def feed_pixels(digest, data: np.ndarray, mask: np.ndarray) -> None:
    """Canonical little-endian pixels and uint8 masks in stored block order."""
    digest.update(data.astype(data.dtype.newbyteorder("<"), copy=False).tobytes())
    digest.update(mask.astype(np.uint8, copy=False).tobytes())


def manifest(directory: Path, *, family: str, tier: str, roles: dict[str, str], reference: dict) -> dict:
    require({path.name for path in directory.iterdir()} == set(roles), "Fixture contains an unexpected sidecar")
    files = []
    for filename, role in sorted(roles.items()):
        path = directory / filename
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append({"path": filename, "role": role, "bytes": path.stat().st_size, "sha256": digest})
    document = {"version": 1, "family": family, "tier": tier, "files": files, "reference": reference}
    (directory / "manifest.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8",
    )
    return document


def run_cli(description, generate) -> int:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--tier", choices=TIERS, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory in an existing parent")
    parser.add_argument("--root", type=Path, default=ROOT, help="GeoPilot prototype holding the benchmark configuration")
    args = parser.parse_args()
    try:
        document = generate(args.tier, args.output.expanduser(), args.root.resolve(strict=True))
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Fixture generation failed: {error}\n")
    print(json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False))
    return 0
