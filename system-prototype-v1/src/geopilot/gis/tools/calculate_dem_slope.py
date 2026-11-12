"""Compute Horn slope in degrees or percent from the prepared DEM."""

import json

import numpy as np
import rasterio

from geopilot.gis.raster.terrain import (
    HORN_ALGORITHM_VERSION, HORN_RADIUS_PIXELS, validate_terrain_unit_report, write_slope_raster,
)
from geopilot.gis.tools.tool_command import run_tool_command


def calculate_dem_slope(request: dict) -> dict:
    source = request["inputs"]["dem"]["path"]
    report = request["inputs"]["unit_report"]["path"]
    target = request["output_targets"]["terrain_raster"]
    parameters = request["parameters"]
    band, z_factor, slope_unit = parameters["band"], parameters["z_factor"], parameters["slope_unit"]
    validate_terrain_unit_report(source, report, band=band, z_factor=z_factor)
    write_slope_raster(source, target["path"], band=band, z_factor=z_factor, slope_unit=slope_unit)
    with rasterio.open(target["path"], driver="GTiff") as dataset:
        tags = dataset.tags()
    evidence = {
        "algorithm": "Horn", "algorithm_version": HORN_ALGORITHM_VERSION, "radius_pixels": HORN_RADIUS_PIXELS,
        "edge_interpolation": False, "source_band": band, "source_scale": float(tags["SOURCE_SCALE"]),
        "source_offset": float(tags["SOURCE_OFFSET"]), "z_factor": z_factor, "output_unit": slope_unit,
        "rasterio": rasterio.__version__, "gdal": rasterio.__gdal_version__, "numpy": np.__version__,
    }
    print("terrain: " + json.dumps(evidence, allow_nan=False))
    return {"terrain_raster": target}


if __name__ == "__main__":
    raise SystemExit(run_tool_command(calculate_dem_slope, description=__doc__))
