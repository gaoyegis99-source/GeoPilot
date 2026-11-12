"""Derive DEM analysis and neighborhood windows from the selected extent layer."""

import json
from pathlib import Path

from geopilot.domain.step_io import StepFileRef
from geopilot.gis.raster.dem_prepare import plan_dem_windows
from geopilot.gis.tools.tool_command import run_tool_command


def expand_dem_extent(request: dict) -> dict:
    evidence = plan_dem_windows(
        request["inputs"]["dem"]["path"],
        StepFileRef.model_validate(request["inputs"]["analysis_extent"]),
        request["parameters"]["radius_pixels"],
    )
    target = request["output_targets"]["windows"]
    with Path(target["path"]).open("x", encoding="utf-8") as output:
        json.dump(evidence, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
    print("dem_windows: " + json.dumps(evidence, ensure_ascii=False, allow_nan=False))
    return {"windows": target}


if __name__ == "__main__":
    raise SystemExit(run_tool_command(expand_dem_extent, description=__doc__))
