"""Prepare two classified periods and intersect them in a confirmed equal-area CRS."""

import json
import math
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from numbers import Integral
from pathlib import Path

import numpy as np
import shapely
from geopandas import GeoDataFrame
from pyproj import CRS
from shapely.errors import GEOSException
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from geopilot.checks.input_checks import InputCheckResult, require_input_checks
from geopilot.domain.data_asset import VectorMetadata
from geopilot.execution.output_staging import temporary_output_directory
from geopilot.execution.step_support import PreparedStep, resolve_step_path
from geopilot.gis.measurement.area import validate_area
from geopilot.gis.measurement.projection import reproject_vector
from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.gis.tools.vector_step_files import read_vector, write_vector_outputs


_PERIODS = {"first_classes": "first", "second_classes": "second"}
_EXTENT = "analysis_extent"
_SOURCE_ID = "source_feature_id"
_MAP_TOOL = "python-land-use-map"
_IMPLEMENTATION = "geopilot.land_use_overlay"
_MAPPED_COLUMNS = (_SOURCE_ID, "period", "source_class_code", "class_code")


class LandUseInputError(ValueError):
    """A classified input or mapping that the user can correct."""

    def __init__(self, message: str, *, field: str, observed: object = None) -> None:
        super().__init__(message)
        self.field = field
        self.observed = observed


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _codes(frame: GeoDataFrame, column: str, role: str) -> list[str]:
    field = f"inputs.{role}.fields.{column}"
    if column == frame.geometry.name or column not in frame:
        raise LandUseInputError(f"{field} is missing", field=field)
    tokens = []
    for position, value in enumerate(frame[column].tolist()):
        if isinstance(value, Integral) and not isinstance(value, bool):
            tokens.append(str(value))
        elif _text(value):
            tokens.append(value)
        else:
            raise LandUseInputError(
                f"{field} requires nonempty text or integer values, invalid at row {position + 1}",
                field=field, observed={"row": position + 1},
            )
    return tokens


def _read_polygon(reference: dict, role: str) -> tuple[GeoDataFrame, str]:
    try:
        frame, geometry_type = read_vector(reference, role=role)
    except ValueError as error:
        raise LandUseInputError(str(error), field=f"inputs.{role}") from error
    geometry = frame.geometry.to_numpy()
    if (
        frame.empty or not frame.geom_type.isin({"Polygon", "MultiPolygon"}).all()
        or not shapely.is_valid(geometry).all() or shapely.is_empty(geometry).any()
        or not np.isfinite(shapely.get_coordinates(geometry)).all()
    ):
        field = f"inputs.{role}.geometry"
        raise LandUseInputError(f"{field} requires finite, nonempty, valid polygons", field=field)
    if role in _PERIODS:
        ids = _codes(frame, _SOURCE_ID, role)
        if len(set(ids)) != len(ids):
            field = f"inputs.{role}.fields.{_SOURCE_ID}"
            raise LandUseInputError(f"{field} must be unique", field=field)
    return frame, geometry_type


def _read_periods(request: dict) -> dict:
    layers = {role: _read_polygon(request["inputs"][role], role) for role in _PERIODS}
    outputs = request["output_targets"]
    write_vector_outputs(outputs, layers)
    return outputs


def _mapped_layers(inputs: dict, parameters: dict) -> dict[str, tuple[GeoDataFrame, str]]:
    layers = {}
    missing = {}
    for role, prefix in _PERIODS.items():
        frame, geometry_type = _read_polygon(inputs[role], role)
        column = parameters[f"{prefix}_class_field"]
        codes = _codes(frame, column, role)
        mapping = parameters[f"{prefix}_class_mapping"]
        mapping_field = f"parameters.{prefix}_class_mapping"
        if not isinstance(mapping, dict) or any(not (_text(key) and _text(value)) for key, value in mapping.items()):
            raise LandUseInputError(
                f"{mapping_field} requires a JSON object mapping source codes to nonempty class names",
                field=mapping_field,
            )
        period = parameters[f"{prefix}_period"]
        if not _text(period):
            field = f"parameters.{prefix}_period"
            raise LandUseInputError(f"{field} requires a nonempty time label", field=field)
        unmapped = sorted(set(codes) - mapping.keys())
        if unmapped:
            missing[role] = unmapped
        layers[role] = GeoDataFrame(
            {
                _SOURCE_ID: _codes(frame, _SOURCE_ID, role),
                "period": period,
                "source_class_code": codes,
                "class_code": [mapping.get(code) for code in codes],
            },
            geometry=frame.geometry,
            crs=frame.crs,
        ), geometry_type
    if missing:
        field = f"parameters.{_PERIODS[next(iter(missing))]}_class_mapping"
        raise LandUseInputError(
            f"{field} has unmapped source codes. {json.dumps(missing, ensure_ascii=False)}",
            field=field, observed=missing,
        )
    if parameters["first_period"] == parameters["second_period"]:
        raise LandUseInputError(
            "Provide distinct labels for the two source periods", field="parameters.second_period",
            observed=parameters["second_period"],
        )
    return layers


