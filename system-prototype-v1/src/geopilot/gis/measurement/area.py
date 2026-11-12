"""Select a verified equal-area metre projection for area measurement."""

from pyproj.crs import CoordinateOperation
from pyproj.exceptions import ProjError

from geopilot.domain.data_asset import AssetMetadata, VectorMetadata
from geopilot.domain.tool_contract import ToolContract
from geopilot.gis.measurement.coverage import (
    MeasurementDecision,
    MeasurementPolicyError,
    verify_projection_coverage,
)
from geopilot.gis.validation.crs_validator import CRSValidationError, inspect_crs


_EQUAL_AREA_PROJECTIONS = {
    "1078": "eqearth",
    "9820": "laea",
    "9822": "aea",
    "9835": "cea",
}
_AXIS_AND_UNIT_STEPS = {"pipeline", "axisswap", "unitconvert"}


def _uses_equal_area_method(operation: CoordinateOperation | None) -> bool:
    if operation is None or operation.method_auth_name != "EPSG":
        return False
    expected_projection = _EQUAL_AREA_PROJECTIONS.get(operation.method_code)
    if expected_projection is None:
        return False

    # A WKT method ID can disagree with the conversion that PROJ actually uses.
    try:
        definition = operation.to_proj4()
    except ProjError:
        return False
    steps = [token.removeprefix("+proj=") for token in (definition or "").split() if token.startswith("+proj=")]
    projections = [step for step in steps if step not in _AXIS_AND_UNIT_STEPS]
    return projections == [expected_projection]


def _validate_area(metadata: AssetMetadata, *, field: str, check_extent: bool) -> MeasurementDecision:
    """Require known metre axes, an equal-area method, and coverage of the input."""
    try:
        info = inspect_crs(metadata, field=field, require_projected_metres=True)
    except CRSValidationError as error:
        if error.code == "CRS_UNKNOWN":
            raise
        raise MeasurementPolicyError(error.message, field=error.field, observed=error.observed) from error

    horizontal = info.crs.to_2d()
    if horizontal.is_bound and horizontal.source_crs is not None:
        horizontal = horizontal.source_crs.to_2d()
    operation = horizontal.coordinate_operation
    if not _uses_equal_area_method(operation):
        method = operation.method_name if operation is not None else "unknown"
        raise MeasurementPolicyError(
            f"Area measurement requires a supported equal-area method. Observed {method}",
            field=field,
            observed=metadata.crs,
        )

    if check_extent:
        verify_projection_coverage(info.crs, metadata.bounds, field=field)
    return MeasurementDecision(method="equal_area", crs=metadata.crs, unit="square_meter")


def validate_area(metadata: AssetMetadata, *, field: str = "inputs.source.crs") -> MeasurementDecision:
    return _validate_area(metadata, field=field, check_extent=True)


def permits_empty_area_input(metadata: AssetMetadata, contract: ToolContract, *, field: str) -> bool:
    """Identify empty target layers whose zones still supply a verified measurement extent."""
    return (
        isinstance(metadata, VectorMetadata) and metadata.feature_count == 0 and metadata.bounds is None
        and field in {"inputs.targets.crs", "inputs.original_targets.crs"}
        and (contract.id, contract.version, contract.runner, contract.implementation) == (
            "python-polygon-zone-statistics", 1, "python", "geopilot.polygon_zone_statistics",
        )
    )


def validate_tool_area(metadata: AssetMetadata, contract: ToolContract, *, field: str) -> MeasurementDecision:
    return _validate_area(metadata, field=field, check_extent=not permits_empty_area_input(metadata, contract, field=field))
