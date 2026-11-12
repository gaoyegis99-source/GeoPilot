"""Publish the selected threshold classes and their equal-area statistics."""

import json
from pathlib import Path

import pyproj
import rasterio

from geopilot.gis.raster.area_stats import summarize_threshold_area
from geopilot.gis.tools.tool_command import run_tool_command


def summarize_dem_area(request: dict) -> dict:
    source = request["inputs"]["threshold_regions"]["path"]
    target = request["output_targets"]["area_statistics"]
    report = summarize_threshold_area(source, area_crs=request["parameters"]["area_crs"])
    document = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    with Path(target["path"]).open("x", encoding="utf-8") as output:
        output.write(document + "\n")
    evidence = {
        "threshold_regions": source, "area_statistics": target["path"],
        "source_crs": report["source_crs"], "area_crs": report["area_crs"], "area_unit": report["area_unit"],
        "cell_area_method": report["cell_area_method"], "resampling": report["resampling"],
        "selected_pixel_count": report["selected_pixel_count"], "total_area_m2": report["total_area_m2"],
        "pyproj": pyproj.__version__, "proj": pyproj.proj_version_str, "rasterio": rasterio.__version__,
    }
    print("raster_area: " + json.dumps(evidence, ensure_ascii=False, allow_nan=False))
    return {"area_statistics": target}


if __name__ == "__main__":
    raise SystemExit(run_tool_command(summarize_dem_area, description=__doc__))
