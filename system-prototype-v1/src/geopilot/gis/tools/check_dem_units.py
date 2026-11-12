"""Reopen the prepared DEM and audit its confirmed elevation-unit conversion."""

import json
import math
from dataclasses import asdict
from pathlib import Path

from geopilot.data.raster_reader import read_raster_metadata
from geopilot.gis.raster.units import resolve_dem_units
from geopilot.gis.tools.tool_command import run_tool_command


def check_dem_units(request: dict) -> dict:
    parameters = request["parameters"]
    metadata = read_raster_metadata(request["inputs"]["dem"]["path"])
    band = parameters["band"]
    selection = parameters["vertical_unit"]
    units = resolve_dem_units(metadata, band=band, vertical_unit=None if selection == "metadata" else selection)
    if not math.isclose(units.z_factor, parameters["z_factor"], rel_tol=1e-12):
        raise ValueError("The confirmed z-factor does not match the DEM units.")
    evidence = {"band": band, **asdict(units)}
    target = request["output_targets"]["unit_report"]
    with Path(target["path"]).open("x", encoding="utf-8") as output:
        json.dump(evidence, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
    print("measurement: " + json.dumps(evidence, ensure_ascii=False, allow_nan=False))
    return {"unit_report": target}


if __name__ == "__main__":
    raise SystemExit(run_tool_command(check_dem_units, description=__doc__))
