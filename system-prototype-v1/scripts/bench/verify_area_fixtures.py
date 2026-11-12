"""Verify the portable classifications retained from P13-04 timeouts."""

import argparse
import json
import math
import sys
from functools import partial
from pathlib import Path

import rasterio
from rasterio.transform import Affine


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from _raster_reference import Grid, aligned_values, area_reference, dem_values, require, sha256, source_file, verify_raster


BUNDLE = ROOT / "benchmarks/raster/area-fixtures"
CASES = {"dem-m": ("raster", "M", 2048), "dem-l": ("raster", "L", 8192), "remote-l": ("remote_sensing", "L", 3072)}
AREA_RULE = {"crs": "EPSG:6933", "relative_tolerance": 2e-7, "absolute_tolerance_m2": 0.02}


def verify_fixture(directory, entry):
    identifier = entry["id"]
    family, tier, side = CASES[identifier]
    require((entry["family"], entry["tier"], entry["path"]) == (family, tier, f"{identifier}.tif"),
            f"{identifier} recipe or filename differs")
    source = entry["source"]
    require((source["execution_status"], source["check_status"], source["error_code"], source["process_exit_code"]) ==
            ("FAILED", "NOT_CHECKED", "PROCESS_TIMEOUT", -15), f"{identifier} provenance is not a P13-04 timeout")
    path = source_file(directory, entry["path"])
    digest = sha256(path)
    require((path.stat().st_size, digest) == (entry["bytes"], entry["sha256"]), f"{identifier} size or SHA-256 differs")

    if family == "raster":
        period = 128
        reference = Grid("EPSG:32650", (side, side), Affine(10, 0, 500000, 0, -10, 4500000))
        grid = Grid(reference.crs, (side - 2, side - 2), reference.affine * Affine.translation(1, 1))
        masked = 9 * (side // period) ** 2
        counts = {"7": math.prod(grid.shape) - masked}
        expected = partial(dem_values, period=period, side=side, offset=1, terrain=7)
    else:
        period = None
        reference = grid = Grid("EPSG:32632", (side, side), Affine(30, 0, 500000, 0, -30, 5100000))
        masked = side**2 // 2
        counts = dict.fromkeys(("1", "2", "3"), side**2 // 6)
        expected = partial(aligned_values, stage="classes", landsat_side=side, offset=7,
                           first_pixel=30, second_pixel=10, scl_pixel=20)

    expected_grid = {
        "crs": grid.crs, "shape": list(grid.shape), "transform": list(grid.affine[:6]),
        "resolution_m": [grid.affine.a, -grid.affine.e], "block_shapes": [[256, 256]],
        "dtypes": ["uint8"], "nodata": [255],
    }
    require(entry["grid"] == expected_grid and entry["valid_class_counts"] == counts and entry["masked_pixels"] == masked,
            f"{identifier} declared grid or class counts differ from the recipe")
    with rasterio.Env(GDAL_NUM_THREADS="1", GDAL_CACHEMAX=32 * 1024**2, GDAL_PAM_ENABLED=False):
        actual = verify_raster(path, grid, dtypes=["uint8"], nodata=[255], expected=expected)
    require(actual["block_shapes"] == expected_grid["block_shapes"] and
            actual["valid_pixels_by_band"] == [sum(counts.values())] and actual["masked_pixels_by_band"] == [masked],
            f"{identifier} blocks or pixel counts differ")
    areas = {str(code): area for code, area in area_reference(reference, period).items()}
    require(areas.keys() == entry["expected_area_m2"].keys() and all(
        math.isclose(area, entry["expected_area_m2"][code], rel_tol=AREA_RULE["relative_tolerance"],
                     abs_tol=AREA_RULE["absolute_tolerance_m2"]) for code, area in areas.items()
    ), f"{identifier} independent areas differ")
    require(sha256(source_file(directory, entry["path"])) == digest, f"{identifier} changed while checking")
    return {"id": identifier, "sha256": digest, "bytes": actual["file_bytes"], "valid_class_counts": counts,
            "masked_pixels": masked, "maximum_absolute_pixel_error": actual["maximum_absolute_error"],
            "area_m2": areas}


def verify_bundle(directory):
    manifest = json.loads(source_file(directory, "manifest.json").read_text(encoding="utf-8"))
    entries = manifest["fixtures"]
    require((manifest["schema_version"], manifest["status"], manifest["area_reference"]) ==
            (1, "unpublished_intermediate", AREA_RULE), "Unexpected area fixture manifest or reference rule")
    require(len(entries) == len(CASES) and {entry["id"] for entry in entries} == CASES.keys(), "Fixture set differs")
    return {"status": "PASS", "area_crs": AREA_RULE["crs"],
            "fixtures": [verify_fixture(directory, entry) for entry in entries]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=BUNDLE, help="Folder containing the manifest and three GeoTIFFs")
    args = parser.parse_args()
    try:
        report = verify_bundle(args.directory)
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(1, f"Area fixture verification failed: {error}\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
