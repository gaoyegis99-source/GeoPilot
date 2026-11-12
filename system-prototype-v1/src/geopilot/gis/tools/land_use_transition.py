"""Write land-use allocation, retained polygons and transition matrices."""

import json
from pathlib import Path

from geopandas import GeoDataFrame
from pyproj import CRS
from shapely.errors import GEOSException

from geopilot.execution.output_staging import temporary_output_directory
from geopilot.gis.measurement.projection import reproject_vector
from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.gis.tools.vector_step_files import read_vector, write_vector_outputs
from geopilot.gis.vector.transition_matrix import (
    allocate_transition_areas,
    build_change_polygons,
    build_transition_reports,
)
from geopilot.methods.land_use_overlay import _stage_outputs


def _read_vector(request: dict, role: str) -> GeoDataFrame:
    frame, _ = read_vector(request["inputs"][role], role=role, require_source_id=True)
    return frame


def _read_json(request: dict, role: str) -> dict:
    path = Path(request["inputs"][role]["path"])
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(outputs: dict, role: str, document: dict) -> None:
    reference = outputs[role]
    path = Path(reference["path"])
    if path.suffix.lower() != ".json" or reference.get("layer") is not None:
        raise ValueError(f"{role} requires a .json path without a layer")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _read_overlay(request: dict) -> GeoDataFrame:
    parameters = request["parameters"]
    overlay = _read_vector(request, "overlay_polygons")
    if parameters["area_unit"] != "square_meter":
        raise ValueError("Transition analysis requires area_unit square_meter")
    if not overlay.crs.equals(CRS.from_user_input(parameters["area_crs"]), ignore_axis_order=True):
        raise ValueError("The overlay layer must use the confirmed equal-area CRS")
    return overlay


def _read_zones(request: dict, area_crs: CRS) -> GeoDataFrame:
    zones = _read_vector(request, "zones")
    if zones.crs.equals(area_crs, ignore_axis_order=True):
        return zones
    if zones.empty:
        return zones.to_crs(area_crs)
    reference = request["inputs"]["zones"]
    target = Path(request["output_targets"]["allocations"]["path"])
    with temporary_output_directory(target, prefix=".land-use-zones-") as directory:
        projected = reproject_vector(
            reference["path"], Path(directory) / "zones.gpkg", source_layer=reference.get("layer"),
            output_layer="zones", method="equal_area", target_crs=area_crs,
        )
        print(f"Projected zones to {projected.decision.crs} ({projected.decision.unit})")
        frame, _ = read_vector({"path": projected.path, "layer": projected.layer}, role="zones")
        return frame


def _assign(request: dict) -> dict:
    overlay = _read_overlay(request)
    first = _read_vector(request, "first_classes")
    second = _read_vector(request, "second_classes")
    zones = _read_zones(request, overlay.crs)
    parameters = request["parameters"]
    allocations = allocate_transition_areas(
        overlay, first, second, zones, boundary_assignment=parameters["boundary_assignment"],
        boundary_tie=parameters["boundary_tie"], area_tolerance_m2=parameters["area_tolerance"],
    )
    outputs = request["output_targets"]
    with _stage_outputs(outputs) as temporary:
        _write_json(temporary, "allocations", allocations)
    print(f"Allocated {len(overlay)} fragments with {allocations['boundary_assignment']}")
    return outputs


def _build(request: dict) -> dict:
    overlay = _read_overlay(request)
    parameters = request["parameters"]
    changes, report = build_change_polygons(
        overlay, minimum_change_area_m2=parameters["minimum_change_area"],
        merge_adjacent_classes=parameters["merge_adjacent_classes"], area_tolerance_m2=parameters["area_tolerance"],
    )
    outputs = request["output_targets"]
    with _stage_outputs(outputs) as temporary:
        write_vector_outputs(temporary, {"change_polygons": (changes, "MultiPolygon")})
        _write_json(temporary, "change_report", report)
    print(f"Retained {report['retained_patch_count']} patches; filtered {report['filtered_area_m2']} square metres")
    return outputs


def _summarize(request: dict) -> dict:
    overlay = _read_overlay(request)
    changes = _read_vector(request, "change_polygons")
    matrix, zones = build_transition_reports(
        overlay, changes, _read_json(request, "allocations"), _read_json(request, "change_report"),
        area_tolerance_m2=request["parameters"]["area_tolerance"],
    )
    outputs = request["output_targets"]
    with _stage_outputs(outputs) as temporary:
        _write_json(temporary, "transition_matrix", matrix)
        _write_json(temporary, "zone_statistics", zones)
    print(f"Reconciled {matrix['total_area_m2']} square metres across {len(matrix['classes'])} classes")
    return outputs


def land_use_transition(request: dict) -> dict:
    """Dispatch the exact version of a registered land-use transition operation."""
    operations = {
        "python-land-use-assign": _assign,
        "python-land-use-build-changes": _build,
        "python-land-use-transition-matrix": _summarize,
    }
    tool = request["tool"]
    operation = operations.get(tool["id"])
    if tool["version"] != 1 or operation is None:
        raise ValueError(f"Unknown land use transition tool version {tool!r}")
    try:
        return operation(request)
    except GEOSException as error:
        raise ValueError(f"Transition geometry operation failed. {error}") from error


if __name__ == "__main__":
    raise SystemExit(run_tool_command(land_use_transition, description=__doc__))
