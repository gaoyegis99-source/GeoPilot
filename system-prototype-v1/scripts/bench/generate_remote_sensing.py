"""Generate and verify one tier of the native-grid optical change benchmark."""

import hashlib
import runpy
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import array_bounds, from_origin
from rasterio.windows import Window

from _raster_fixtures import (
    checked_grid, feed_pixels, manifest, new_output, open_tiff, raster_environment, run_cli, tile_indices, write_extent,
)
from validate_benchmark_manifest import (
    MANIFEST, ROOT, TIERS, Manifest, RemoteSensing, exact, read_yaml, remote_sizes, require,
)

sys.path.insert(0, str(ROOT / "src"))

from geopilot.remote_sensing.band_mapper import map_product_bands
from geopilot.remote_sensing.config_models import SensorProductConfig
from geopilot.remote_sensing.config_registry import load_sensor_registry
from geopilot.remote_sensing.product_metadata import LANDSAT_PROFILE, PRODUCT_METADATA_DOMAIN, SENTINEL_PROFILE


LANDSAT = "first-landsat.tif"
SENTINEL = "second-sentinel.tif"
NATIVE_SCL = "sentinel-scl-20m.tif"
AOI = "analysis-extent.gpkg"
LANDSAT_CODES = ("QA_PIXEL", "SR_B5", "QA_RADSAT", "SR_B4")
SENTINEL_CODES = ("SCL", "B08", "B04")
PHASES = ("increase", "decrease", "stable", "first_cloud", "second_cloud", "first_fill")
DELTA = (0.5, 0.5 - 11 / 17, 0.0)
THRESHOLD = 0.1


