"""Verify local Python GIS drivers by reopening synthetic GPKG and GeoTIFF files."""

import argparse
import logging
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import rasterio
from geopandas.testing import assert_geodataframe_equal
from rasterio.crs import CRS
from rasterio.transform import from_origin


DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "artifacts/smoke/python-gis"
EPSG = 32650
LAYER = "checkpoints"
DESCRIPTIONS = ("elevation", "uncertainty")
UNITS = ("m", "m")


def check_geopackage(path, expected, log):
    layers = pyogrio.list_layers(path).tolist()
    if layers != [[LAYER, "Point"]]:
        raise ValueError(f"{path.name}: unexpected layers {layers!r}")

    info = pyogrio.read_info(path, layer=LAYER)
    if info["driver"] != "GPKG" or info["fields"].tolist() != ["sample_id", "weight"]:
        raise ValueError(f"{path.name}: unexpected driver or field schema")

    actual = pyogrio.read_dataframe(path, layer=LAYER).sort_values("sample_id")
    assert_geodataframe_equal(actual.reset_index(drop=True), expected, check_geom_type=True)
    log.info("Reopened %s: EPSG:%s, layer=%s, fields=%s, features=%d",
             path.name, actual.crs.to_epsg(), LAYER, info["fields"].tolist(), len(actual))
    return actual


def write_geotiff(path, profile, pixels):
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(pixels)
        for band, (description, unit) in enumerate(zip(DESCRIPTIONS, UNITS, strict=True), 1):
            dataset.set_band_description(band, description)
            dataset.set_band_unit(band, unit)


def check_geotiff(path, expected_profile, expected_pixels, log):
    with rasterio.open(path) as dataset:
        profile = dataset.profile.copy()
        for key, value in expected_profile.items():
            if profile[key] != value:
                raise ValueError(f"{path.name}: expected {key}={value!r}, got {profile[key]!r}")
        if dataset.descriptions != DESCRIPTIONS or dataset.units != UNITS:
            raise ValueError(f"{path.name}: unexpected band descriptions or units")

        pixels = dataset.read()
        expected_masks = np.where(expected_pixels == expected_profile["nodata"], 0, 255)
        np.testing.assert_array_equal(dataset.read_masks(), expected_masks)
        np.testing.assert_array_equal(pixels, expected_pixels)
        log.info("Reopened %s: EPSG:%s, size=%dx%d, bands=%d, transform=%s, "
                 "NoData=%s, descriptions=%s, units=%s, pixels=%s",
                 path.name, dataset.crs.to_epsg(), dataset.width, dataset.height,
                 dataset.count, tuple(dataset.transform)[:6], dataset.nodata,
                 dataset.descriptions, dataset.units, pixels.tolist())
    return profile, pixels


def verify_round_trip(output_dir, log):
    log.info("Python %s (%s); GeoPandas %s; Pyogrio %s (GDAL %s); Rasterio %s (GDAL %s)",
             sys.version.split()[0], sys.executable, gpd.__version__, pyogrio.__version__,
             pyogrio.__gdal_version__, rasterio.__version__, rasterio.__gdal_version__)
    log.info("Source: two synthetic points and a synthetic two-band grid, EPSG:%s in metres", EPSG)

    vector_input = output_dir / "input.gpkg"
    vector_output = output_dir / "output.gpkg"
    points = gpd.GeoDataFrame(
        {"sample_id": ["P1", "P2"], "weight": [2, 5]},
        geometry=gpd.points_from_xy([500010, 500050], [3200030, 3200010]),
        crs=f"EPSG:{EPSG}",
    )
    pyogrio.write_dataframe(points, vector_input, driver="GPKG", layer=LAYER)
    reopened_points = check_geopackage(vector_input, points, log)
    pyogrio.write_dataframe(reopened_points, vector_output, driver="GPKG", layer=LAYER)
    check_geopackage(vector_output, points, log)

    raster_input = output_dir / "input.tif"
    raster_output = output_dir / "output.tif"
    pixels = np.array(
        [
            [[100, 102, 104], [106, -9999, 110]],
            [[1, 2, -9999], [4, 5, 6]],
        ],
        dtype="int16",
    )
    profile = {
        "driver": "GTiff",
        "dtype": "int16",
        "nodata": -9999,
        "width": 3,
        "height": 2,
        "count": 2,
        "crs": CRS.from_epsg(EPSG),
        "transform": from_origin(500000, 3200040, 20, 20),
    }
    write_geotiff(raster_input, profile, pixels)
    reopened_profile, reopened_pixels = check_geotiff(raster_input, profile, pixels, log)
    write_geotiff(raster_output, reopened_profile, reopened_pixels)
    check_geotiff(raster_output, profile, pixels, log)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="A new directory for the sample files and log")
    args = parser.parse_args()

    try:
        if args.output_dir is None:
            DEFAULT_ROOT.mkdir(parents=True, exist_ok=True)
            output_dir = Path(tempfile.mkdtemp(prefix="run-", dir=DEFAULT_ROOT))
        else:
            output_dir = args.output_dir.absolute()
            output_dir.mkdir(parents=True)
    except OSError as error:
        parser.error(f"cannot create output directory: {error}")

    log_path = output_dir / "smoke.log"
    with log_path.open("x", encoding="utf-8") as stream:
        log = logging.Logger("python-gis-smoke", level=logging.INFO)
        log.addHandler(logging.StreamHandler(stream))
        try:
            verify_round_trip(output_dir, log)
        except Exception as error:
            log.exception("FAIL: Python GIS round trip")
            print(f"FAIL: {error}. See {log_path}", file=sys.stderr)
            return 1
        log.info("PASS: Python GIS round trip")
    print(f"PASS: Python GIS round trip in {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
