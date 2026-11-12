"""Reproject selected lines into the confirmed measurement CRS."""

from geopilot.gis.measurement.projection import reproject_vector
from geopilot.gis.tools.tool_command import run_tool_command


def prepare_lines(request: dict) -> dict:
    source = request["inputs"]["source"]
    target = request["output_targets"]["roads"]
    parameters = request["parameters"]
    result = reproject_vector(
        source["path"], target["path"], method=parameters["method"], target_crs=parameters["target_crs"],
        source_layer=source.get("layer"), output_layer=target.get("layer"),
    )
    print(f"Prepared {result.decision.crs} ({result.decision.unit})")
    return {"roads": target}


if __name__ == "__main__":
    raise SystemExit(run_tool_command(prepare_lines, description=__doc__))
