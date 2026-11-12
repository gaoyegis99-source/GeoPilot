"""Clip the expanded terrain to the original analysis polygon and pixel grid."""

import json
from pathlib import Path

from geopilot.domain.step_io import StepFileRef
from geopilot.gis.raster.clip_back import clip_terrain_to_analysis_extent
from geopilot.gis.tools.tool_command import run_tool_command


def clip_dem_back(request: dict) -> dict:
    inputs = request["inputs"]
    source = inputs["terrain_raster"]["path"]
    windows_path = inputs["windows"]["path"]
    windows = json.loads(Path(windows_path).read_text(encoding="utf-8"))
    extent = StepFileRef.model_validate(inputs["analysis_extent"])
    target = request["output_targets"]["terrain_raster"]
    metadata = clip_terrain_to_analysis_extent(source, target["path"], windows=windows, analysis_extent=extent)
    evidence = {
        "coverage": "pixel_center_covers", "resampling": "none", "analysis_bounds": metadata.bounds,
        "terrain_raster": target["path"], "source": source, "windows": windows_path,
    }
    print("terrain_clip: " + json.dumps(evidence, allow_nan=False))
    return {"terrain_raster": target}


if __name__ == "__main__":
    raise SystemExit(run_tool_command(clip_dem_back, description=__doc__))
