"""Resolve the DEM unit ratio and enforce local terrain plan preconditions."""

import math
from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue, ValidationError
from pyproj import CRS
from pyproj.exceptions import CRSError

from geopilot.application.task_context import InputReadiness, TaskContext
from geopilot.data.assets import InputChangedError
from geopilot.data.errors import SourceReadError
from geopilot.data.resolver import resolve_asset
from geopilot.domain.data_asset import DataAsset, RasterMetadata
from geopilot.gis.measurement.coverage import MeasurementPolicyError
from geopilot.gis.measurement.projection import select_measurement_crs
from geopilot.gis.raster.extent_expansion import expand_analysis_window
from geopilot.gis.raster.terrain import HORN_RADIUS_PIXELS
from geopilot.gis.raster.threshold import ThresholdSpec
from geopilot.gis.raster.units import DemUnitError, DemUnits, resolve_dem_units
from geopilot.gis.validation.crs_validator import CRSValidationError
from geopilot.methods.models import MethodRef
from geopilot.plans.models import AnalysisPlan, PlanIssue


_TERRAIN_METHOD = MethodRef(id="dem-terrain-analysis", version=1)


def derived_terrain_options(method: MethodRef) -> tuple[str, ...]:
    return ("z_factor",) if method == _TERRAIN_METHOD else ()


def _terrain_units(metadata: RasterMetadata, options: Mapping[str, JsonValue]) -> DemUnits:
    vertical = options.get("vertical_unit", "metadata")
    if not isinstance(vertical, str):
        raise DemUnitError("DEM_VERTICAL_UNIT_UNSUPPORTED", "critical_options.vertical_unit",
                           "Specify a valid elevation unit for the DEM.")
    return resolve_dem_units(
        metadata, band=options.get("band", 1), vertical_unit=None if vertical == "metadata" else vertical,
    )


def resolve_terrain_draft_options(
    database_path: str | Path, context: TaskContext, options: Mapping[str, JsonValue],
) -> tuple[TaskContext, Mapping[str, JsonValue]]:
    if not derived_terrain_options(context.method):
        return context, options
    resolved = dict(options)
    resolved.pop("z_factor", None)
    dem = context.roles.get("dem")
    if dem is None or dem.asset is None or dem.status is not InputReadiness.READY:
        return context, resolved
    try:
        metadata = resolve_asset(database_path, dem.asset).metadata
        if not isinstance(metadata, RasterMetadata):
            raise DemUnitError("DEM_INPUT_REQUIRED", "inputs.dem", "Select an elevation raster.")
        resolved["z_factor"] = _terrain_units(metadata, resolved).z_factor
    except (DemUnitError, CRSValidationError, SourceReadError, InputChangedError, LookupError) as error:
        blocked = dem.model_copy(update={
            "status": InputReadiness.BLOCKED, "code": getattr(error, "code", "SOURCE_UNREADABLE"),
            "message": str(error),
        })
        context = context.model_copy(update={"roles": {**context.roles, "dem": blocked}})
    return context, resolved


def _issue(field: str, message: str) -> PlanIssue:
    return PlanIssue(code="METHOD_PRECONDITION_FAILED", field=field, message=message)


def terrain_inputs_are_consistent(
    plan: AnalysisPlan, assets: Mapping[str, DataAsset],
) -> tuple[PlanIssue, ...]:
    dem = assets["dem"].metadata
    extent = assets["analysis_extent"].metadata
    options = plan.critical_options
    if not isinstance(dem, RasterMetadata):
        return (_issue("inputs.dem", "Select an elevation raster."),)
    try:
        units = _terrain_units(dem, options)
    except (DemUnitError, CRSValidationError) as error:
        return (_issue(error.field, error.message),)
    ratio = options.get("z_factor")
    if type(ratio) not in (int, float) or not math.isclose(ratio, units.z_factor, rel_tol=1e-12):
        return (_issue("critical_options.z_factor", "Generate the draft again to verify the DEM unit ratio."),)

    try:
        analysis_crs = CRS.from_user_input(extent.crs).to_2d()
        source_crs = CRS.from_user_input(dem.crs).to_2d()
    except (CRSError, TypeError, ValueError):
        return (_issue("inputs.analysis_extent.crs", "The analysis extent requires a known CRS."),)
    if not analysis_crs.equals(source_crs, ignore_axis_order=True):
        return (_issue("inputs.analysis_extent.crs", "The analysis extent must use the DEM horizontal CRS."),)
    if extent.bounds is None:
        return (_issue("inputs.analysis_extent.bounds", "Choose a nonempty polygon analysis extent."),)
    try:
        windows = expand_analysis_window(dem, extent.bounds, HORN_RADIUS_PIXELS)
    except ValueError as error:
        return (_issue("inputs.dem.bounds", str(error)),)
    try:
        ThresholdSpec.model_validate(options.get("threshold"))
    except ValidationError:
        return (_issue("critical_options.threshold", "Specify nonempty, non-overlapping threshold intervals."),)
    try:
        select_measurement_crs(
            dem.model_copy(update={"bounds": windows.analysis_bounds}),
            method="equal_area", target_crs=options.get("area_crs"),
        )
    except (MeasurementPolicyError, CRSValidationError) as error:
        return (_issue("critical_options.area_crs", error.message),)
    return ()