def _coarse_indexes(indices, pixel, profile):
    return (indices * pixel + pixel // 2 - profile.offset_m) // profile.landsat_pixel_m


def _write_landsat(path, grid, profile, template):
    xml = template["build_landsat_xml"]().replace("20241101", "20240701").replace("20241103", "20240703")
    with open_tiff(path, **grid) as dataset:
        dataset.descriptions = LANDSAT_CODES
        dataset.update_tags(ns=PRODUCT_METADATA_DOMAIN, PROFILE=LANDSAT_PROFILE, METADATA_XML=xml)
        for _, window in dataset.block_windows(1):
            rows, cols = tile_indices(window)
            phase = cols % profile.phase_columns
            tile = np.empty((4, len(rows), len(cols)), dtype=grid["dtype"])
            tile[0] = np.where(phase == 3, 1 << 3, 0)
            tile[1] = np.where(phase == 1, 20000, 10000)
            tile[2] = 0
            tile[3] = np.where(phase == 5, 0, 10000)
            dataset.write(tile, window=window)


def _write_native_scl(path, grid, profile):
    with open_tiff(path, **grid) as dataset:
        dataset.set_band_description(1, "SCL")
        for _, window in dataset.block_windows(1):
            rows, cols = tile_indices(window)
            phase = _coarse_indexes(cols, profile.scl_pixel_m, profile) % profile.phase_columns
            values = np.where(phase == 4, 9, 4).astype(grid["dtype"])
            dataset.write(np.broadcast_to(values, (len(rows), len(cols))), 1, window=window)


def _expand_scl(dataset, window):
    """Expand complete 20 m cells onto the co-origin, half-sized optical grid."""
    x, y, width, height = (int(getattr(window, attr)) for attr in ("col_off", "row_off", "width", "height"))
    require(all(value % 2 == 0 for value in (x, y, width, height)), "Packed SCL window is not aligned to the native grid")
    native = dataset.read(1, window=Window(x // 2, y // 2, width // 2, height // 2))
    return native.repeat(2, axis=0).repeat(2, axis=1)


def _write_sentinel(path, native_path, grid, profile, landsat_side, template):
    xml = template["build_sentinel_xml"]()
    with rasterio.open(native_path) as native, open_tiff(path, **grid) as dataset:
        dataset.descriptions = SENTINEL_CODES
        dataset.update_tags(
            ns=PRODUCT_METADATA_DOMAIN,
            PROFILE=SENTINEL_PROFILE, METADATA_XML=xml, GRANULE_ID=template["GRANULE"],
        )
        dataset.update_tags(1, ns=PRODUCT_METADATA_DOMAIN, RESAMPLING="nearest")
        for _, window in dataset.block_windows(1):
            rows, cols = tile_indices(window)
            coarse_rows = _coarse_indexes(rows, profile.sentinel_pixel_m, profile)
            coarse_cols = _coarse_indexes(cols, profile.sentinel_pixel_m, profile)
            phase = coarse_cols % profile.phase_columns
            tile = np.empty((3, len(rows), len(cols)), dtype=grid["dtype"])
            tile[0] = _expand_scl(native, window)
            tile[1] = np.where(phase == 2, 2250, 4750)
            tile[2] = 2250
            dataset.write(tile, window=window)
            valid_rows = (0 <= coarse_rows) & (coarse_rows < landsat_side)
            valid_cols = (0 <= coarse_cols) & (coarse_cols < landsat_side)
            dataset.write_mask(np.outer(valid_rows, valid_cols).astype("uint8") * 255, window=window)


def _native_phase(dataset, cols, first_transform, phases):
    centers = dataset.transform.c + dataset.transform.a * (cols + 0.5)
    return np.floor((centers - first_transform.c) / first_transform.a).astype("int64") % phases


def _verify_native(path, grid, first_transform, phases):
    digest = hashlib.sha256()
    with rasterio.open(path) as dataset:
        checked_grid(dataset, **grid)
        require(dataset.descriptions == ("SCL",), "Native 20 m quality band description differs")
        for _, window in dataset.block_windows(1):
            data = dataset.read(1, window=window)
            mask = dataset.read_masks(1, window=window)
            _, cols = tile_indices(window)
            expected = np.where(_native_phase(dataset, cols, first_transform, phases) == 4, 9, 4)
            require(np.all(data == expected) and np.all(mask == 255), "Native SCL cells or source mask differ")
            feed_pixels(digest, data, mask)
    return digest.hexdigest()


def _verify_products(landsat_path, sentinel_path, native_path, grids, profile, root):
    registry = load_sensor_registry(root / "config/sensors")
    first = map_product_bands(landsat_path, registry=registry)
    second = map_product_bands(sentinel_path, registry=registry)
    expected_first = SensorProductConfig.model_validate(read_yaml(root, profile.landsat_config))
    expected_second = SensorProductConfig.model_validate(read_yaml(root, profile.sentinel_config))
    require(
        (first.configuration, second.configuration) == (expected_first, expected_second)
        and {name: band.index for name, band in first.bands.items()} == {"quality": 1, "nir": 2, "saturation": 3, "red": 4}
        and {name: band.index for name, band in second.bands.items()} == {"quality": 1, "nir": 2, "red": 3}
        and second.bands["quality"].resampling == "nearest"
        and (second.bands["red"].band_id, second.bands["nir"].band_id) == (3, 7),
        "Reopened products differ from the exact V1 configurations or band order",
    )
    first_digest = hashlib.sha256()
    second_digest = hashlib.sha256()
    sentinel_valid = 0
    phase_counts = {phase: 0 for phase in PHASES}
    with rasterio.open(landsat_path) as source, rasterio.open(sentinel_path) as target, rasterio.open(native_path) as native:
        checked_grid(source, **grids["landsat"])
        checked_grid(target, **grids["sentinel"])
        require(source.descriptions == LANDSAT_CODES and target.descriptions == SENTINEL_CODES,
                "Reopened product band descriptions differ")
        for _, window in source.block_windows(1):
            data = source.read(window=window)
            masks = source.read_masks(window=window)
            require(np.all(masks == 255), "Landsat shared source mask must preserve raw QA zero")
            _verify_aligned_window(source, target, window, data, phase_counts, profile)
            feed_pixels(first_digest, data, masks)

        west, south, east, north = source.bounds
        for _, window in target.block_windows(1):
            data = target.read(window=window)
            masks = target.read_masks(window=window)
            rows, cols = tile_indices(window)
            centers_x = target.transform.c + target.transform.a * (cols + 0.5)
            centers_y = target.transform.f + target.transform.e * (rows + 0.5)
            coverage = np.outer((south < centers_y) & (centers_y < north), (west < centers_x) & (centers_x < east))
            phase = _native_phase(target, cols, source.transform, profile.phase_columns)
            require(
                np.array_equal(data[0], _expand_scl(native, window))
                and np.all(data[1] == np.where(phase == 2, 2250, 4750)) and np.all(data[2] == 2250)
                and np.all((masks != 0) == coverage),
                "Reopened Sentinel windows differ from native SCL, spectral phases or source coverage",
            )
            sentinel_valid += int(np.count_nonzero(coverage))
            feed_pixels(second_digest, data, masks)
        require(sentinel_valid == (source.width * 3) ** 2, "Sentinel source footprint does not cover the reference grid")

    products = {}
    for label, mapping, digest in (("landsat", first, first_digest), ("sentinel", second, second_digest)):
        products[label] = {
            "id": mapping.metadata.product_id,
            "product_key": mapping.metadata.key.model_dump(mode="json"),
            "configuration": {"id": mapping.configuration.id, "version": mapping.configuration.version},
            "bands": {name: {
                "index": band.index, "code": band.code, "native_resolution_m": band.native_resolution_m,
                "nodata_dn": band.nodata_dn, "band_id": band.band_id, "resampling": band.resampling,
            } for name, band in mapping.bands.items()},
            "logical_pixel_sha256": digest.hexdigest(),
        }
    return products, phase_counts


def _verify_aligned_window(source, target, window, data, counts, profile) -> None:
    """Check independent centre sampling, QA/fill and index differences for every AOI pixel."""
    rows, cols = tile_indices(window)
    height, width = len(rows), len(cols)
    phase = cols % profile.phase_columns
    qa, nir, saturation, red = data
    require(
        np.all(qa == np.where(phase == 3, 1 << 3, 0)) and np.all(saturation == 0)
        and np.all(nir == np.where(phase == 1, 20000, 10000)) and np.all(red == np.where(phase == 5, 0, 10000)),
        "Reopened Landsat raw DN and QA differ from the phase reference",
    )
    x, y = source.xy(int(rows[0]), int(cols[0]))
    start_row, start_col = target.index(x, y)
    factor = profile.landsat_pixel_m // profile.sentinel_pixel_m
    covering = Window(start_col, start_row, (width - 1) * factor + 1, (height - 1) * factor + 1)
    scl, nir2, red2 = target.read(window=covering)[:, ::factor, ::factor]
    source_mask = target.read_masks(1, window=covering)[::factor, ::factor]
    valid1 = ((qa & (1 << 3)) == 0) & (saturation == 0) & (red != 0) & (nir != 0)
    valid2 = (source_mask != 0) & (scl == 4) & (red2 != 0) & (nir2 != 0)
    valid = valid1 & valid2
    require(
        np.all(valid1 == ((phase != 3) & (phase != 5)))
        and np.all(valid2 == (phase != 4)) and np.all(valid == (phase < 3)),
        "Reopened QA, source masks and fill values differ on the aligned grid",
    )
    first_red, first_nir = red[valid] * 0.0000275 - 0.2, nir[valid] * 0.0000275 - 0.2
    second_red, second_nir = (red2[valid].astype("float64") - 1000) / 10000, (nir2[valid].astype("float64") - 1000) / 10000
    change = (second_nir - second_red) / (second_nir + second_red) - (first_nir - first_red) / (first_nir + first_red)
    valid_phases = np.broadcast_to(phase, valid.shape)[valid]
    require(np.allclose(change, np.asarray(DELTA)[valid_phases], rtol=0, atol=1e-12),
            "Computed independent aligned NDVI delta differs from the analytic phase reference")
    for index, name in enumerate(PHASES):
        counts[name] += height * int(np.count_nonzero(phase == index))


def generate(tier: str, output: Path, root: Path = ROOT) -> dict:
    specification = Manifest.model_validate(read_yaml(root, MANIFEST))
    profile = RemoteSensing.model_validate(read_yaml(root, specification.profiles.remote_sensing))
    exact(profile.tiers, TIERS, "remote_sensing.tiers")
    sizes = remote_sizes(root, profile)[tier]
    side = profile.tiers[tier].side_pixels
    sentinel_side = side * (profile.landsat_pixel_m // profile.sentinel_pixel_m) + 2
    east, north, offset = profile.origin_east_m, profile.origin_north_m, profile.offset_m
    first_grid = from_origin(east, north, profile.landsat_pixel_m, profile.landsat_pixel_m)
    second_grid = from_origin(east - offset, north + offset, profile.sentinel_pixel_m, profile.sentinel_pixel_m)
    native_grid = from_origin(east - offset, north + offset, profile.scl_pixel_m, profile.scl_pixel_m)
    shared = {"crs": profile.crs, "dtype": profile.dtype, "block": profile.block_pixels, "nodata": None}
    grids = {
        "landsat": {**shared, "side": side, "transform": first_grid, "bands": 4},
        "sentinel": {**shared, "side": sentinel_side, "transform": second_grid, "bands": 3},
        "native_scl": {**shared, "side": sentinel_side // 2, "transform": native_grid, "bands": 1},
    }
    bounds = array_bounds(side, side, first_grid)
    template = runpy.run_path(str(ROOT / "tests/remote_sensing/product_fixtures.py"))
    with new_output(output) as directory, raster_environment():
        _write_landsat(directory / LANDSAT, grids["landsat"], profile, template)
        _write_native_scl(directory / NATIVE_SCL, grids["native_scl"], profile)
        _write_sentinel(directory / SENTINEL, directory / NATIVE_SCL, grids["sentinel"], profile, side, template)
        logical_scl = _verify_native(directory / NATIVE_SCL, grids["native_scl"], first_grid, profile.phase_columns)
        products, counts = _verify_products(directory / LANDSAT, directory / SENTINEL, directory / NATIVE_SCL, grids, profile, root)
        require(counts == dict.fromkeys(PHASES, sizes["expected_pixels_per_phase"]), "Aligned phase counts differ")
        write_extent(directory / AOI, profile.crs, bounds)
        return manifest(
            directory, family="remote_sensing", tier=tier,
            roles={LANDSAT: "first_product", SENTINEL: "second_product", AOI: "analysis_extent", NATIVE_SCL: "native_quality_reference"},
            reference={
                "crs": profile.crs,
                "transform": list(first_grid[:6]),
                "compression": "none",
                "grids": {name: {
                    "shape": [grid["side"]] * 2, "transform": list(grid["transform"][:6]),
                    "dtype": grid["dtype"], "bands": grid["bands"], "nodata": grid["nodata"],
                } for name, grid in grids.items()},
                "analysis_extent": {"layer": "analysis_extent", "bounds": list(bounds)},
                "products": products,
                "native_scl_logical_pixel_sha256": logical_scl,
                **sizes,
                "resampling": "nearest",
                "change_threshold": THRESHOLD,
                "change_direction": "second_minus_first",
                "phase_counts": counts,
                "change_counts": {name: counts[name] for name in PHASES[:3]},
                "nodata_pixels": sum(counts[name] for name in PHASES[3:]),
                "ndvi_delta_by_valid_phase": dict(zip(PHASES[:3], DELTA, strict=True)),
                "sentinel_shared_mask": "optical_pixel_center_inside_first_product_footprint",
            },
        )


if __name__ == "__main__":
    raise SystemExit(run_cli(__doc__, generate))
