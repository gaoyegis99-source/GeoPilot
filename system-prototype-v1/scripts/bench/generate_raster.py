"""Generate and verify one tier of the synthetic P13 DEM benchmark."""

import hashlib
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.windows import transform as window_transform
from rasterio.windows import Window

from _raster_fixtures import (
    checked_grid, feed_pixels, manifest, new_output, open_tiff, raster_environment, run_cli, tile_indices, write_extent,
)
from validate_benchmark_manifest import MANIFEST, ROOT, TIERS, Manifest, Raster, exact, raster_sizes, read_yaml, require


DEM = "dem.tif"
AOI = "analysis-extent.gpkg"


def _write_dem(path, grid, period):
    with open_tiff(path, **grid) as dataset:
        dataset.set_band_description(1, "elevation")
        dataset.set_band_unit(1, "m")
        for _, window in dataset.block_windows(1):
            rows, cols = tile_indices(window)
            elevations = np.broadcast_to(100 + cols * 10, (len(rows), len(cols))).astype(grid["dtype"])
            holes = np.ix_(rows % period == period // 2, cols % period == period // 2)
            elevations[holes] = grid["nodata"]
            dataset.write(elevations, 1, window=window)


def _verify_dem(path, grid, period, expected_nodata) -> str:
    """Reopen every block and independently check planar values and hole positions."""
    digest = hashlib.sha256()
    invalid = 0
    center = period // 2
    with rasterio.open(path) as dataset:
        checked_grid(dataset, **grid)
        require(dataset.units == ("m",) and dataset.descriptions == ("elevation",), "Reopened DEM band metadata differs")
        for _, window in dataset.block_windows(1):
            elevations = dataset.read(1, window=window)
            mask = dataset.read_masks(1, window=window)
            y, x = int(window.row_off), int(window.col_off)
            height, width = elevations.shape
            hole_rows = range((center - y) % period, height, period)
            hole_cols = range((center - x) % period, width, period)
            expected_valid = np.ones((height, width), dtype=bool)
            expected_valid[np.ix_(hole_rows, hole_cols)] = False
            require(np.array_equal(mask != 0, expected_valid), "Reopened DEM NoData mask differs")
            expected_line = np.linspace(100 + 10 * x, 100 + 10 * (x + width - 1), width, dtype=grid["dtype"])
            expected = np.where(expected_valid, expected_line, grid["nodata"])
            require(np.array_equal(elevations, expected), "Reopened DEM differs from the analytic input plane")
            invalid += int(np.count_nonzero(mask == 0))
            feed_pixels(digest, elevations, mask)
    require(invalid == expected_nodata, "Reopened DEM NoData count differs from the analytical reference")
    return digest.hexdigest()


def generate(tier: str, output: Path, root: Path = ROOT) -> dict:
    specification = Manifest.model_validate(read_yaml(root, MANIFEST))
    profile = Raster.model_validate(read_yaml(root, specification.profiles.raster))
    exact(profile.tiers, TIERS, "raster.tiers")
    sizes = raster_sizes(profile)[tier]
    side = profile.tiers[tier].side_pixels
    pixel = profile.pixel_m
    north, east = profile.origin_north_m, profile.origin_east_m
    transform = from_origin(east, north, pixel, pixel)
    halo = profile.halo_pixels
    inner_side = side - 2 * halo
    analysis = Window(halo, halo, inner_side, inner_side)
    extent = rasterio.windows.bounds(analysis, transform)
    grid = {
        "side": side, "crs": profile.crs, "transform": transform, "bands": profile.bands,
        "dtype": profile.dtype, "block": profile.block_pixels, "nodata": profile.nodata,
    }
    with new_output(output) as directory, raster_environment():
        _write_dem(directory / DEM, grid, profile.nodata_period_pixels)
        logical_digest = _verify_dem(directory / DEM, grid, profile.nodata_period_pixels, sizes["input_nodata_pixels"])
        write_extent(directory / AOI, profile.crs, extent)
        return manifest(
            directory, family="raster", tier=tier, roles={DEM: "dem", AOI: "analysis_extent"},
            reference={
                "crs": profile.crs,
                "transform": list(transform[:6]),
                "shape": [side, side],
                "bands": profile.bands,
                "dtype": profile.dtype,
                "compression": "none",
                "nodata": profile.nodata,
                "halo_pixels": halo,
                "analysis_extent": {
                    "layer": "analysis_extent",
                    "bounds": list(extent),
                    "shape": [inner_side, inner_side],
                    "transform": list(window_transform(analysis, transform)[:6]),
                },
                **sizes,
                "terrain_nodata_pixels": inner_side * inner_side - sizes["valid_terrain_pixels"],
                "slope_degrees": 45,
                "slope_percent": 100,
                "aspect_degrees": 270,
                "logical_pixel_sha256": logical_digest,
            },
        )


if __name__ == "__main__":
    raise SystemExit(run_cli(__doc__, generate))