def preflight_land_use_step(step: PreparedStep) -> None:
    """Report missing codes from both actual layers before starting the mapping worker."""
    contract = step.contract
    if (contract.id, contract.version, contract.runner, contract.implementation) != (
        _MAP_TOOL, 1, "python", _IMPLEMENTATION,
    ):
        return
    inputs = {
        role: {**ref, "path": resolve_step_path(step.workdir, ref["path"], field=f"inputs.{role}.path")}
        for role, ref in step.request["inputs"].items()
    }
    expected = "All codes in both source periods have explicit class mappings"
    try:
        layers = _mapped_layers(inputs, step.request["parameters"])
    except LandUseInputError as error:
        field, observed, message = error.field, error.observed, str(error)
        passed = False
    else:
        field = "parameters"
        observed = {role: sorted(set(frame["source_class_code"])) for role, (frame, _) in layers.items()}
        message = expected
        passed = True
    result = InputCheckResult(
        rule="classification_mapping_complete", field=field, severity="blocking",
        passed=passed, observed=observed, expected=expected, message=message,
    )
    with step.log_path.open("a", encoding="utf-8") as log:
        log.write(f"input_check: {result.model_dump_json()}\n")
    require_input_checks((result,))


def _map_periods(request: dict) -> dict:
    layers = _mapped_layers(request["inputs"], request["parameters"])
    outputs = request["output_targets"]
    write_vector_outputs(outputs, layers)
    return outputs


@contextmanager
def _stage_outputs(outputs: dict) -> Iterator[dict]:
    """Publish a group of completed files without replacing an existing target."""
    with ExitStack() as staging:
        temporary = {}
        for role, reference in outputs.items():
            target = Path(reference["path"])
            directory = staging.enter_context(temporary_output_directory(target, prefix=".land-use-"))
            temporary[role] = {**reference, "path": str(Path(directory) / target.name)}
        yield temporary
        with ExitStack() as rollback:
            for role, reference in outputs.items():
                target = Path(reference["path"])
                os.link(temporary[role]["path"], target)
                rollback.callback(target.unlink)
            rollback.pop_all()


def _project_periods(request: dict) -> dict:
    outputs = request["output_targets"]
    with _stage_outputs(outputs) as temporary:
        for role, source in request["inputs"].items():
            target = temporary[role]
            result = reproject_vector(
                source["path"], target["path"], source_layer=source.get("layer"),
                output_layer=target.get("layer") or role, method="equal_area",
                target_crs=request["parameters"]["target_crs"],
            )
            print(f"Projected {role} to {result.decision.crs} ({result.decision.unit})")
    return outputs


def _overlay_inputs(inputs: dict, area_crs: CRS) -> dict[str, GeoDataFrame]:
    frames = {}
    for role, reference in inputs.items():
        frame, geometry_type = _read_polygon(reference, role)
        metadata = VectorMetadata(
            format="GeoDataFrame", crs=frame.crs.to_string(),
            bounds=tuple(float(value) for value in frame.total_bounds),
            geometry_type=geometry_type, feature_count=len(frame), fields=(),
        )
        field = f"inputs.{role}.crs"
        validate_area(metadata, field=field)
        if not area_crs.equals(frame.crs, ignore_axis_order=True):
            raise ValueError(f"{field} must match the confirmed equal-area CRS {area_crs.to_string()}")
        if role in _PERIODS:
            frame = frame.assign(**{column: _codes(frame, column, role) for column in _MAPPED_COLUMNS})
        frames[role] = frame
    return frames


def _polygon_parts(geometry: BaseGeometry) -> Iterator[Polygon]:
    if geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
    elif geometry.geom_type in {"MultiPolygon", "GeometryCollection"}:
        for part in geometry.geoms:
            yield from _polygon_parts(part)


def _clipped_periods(frames: dict[str, GeoDataFrame], scope: BaseGeometry) -> tuple[dict, dict, dict]:
    clipped, union, evidence = {}, {}, {}
    for role in _PERIODS:
        geometries = shapely.force_2d(frames[role].geometry.to_numpy())
        clipped[role] = shapely.intersection(geometries, scope)
        union[role] = shapely.union_all(clipped[role])
        area_sum = math.fsum(shapely.area(clipped[role]))
        covered = union[role].area
        evidence[role] = {
            "clipped_area_sum_m2": area_sum,
            "covered_area_m2": covered,
            "overlap_area_m2": max(0.0, area_sum - covered),
            "uncovered_area_m2": scope.difference(union[role]).area,
        }
    return clipped, union, evidence


