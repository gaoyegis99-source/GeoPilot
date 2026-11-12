"""Write the local WGS84 road line buffers as a GeoPackage layer."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import geopandas as gpd
import pyogrio
from pyproj import CRS

from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.vector_reader import local_ogr_path, read_layer_metadata
from geopilot.gis.measurement.geodesic_buffer import (
    MAX_CAP_ANGLE_DEG,
    MAX_DISTANCE_M,
    MAX_SEGMENT_STEP_M,
    MIN_DISTANCE_M,
    geodesic_buffer,
    merge_geodesic_buffers,
    validate_geodesic_bounds,
    validate_geodesic_buffer_input,
)
from geopilot.gis.measurement.projection import _layer_options
from geopilot.gis.tools.tool_command import run_tool_command


def buffer_roads(request: dict) -> dict:
    source = request["inputs"]["source"]
    path = local_ogr_path(source["path"])
    layer = source.get("layer")
    if path.suffix.lower() == ".gpkg":
        layer, metadata = read_geopackage_metadata(path, layer)
    else:
        metadata = read_layer_metadata(path, layer)
    decision = validate_geodesic_buffer_input(metadata)
    frame = pyogrio.read_dataframe(path, layer=layer)
    if (
        not isinstance(frame, gpd.GeoDataFrame) or frame.crs is None
        or not frame.crs.equals(CRS.from_user_input(decision.crs), ignore_axis_order=True)
        or len(frame) != metadata.feature_count
    ):
        raise ValueError("The reopened WGS84 road layer differs from its inspected metadata")

    parameters = request["parameters"]
    distance = parameters["distance"]
    dissolve = parameters["dissolve"]
    if type(distance) not in (int, float) or not MIN_DISTANCE_M <= distance <= MAX_DISTANCE_M:
        raise ValueError(f"Geodesic buffer distance must be between {MIN_DISTANCE_M:g} and {MAX_DISTANCE_M:g} metres")
    if type(dissolve) is not bool:
        raise ValueError("dissolve must be a Boolean")
    buffers = []
    for row, geometry in enumerate(frame.geometry, start=1):
        try:
            buffers.append(geodesic_buffer(geometry, distance))
        except ValueError as error:
            raise ValueError(f"Road row {row}: {error}") from error
    if dissolve:
        geometry = [merge_geodesic_buffers(buffers)] if buffers else []
        output = gpd.GeoDataFrame(geometry=geometry, crs=decision.crs)
    else:
        geometry = gpd.GeoSeries(buffers, index=frame.index, name=frame.geometry.name, crs=decision.crs)
        output = frame.set_geometry(geometry)
    if not output.empty:
        validate_geodesic_bounds(output.total_bounds)

    target = request["output_targets"]["buffer"]
    destination = Path(target["path"])
    if destination.suffix.lower() != ".gpkg":
        raise ValueError("The geodesic buffer output requires a .gpkg path")
    with TemporaryDirectory(prefix=".geodesic-buffer-", dir=destination.parent) as directory:
        staged = Path(directory) / "buffer.gpkg"
        pyogrio.write_dataframe(
            output, staged, driver="GPKG", layer=target.get("layer") or "buffer", layer_options=_layer_options(output),
            geometry_type="MultiPolygon" if output.empty else None,
        )
        destination.hardlink_to(staged)
    print(
        f"WGS84 geodesic buffer of {len(frame)} road feature(s), distance {distance:g} m, "
        f"station spacing ≤ {MAX_SEGMENT_STEP_M:g} m and cap increments ≤ {MAX_CAP_ANGLE_DEG:g}°"
    )
    print("geodesic_measurement: " + json.dumps({
        "algorithm": "wgs84_geodesic_line_buffer", "ellipsoid": "WGS84", "distance_m": distance,
        "dissolve": dissolve, "road_count": len(frame), "max_segment_step_m": MAX_SEGMENT_STEP_M,
        "max_cap_angle_deg": MAX_CAP_ANGLE_DEG,
    }, allow_nan=False))
    return {"buffer": target}


if __name__ == "__main__":
    raise SystemExit(run_tool_command(buffer_roads, description=__doc__))
