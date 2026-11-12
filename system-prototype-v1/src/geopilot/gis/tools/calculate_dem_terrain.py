"""Run the selected slope or aspect calculation in the fixed DEM method step."""

from geopilot.gis.tools.calculate_dem_aspect import calculate_dem_aspect
from geopilot.gis.tools.calculate_dem_slope import calculate_dem_slope
from geopilot.gis.tools.tool_command import run_tool_command


_TERRAIN_CALCULATORS = {"slope": calculate_dem_slope, "aspect": calculate_dem_aspect}


def calculate_dem_terrain(request: dict) -> dict:
    return _TERRAIN_CALCULATORS[request["parameters"]["terrain_output"]](request)


if __name__ == "__main__":
    raise SystemExit(run_tool_command(calculate_dem_terrain, description=__doc__))
