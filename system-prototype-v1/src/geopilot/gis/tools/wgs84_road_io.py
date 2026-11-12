"""Read and publish the vector layers used by the WGS84 road workflow."""

from pathlib import Path

import geopandas as gpd
import pyogrio

from geopilot.gis.measurement.geodesic_buffer import validate_geodesic_bounds
from geopilot.gis.measurement.geodesic_road_scope import WGS84, prepare_wgs84_geometry
from geopilot.gis.measurement.projection import _layer_options
from geopilot.methods.land_use_overlay import _stage_outputs


LINE_TYPES = {"LineString", "MultiLineString"}
POINT_TYPES = {"Point", "MultiPoint"}
POLYGON_TYPES = {"Polygon", "MultiPolygon"}


def wgs84_frame(frame: gpd.GeoDataFrame, *, role: str, local: bool) -> gpd.GeoDataFrame:
    """Preserve feature attributes and prepare the layer's WGS84 geometry."""
    try:
        geometries = prepare_wgs84_geometry(frame.geometry.to_numpy(), frame.crs)
        geometry = gpd.GeoSeries(geometries, index=frame.index, name=frame.geometry.name, crs=WGS84)
        if local and not geometry.empty:
            validate_geodesic_bounds(geometry.total_bounds)
    except ValueError as error:
        raise ValueError(f"inputs.{role}: {error}") from error
    return frame.set_geometry(geometry)


def write_road_layers(outputs: dict, frames: dict[str, tuple[gpd.GeoDataFrame, str]]) -> None:
    """Publish named layers together and retain the geometry type of empty outputs."""
    with _stage_outputs(outputs) as temporary:
        for role, reference in temporary.items():
            path = Path(reference["path"])
            if path.suffix.lower() != ".gpkg":
                raise ValueError(f"outputs.{role} requires a .gpkg path")
            frame, declared_type = frames[role]
            pyogrio.write_dataframe(
                frame, path, layer=reference.get("layer") or role, driver="GPKG",
                geometry_type=declared_type if frame.empty else None, layer_options=_layer_options(frame),
            )