def _intersections(frames: dict[str, GeoDataFrame], clipped: dict) -> list[dict]:
    first, second = _PERIODS
    first_frame, second_frame = frames[first], frames[second]
    left, right = clipped[first], clipped[second]
    first_ids, second_ids = first_frame[_SOURCE_ID].tolist(), second_frame[_SOURCE_ID].tolist()
    index_pairs = shapely.STRtree(right).query(left, predicate="intersects")
    pairs = sorted(zip(*index_pairs), key=lambda pair: (first_ids[pair[0]], second_ids[pair[1]]))
    rows = []
    for i, j in pairs:
        start, end = first_frame.iloc[i], second_frame.iloc[j]
        parts = sorted(_polygon_parts(left[i].intersection(right[j])), key=lambda part: part.normalize().wkb)
        for polygon in parts:
            rows.append({
                _SOURCE_ID: f"change-{len(rows) + 1:06d}",
                "first_source_feature_id": start[_SOURCE_ID], "second_source_feature_id": end[_SOURCE_ID],
                "first_period": start["period"], "second_period": end["period"],
                "first_source_code": start["source_class_code"], "second_source_code": end["source_class_code"],
                "from_class": start["class_code"], "to_class": end["class_code"],
                "is_change": start["class_code"] != end["class_code"],
                "area_m2": polygon.area, "geometry": polygon,
            })
    return rows


def _check_balance(evidence: dict, differences: list[float], *, has_polygons: bool = True) -> None:
    if not has_polygons or any(not math.isfinite(value) or value > evidence["area_tolerance_m2"]
                              for value in differences):
        raise ValueError(f"Land use overlay failed area balance. {json.dumps(evidence, ensure_ascii=False)}")


def _overlay(request: dict) -> dict:
    parameters = request["parameters"]
    area_crs = CRS.from_user_input(parameters["area_crs"])
    tolerance = parameters["area_tolerance"]
    if parameters["area_unit"] != "square_meter" or not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("Overlay requires a finite nonnegative tolerance in square meters")
    frames = _overlay_inputs(request["inputs"], area_crs)
    try:
        scope = shapely.union_all(shapely.force_2d(frames[_EXTENT].geometry.to_numpy()))
        clipped, union, periods = _clipped_periods(frames, scope)
        evidence = {
            "area_crs": area_crs.to_string(), "area_unit": "square_meter", "area_tolerance_m2": tolerance,
            "analysis_extent_m2": scope.area, "periods": periods,
        }
        _check_balance(evidence, [
            metrics[key] for metrics in periods.values() for key in ("uncovered_area_m2", "overlap_area_m2")
        ])
        rows = _intersections(frames, clipped)
        first, second = _PERIODS
        common_area = union[first].intersection(union[second]).area
        total = math.fsum(row["area_m2"] for row in rows)
        union_area = shapely.union_all([row["geometry"] for row in rows]).area
    except GEOSException as error:
        raise ValueError(f"Land use overlay could not intersect valid polygon inputs. {error}") from error
    evidence.update(
        overlay_area_m2=total, overlay_union_area_m2=union_area, common_period_area_m2=common_area,
        fragment_count=len(rows),
    )
    _check_balance(evidence, [abs(scope.area - total), abs(common_area - total), abs(union_area - total)],
                   has_polygons=bool(rows))

    outputs = request["output_targets"]
    with _stage_outputs(outputs) as temporary:
        write_vector_outputs(temporary, {"overlay_polygons": (GeoDataFrame(rows, crs=area_crs), "Polygon")})
        report = temporary["overlay_report"]
        path = Path(report["path"])
        if path.suffix.lower() != ".json" or report.get("layer") is not None:
            raise ValueError("overlay_report requires a .json path without a layer")
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Land use overlay: {json.dumps(evidence, ensure_ascii=False)}")
    return outputs


def land_use_overlay(request: dict) -> dict:
    """Dispatch a trusted worker operation by the exact requested tool version."""
    operations = {
        "python-land-use-read": _read_periods, _MAP_TOOL: _map_periods,
        "python-land-use-project": _project_periods, "python-land-use-overlay": _overlay,
    }
    tool = request["tool"]
    operation = operations.get(tool["id"])
    if tool["version"] != 1 or operation is None:
        raise ValueError(f"Unknown land use tool version {tool!r}")
    return operation(request)


if __name__ == "__main__":
    raise SystemExit(run_tool_command(land_use_overlay, description=__doc__))
