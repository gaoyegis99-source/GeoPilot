"""Prepare common WGS84 layers and bound the AOI's geodesic influence."""

import json

import geopandas as gpd
import shapely

from geopilot.gis.measurement.geodesic_road_scope import (
    CANDIDATE_PADDING_M, ROAD_STATION_SPACING_M, WGS84, expand_wgs84_analysis_extent,
)
from geopilot.gis.tools.clip_points_to_extent import _read_vector
from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.gis.tools.wgs84_road_io import POINT_TYPES, POLYGON_TYPES, wgs84_frame, write_road_layers


def expand_analysis_extent(request: dict) -> dict:
    parameters = request["parameters"]
    if parameters["target_crs"] != WGS84.to_string():
        raise ValueError("Geodesic influence scope requires EPSG:4326")
    inputs = request["inputs"]
    frames = {}
    for source, output, kinds in (
        ("source", "analysis_extent", POLYGON_TYPES), ("targets", "targets", POINT_TYPES),
        ("zones", "zones", POLYGON_TYPES),
    ):
        frame, geometry_type = _read_vector(inputs[source], role=f"inputs.{source}", kinds=kinds)
        frames[output] = wgs84_frame(frame, role=source, local=True), geometry_type

    distance = parameters["distance"]
    analysis = frames["analysis_extent"][0]
    if analysis.empty:
        raise ValueError("inputs.source requires a nonempty analysis area")
    combined = shapely.union_all(analysis.geometry.to_numpy())
    expanded = expand_wgs84_analysis_extent(combined, distance)
    frames["expanded_extent"] = gpd.GeoDataFrame(geometry=[expanded], crs=WGS84), "MultiPolygon"
    outputs = request["output_targets"]
    write_road_layers(outputs, frames)
    print("geodesic_measurement: " + json.dumps({
        "algorithm": "wgs84_ellipsoid_influence_envelope", "ellipsoid": "WGS84", "distance_m": distance,
        "candidate_padding_m": CANDIDATE_PADDING_M, "road_station_spacing_m": ROAD_STATION_SPACING_M,
    }, allow_nan=False))
    return outputs


if __name__ == "__main__":
    raise SystemExit(run_tool_command(expand_analysis_extent, description=__doc__))
