"""Clip already selected targets and keep originals for nonempty intersections."""

import shapely
from geopandas import GeoSeries
from shapely.errors import GEOSException

from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.gis.tools.vector_step_files import read_vector, write_vector_outputs
from geopilot.gis.vector.spatial_relations import _prepare_selection


def clip_vector_to_extent(request: dict) -> dict:
    inputs = request["inputs"]
    targets, geometry_type = read_vector(inputs["targets"], role="targets", require_source_id=True)
    area, _ = read_vector(inputs["area"], role="area")
    geometry, scope = _prepare_selection(targets, area)
    try:
        intersections = shapely.intersection(geometry, scope)
    except GEOSException as error:
        raise ValueError(f"Could not clip targets.geometry to area.geometry. {error}") from error
    retained = ~shapely.is_empty(intersections)
    originals = targets.iloc[retained].copy()
    clipped = originals.set_geometry(GeoSeries(
        intersections[retained], index=originals.index, name=originals.geometry.name, crs=originals.crs,
    ))

    outputs = request["output_targets"]
    # A nonempty contact can have lower dimension than its source feature.
    write_vector_outputs(outputs, {"targets": (clipped, "Unknown"), "original_targets": (originals, geometry_type)})
    print(f"Clipped {len(originals)} of {len(targets)} selected target(s) with nonempty intersections")
    return outputs


if __name__ == "__main__":
    raise SystemExit(run_tool_command(clip_vector_to_extent, description=__doc__))
