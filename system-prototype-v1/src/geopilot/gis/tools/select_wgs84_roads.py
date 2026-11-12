"""Select full source road features whose WGS84 geodesic arcs reach the AOI envelope."""

import json

import numpy as np
import shapely

from geopilot.gis.measurement.geodesic_buffer import validate_geodesic_bounds
from geopilot.gis.measurement.geodesic_road_scope import ROAD_STATION_SPACING_M, WGS84, road_intersects_influence
from geopilot.gis.tools.clip_points_to_extent import _read_vector
from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.gis.tools.wgs84_road_io import LINE_TYPES, POLYGON_TYPES, wgs84_frame, write_road_layers


def select_roads(request: dict) -> dict:
    if request["parameters"]["predicate"] != "intersects":
        raise ValueError("Geodesic road selection requires the intersects predicate")
    inputs = request["inputs"]
    area, _ = _read_vector(inputs["area"], role="inputs.area", kinds=POLYGON_TYPES)
    if area.empty or not area.crs.equals(WGS84, ignore_axis_order=True):
        raise ValueError("inputs.area requires a nonempty WGS84 influence envelope")
    influence = shapely.union_all(area.geometry.to_numpy())
    validate_geodesic_bounds(influence.bounds)
    shapely.prepare(influence)
    roads, geometry_type = _read_vector(inputs["source"], role="inputs.source", kinds=LINE_TYPES)
    geographic = wgs84_frame(roads, role="source", local=False)
    selected = np.zeros(len(roads), dtype=bool)
    for position, line in enumerate(geographic.geometry):
        try:
            selected[position] = road_intersects_influence(line, influence)
        except ValueError as error:
            raise ValueError(f"Road row {position + 1}: {error}") from error
    candidates = roads.loc[selected]
    outputs = request["output_targets"]
    write_road_layers(outputs, {"roads": (candidates, geometry_type)})
    print("geodesic_candidates: " + json.dumps({
        "road_count": len(roads), "selected_count": len(candidates), "road_station_spacing_m": ROAD_STATION_SPACING_M,
    }, allow_nan=False))
    return outputs


if __name__ == "__main__":
    raise SystemExit(run_tool_command(select_roads, description=__doc__))
