"""Apply confirmed threshold intervals to a clipped terrain raster."""

import json

from geopilot.gis.raster.threshold import BACKGROUND_CLASS, classify_raster
from geopilot.gis.tools.tool_command import run_tool_command


def classify_dem_threshold(request: dict) -> dict:
    source = request["inputs"]["terrain_raster"]["path"]
    target = request["output_targets"]["threshold_regions"]
    threshold = request["parameters"]["threshold"]
    metadata = classify_raster(source, target["path"], threshold=threshold)
    evidence = {
        "threshold": threshold, "resampling": "none", "pixel_size": metadata.resolution,
        "background_class": BACKGROUND_CLASS, "nodata": metadata.nodata[0], "crs": metadata.crs,
        "terrain_raster": source, "threshold_regions": target["path"],
    }
    print("threshold_classification: " + json.dumps(evidence, allow_nan=False))
    return {"threshold_regions": target}


if __name__ == "__main__":
    raise SystemExit(run_tool_command(classify_dem_threshold, description=__doc__))
