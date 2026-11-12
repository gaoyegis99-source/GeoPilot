"""Select complete original targets using the requested spatial predicate."""

from shapely.errors import GEOSException

from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.gis.tools.vector_step_files import read_vector, write_vector_outputs
from geopilot.gis.vector.spatial_relations import select_intersecting, select_within


_SELECTORS = {"intersects": select_intersecting, "within": select_within}


def select_spatial_relation(request: dict) -> dict:
    relation = request["parameters"]["spatial_relation"]
    select = _SELECTORS.get(relation)
    if select is None:
        raise ValueError(f"Unsupported spatial_relation {relation!r}")

    inputs = request["inputs"]
    targets, geometry_type = read_vector(inputs["targets"], role="targets", require_source_id=True)
    area, _ = read_vector(inputs["area"], role="area")
    try:
        selected = select(targets, area)
    except GEOSException as error:
        raise ValueError(f"Could not evaluate {relation} for targets.geometry. {error}") from error
    outputs = request["output_targets"]
    write_vector_outputs(outputs, {"targets": (selected, geometry_type)})
    print(f"Selected {len(selected)} of {len(targets)} complete target(s) using {relation}")
    return outputs


if __name__ == "__main__":
    raise SystemExit(run_tool_command(select_spatial_relation, description=__doc__))
