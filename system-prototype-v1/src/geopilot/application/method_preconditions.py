"""Checks registered by the initial local method configurations."""

from collections.abc import Callable, Mapping

from pyproj import CRS
from pyproj.exceptions import CRSError

from geopilot.application.terrain_plan import terrain_inputs_are_consistent
from geopilot.domain.data_asset import DataAsset, VectorMetadata
from geopilot.gis.validation.crs_validator import CRSValidationError, inspect_crs
from geopilot.plans.models import AnalysisPlan, PlanIssue


MethodPrecondition = Callable[[AnalysisPlan, Mapping[str, DataAsset]], tuple[PlanIssue, ...]]


def _issue(field: str, message: str) -> PlanIssue:
    return PlanIssue(code="METHOD_PRECONDITION_FAILED", field=field, message=message)


def _point_count_inputs_match_distance_crs(
    plan: AnalysisPlan, assets: Mapping[str, DataAsset]
) -> tuple[PlanIssue, ...]:
    value = plan.critical_options.get("distance_crs_ref")
    try:
        target = CRS.from_user_input(value)
    except (CRSError, TypeError, ValueError):
        return (_issue("critical_options.distance_crs_ref", "Select a known local measurement CRS"),)

    issues = []
    for role in ("analysis_extent", "targets", "zones", "roads"):
        observed = assets[role].metadata.crs
        field = f"inputs.{role}.crs"
        try:
            source = CRS.from_user_input(observed)
        except (CRSError, TypeError, ValueError):
            issues.append(_issue(field, f"The selected {role} asset needs a known CRS"))
            continue
        if role != "roads" and not source.equals(target, ignore_axis_order=True):
            issues.append(_issue(
                field, f"Role {role} has {observed}, expected the confirmed distance CRS {value}"
            ))
    return tuple(issues)


def _road_point_count_inputs_match_measurement(
    plan: AnalysisPlan, assets: Mapping[str, DataAsset],
) -> tuple[PlanIssue, ...]:
    if plan.critical_options.get("distance_method") == "local_projected":
        return _point_count_inputs_match_distance_crs(plan, assets)

    types = {
        "analysis_extent": {"Polygon", "MultiPolygon"}, "roads": {"LineString", "MultiLineString"},
        "targets": {"Point"}, "zones": {"Polygon", "MultiPolygon"},
    }
    issues = []
    for role, geometry_types in types.items():
        metadata = assets[role].metadata
        if not isinstance(metadata, VectorMetadata) or metadata.geometry_type not in geometry_types:
            issues.append(_issue(f"inputs.{role}.geometry", f"Role {role} requires a supported 2D geometry"))
            continue
        field = f"inputs.{role}.crs"
        try:
            crs = inspect_crs(metadata, field=field).crs
        except CRSValidationError as error:
            issues.append(_issue(field, error.message))
        else:
            if len(crs.axis_info) != 2:
                issues.append(_issue(field, "WGS84 preparation requires a known two-dimensional source CRS"))
    return tuple(issues)


def _selected_field_exists(plan: AnalysisPlan, assets: Mapping[str, DataAsset]) -> tuple[PlanIssue, ...]:
    field = plan.critical_options.get("field_name")
    available = {column.name for column in getattr(assets["features"].metadata, "fields", ())}
    if not isinstance(field, str) or field not in available:
        return (_issue("critical_options.field_name", f"Field {field!r} is absent from the selected asset"),)
    return ()


METHOD_PRECONDITIONS: dict[str, MethodPrecondition] = {
    "point_count_inputs_match_distance_crs": _point_count_inputs_match_distance_crs,
    "road_point_count_inputs_match_measurement": _road_point_count_inputs_match_measurement,
    "selected_field_exists": _selected_field_exists,
    "terrain_inputs_are_consistent": terrain_inputs_are_consistent,
}
