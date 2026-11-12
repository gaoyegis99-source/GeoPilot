"""Resolve and confirm exact local product configurations for NDVI change."""

from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue
from pyproj import CRS
from pyproj.exceptions import CRSError

from geopilot.application.task_context import InputReadiness, TaskContext
from geopilot.data.assets import InputChangedError
from geopilot.data.errors import SourceReadError
from geopilot.data.resolver import resolve_asset
from geopilot.domain.data_asset import DataAsset
from geopilot.gis.measurement.coverage import MeasurementPolicyError
from geopilot.gis.measurement.projection import select_measurement_crs
from geopilot.gis.validation.crs_validator import CRSValidationError
from geopilot.methods.models import MethodRef
from geopilot.plans.models import AnalysisPlan, PlanIssue
from geopilot.remote_sensing.band_mapper import BandMappingError, map_product_bands
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.remote_sensing.product_metadata import ProductMetadataError
from geopilot.remote_sensing.scaling import ReflectanceScalingError, resolve_reflectance_coefficients


_REMOTE_METHOD = MethodRef(id="remote-sensing-index-change", version=1)
_PRODUCT_OPTIONS = {"first_product": "first_configuration", "second_product": "second_configuration"}
_PRODUCT_ERRORS = (
    ProductMetadataError, BandMappingError, ReflectanceScalingError,
    SourceReadError, InputChangedError, LookupError,
)
_EMPTY_REGISTRY = SensorConfigRegistry(())


def derived_remote_sensing_options(method: MethodRef) -> tuple[str, ...]:
    return tuple(_PRODUCT_OPTIONS.values()) if method == _REMOTE_METHOD else ()


def _configuration_snapshot(asset: DataAsset, sensor_registry: SensorConfigRegistry | None) -> dict[str, JsonValue]:
    mapping = map_product_bands(asset.source.path, registry=sensor_registry or _EMPTY_REGISTRY)
    resolve_reflectance_coefficients(mapping)
    return mapping.configuration.model_dump(mode="json")


def _product_issue(role: str, error: Exception) -> PlanIssue:
    field = getattr(error, "field", "sensor_configuration")
    return PlanIssue(
        code=getattr(error, "code", "SOURCE_UNREADABLE"), field=f"inputs.{role}.{field}", message=str(error),
    )


def resolve_remote_sensing_draft_options(
    database_path: str | Path, context: TaskContext, options: Mapping[str, JsonValue],
    *, sensor_registry: SensorConfigRegistry | None,
) -> tuple[TaskContext, Mapping[str, JsonValue]]:
    if context.method != _REMOTE_METHOD:
        return context, options
    resolved = dict(options)
    roles = context.roles.copy()
    for role, option in _PRODUCT_OPTIONS.items():
        resolved.pop(option, None)
        selected = roles.get(role)
        if selected is None or selected.asset is None or selected.status is not InputReadiness.READY:
            continue
        try:
            asset = resolve_asset(database_path, selected.asset)
            resolved[option] = _configuration_snapshot(asset, sensor_registry)
        except _PRODUCT_ERRORS as error:
            issue = _product_issue(role, error)
            roles[role] = selected.model_copy(update={
                "status": InputReadiness.BLOCKED, "code": issue.code, "message": issue.message,
            })
    return context.model_copy(update={"roles": roles}), resolved


def remote_sensing_inputs_are_consistent(
    plan: AnalysisPlan, assets: Mapping[str, DataAsset], *, sensor_registry: SensorConfigRegistry | None,
) -> tuple[PlanIssue, ...]:
    issues = []
    for role, option in _PRODUCT_OPTIONS.items():
        try:
            configuration = _configuration_snapshot(assets[role], sensor_registry)
        except _PRODUCT_ERRORS as error:
            issues.append(_product_issue(role, error))
            continue
        if plan.critical_options.get(option) != configuration:
            issues.append(PlanIssue(
                code="SENSOR_CONFIGURATION_CHANGED", field=f"critical_options.{option}",
                message="Generate the draft again to record the current product configuration.",
            ))
    if issues:
        return tuple(issues)

    extent = assets["analysis_extent"].metadata
    try:
        CRS.from_user_input(extent.crs)
    except (CRSError, TypeError, ValueError):
        return (PlanIssue(
            code="METHOD_PRECONDITION_FAILED", field="inputs.analysis_extent.crs",
            message="The analysis extent requires a known CRS.",
        ),)
    if extent.bounds is None:
        return (PlanIssue(
            code="METHOD_PRECONDITION_FAILED", field="inputs.analysis_extent.bounds",
            message="Choose a nonempty polygon analysis extent.",
        ),)
    try:
        select_measurement_crs(
            assets["first_product"].metadata, method="equal_area", target_crs=plan.critical_options["area_crs"],
        )
    except (MeasurementPolicyError, CRSValidationError) as error:
        return (PlanIssue(code="METHOD_PRECONDITION_FAILED", field="critical_options.area_crs", message=error.message),)
    return ()
