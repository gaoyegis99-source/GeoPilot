"""Calculate Horn downhill azimuth for a prepared local DEM."""

import json

from geopilot.gis.raster.aspect import calculate_aspect
from geopilot.gis.raster.terrain import HORN_ALGORITHM_VERSION, validate_terrain_unit_report
from geopilot.gis.tools.tool_command import run_tool_command


def calculate_dem_aspect(request: dict) -> dict:
    inputs = request["inputs"]
    parameters = request["parameters"]
    source = inputs["dem"]["path"]
    target = request["output_targets"]["terrain_raster"]
    band, z_factor = parameters["band"], parameters["z_factor"]
    validate_terrain_unit_report(source, inputs["unit_report"]["path"], band=band, z_factor=z_factor)
    calculate_aspect(source, target["path"], band=band, z_factor=z_factor)
    evidence = {
        "algorithm": "Horn", "algorithm_version": HORN_ALGORITHM_VERSION, "terrain_output": "aspect", "z_factor": z_factor,
        "unit": "degree", "compute_edges": False, "direction": "clockwise_from_grid_north", "flat": "nodata",
    }
    print("terrain: " + json.dumps(evidence, ensure_ascii=False, allow_nan=False))
    return {"terrain_raster": target}


if __name__ == "__main__":
    raise SystemExit(run_tool_command(calculate_dem_aspect, description=__doc__))
