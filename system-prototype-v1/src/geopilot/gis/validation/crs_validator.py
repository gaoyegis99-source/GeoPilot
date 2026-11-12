"""Validate explicit CRS definitions and their horizontal measurement units."""

import math
from dataclasses import dataclass
from typing import Literal

from pyproj import CRS
from pyproj.exceptions import CRSError

from geopilot.domain.data_asset import AssetMetadata, TableMetadata


_CRSInput = str | int | CRS | None
_ErrorCode = Literal["CRS_UNKNOWN", "MEASUREMENT_POLICY_REJECTED"]
_UNDEFINED_CRS_NAMES = {"undefined cartesian srs", "undefined geographic srs"}
_WEB_MERCATOR = (CRS.from_epsg(3857), CRS.from_epsg(3785))
_COMPASS_DIRECTIONS = ("north", "south", "east", "west")


class CRSValidationError(ValueError):
    phase = "preflight"

    def __init__(self, message: str, *, code: _ErrorCode, field: str, observed: object) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message
        self.observed = observed


@dataclass(frozen=True, slots=True)
class CRSInfo:
    crs: CRS
    horizontal_unit: str
    metres_per_unit: float | None
    is_web_mercator: bool


def _observed_crs(value: _CRSInput) -> str | int | None:
    return value.to_string() if isinstance(value, CRS) else value


def _parse_crs(value: _CRSInput, field: str) -> CRS:
    observed = _observed_crs(value)
    if value is None:
        raise CRSValidationError("CRS is missing", code="CRS_UNKNOWN", field=field, observed=observed)
    try:
        crs = CRS.from_user_input(value)
    except (CRSError, TypeError, ValueError) as error:
        raise CRSValidationError(
            "CRS could not be parsed", code="CRS_UNKNOWN", field=field, observed=observed
        ) from error
    if crs.name.casefold() in _UNDEFINED_CRS_NAMES:
        raise CRSValidationError("CRS is undefined", code="CRS_UNKNOWN", field=field, observed=observed)
    return crs


def _policy_error(message: str, field: str, observed: object) -> CRSValidationError:
    return CRSValidationError(
        message, code="MEASUREMENT_POLICY_REJECTED", field=field, observed=observed
    )


def _is_web_mercator(crs: CRS) -> bool:
    operation = crs.coordinate_operation
    if operation is not None:
        if (operation.method_auth_name, operation.method_code) == ("EPSG", "1024"):
            return True
        # WKT can retain the projection method while omitting its authority ID.
        method = " ".join(operation.method_name.casefold().replace("-", " ").split())
        if method in {
            "popular visualisation pseudo mercator",
            "popular visualization pseudo mercator",
        }:
            return True
    return any(crs.equals(reference, ignore_axis_order=True) for reference in _WEB_MERCATOR)


def _inspect_value(value: _CRSInput, field: str) -> CRSInfo:
    crs = _parse_crs(value, field)
    observed = _observed_crs(value)
    horizontal = crs.to_2d()
    if horizontal.is_bound and horizontal.source_crs is not None:
        horizontal = horizontal.source_crs.to_2d()

    if not (horizontal.is_projected or horizontal.is_geographic):
        raise _policy_error("CRS has no projected or geographic horizontal component", field, observed)
    axes = horizontal.axis_info
    if len(axes) != 2 or any(not axis.direction.casefold().startswith(_COMPASS_DIRECTIONS) for axis in axes):
        raise _policy_error("CRS must have two horizontal axes", field, observed)

    # PROJJSON preserves the unit category. A radian and a metre can both have factor 1.
    category, builtin_unit = ("LinearUnit", "metre") if horizontal.is_projected else ("AngularUnit", "degree")
    coordinate_system = horizontal.coordinate_system
    units = [axis.get("unit") for axis in coordinate_system.to_json_dict()["axis"]]
    if len(units) != 2 or any(
        unit != builtin_unit and (not isinstance(unit, dict) or unit.get("type") != category)
        for unit in units
    ):
        raise _policy_error(f"Both horizontal axes must use {category} units", field, observed)

    first_factor, second_factor = (axis.unit_conversion_factor for axis in axes)
    if not math.isfinite(first_factor) or first_factor <= 0 or first_factor != second_factor:
        names = ", ".join(axis.unit_name for axis in axes)
        raise _policy_error(f"Horizontal axes have incompatible units: {names}", field, observed)

    return CRSInfo(
        crs=crs,
        horizontal_unit=axes[0].unit_name,
        metres_per_unit=first_factor if horizontal.is_projected else None,
        is_web_mercator=horizontal.is_projected and _is_web_mercator(horizontal),
    )


def inspect_crs(
    metadata: AssetMetadata, *, field: str = "inputs.source.crs", require_projected_metres: bool = False
) -> CRSInfo:
    """Inspect a file's observed CRS and optionally require a projected metre grid."""
    observed = metadata.crs
    if isinstance(metadata, TableMetadata) and observed is not None:
        raise _policy_error("A nonspatial table has no horizontal coordinates", field, observed)
    info = _inspect_value(observed, field)
    if require_projected_metres:
        if info.metres_per_unit is None:
            raise _policy_error("Geographic CRS axes are angular and cannot be used for planar measurements", field, observed)
        if info.is_web_mercator:
            raise _policy_error("Web Mercator cannot be used for planar measurements", field, observed)
        if info.metres_per_unit != 1:
            raise _policy_error(
                f"Planar measurement requires metre axes, observed {info.horizontal_unit}", field, observed
            )
    return info


def validate_crs_operation(
    source_crs: _CRSInput, target_crs: _CRSInput, *, operation: Literal["assign", "reproject"]
) -> None:
    """Check the declarations for assignment or reprojection without changing any data."""
    if operation == "assign":
        if source_crs is not None:
            _parse_crs(source_crs, "source_crs")
            raise _policy_error("Assignment requires an unknown source CRS", "source_crs", _observed_crs(source_crs))
    elif operation == "reproject":
        _inspect_value(source_crs, "source_crs")
    else:
        raise _policy_error("Expected assign or reproject", "operation", operation)
    _inspect_value(target_crs, "target_crs")
