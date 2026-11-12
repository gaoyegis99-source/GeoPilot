"""Convert the complete candidate roads to the verified WGS84 buffer's input CRS."""

from geopilot.gis.measurement.geodesic_buffer import validate_geodesic_line
from geopilot.gis.measurement.geodesic_road_scope import WGS84
from geopilot.gis.tools.clip_points_to_extent import _read_vector
from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.gis.tools.wgs84_road_io import LINE_TYPES, wgs84_frame, write_road_layers


def prepare_lines(request: dict) -> dict:
    parameters = request["parameters"]
    if (parameters["method"], parameters["target_crs"]) != ("geodesic", WGS84.to_string()):
        raise ValueError("Geodesic road preparation requires EPSG:4326")
    source, geometry_type = _read_vector(request["inputs"]["source"], role="inputs.source", kinds=LINE_TYPES)
    prepared = wgs84_frame(source, role="source", local=True)
    for row, geometry in enumerate(prepared.geometry, start=1):
        try:
            validate_geodesic_line(geometry)
        except ValueError as error:
            raise ValueError(f"Road row {row}: {error}") from error
    outputs = request["output_targets"]
    write_road_layers(outputs, {"roads": (prepared, geometry_type)})
    print(f"Prepared {len(prepared)} road feature(s) in {WGS84.to_string()} for geodesic measurement")
    return outputs


if __name__ == "__main__":
    raise SystemExit(run_tool_command(prepare_lines, description=__doc__))
