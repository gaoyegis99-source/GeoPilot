"""Resolve and record the horizontal and elevation units of a DEM."""

import math
from dataclasses import dataclass
from typing import Literal

from pyproj import CRS

from geopilot.domain.data_asset import RasterMetadata
from geopilot.domain.tool_contract import ParameterSpec
from geopilot.gis.validation.crs_validator import CRSValidationError, inspect_crs
from geopilot.tools.parameter_normalizer import _convert_quantity


_ELEVATION_FIELD = "critical_options.vertical_unit"
_METRES = ParameterSpec(type="number", unit="meter")
_UNIT_NAMES = {
    "meter": ("m", "meter", "metre", "meters", "metres"),
    "kilometer": ("km", "kilometer", "kilometre", "kilometers", "kilometres"),
    "centimeter": ("cm", "centimeter", "centimetre", "centimeters", "centimetres"),
    "millimeter": ("mm", "millimeter", "millimetre", "millimeters", "millimetres"),
    "foot": ("ft", "foot", "feet", "international foot", "international feet"),
    "us_survey_foot": ("us survey foot", "us survey feet", "u.s. survey foot", "ftus", "us-ft"),
}
_ALIASES = {name: canonical for canonical, names in _UNIT_NAMES.items() for name in names}


class DemUnitError(ValueError):
    phase = "preflight"

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


@dataclass(frozen=True, slots=True)
class DemUnits:
    horizontal_unit: str
    vertical_unit: str
    horizontal_metres_per_unit: float
    vertical_metres_per_unit: float
    z_factor: float
    vertical_source: Literal["band", "vertical_crs", "explicit"]


def _declared_unit(value: str, field: str) -> tuple[str, float]:
    token = " ".join(value.casefold().replace("_", " ").split())
    unit = _ALIASES.get(token)
    if unit is None:
        raise DemUnitError("DEM_VERTICAL_UNIT_UNSUPPORTED", field, "The elevation unit must be a known linear unit.")
    return unit, float(_convert_quantity(_METRES, {"value": 1, "unit": unit}))


def _vertical_crs_unit(crs: CRS) -> tuple[str, float] | None:
    for component in crs.sub_crs_list:
        if not component.is_vertical:
            continue
        axes = component.axis_info
        if len(axes) != 1 or axes[0].direction.casefold() != "up":
            raise DemUnitError("DEM_VERTICAL_AXIS_UNSUPPORTED", "inputs.dem.crs", "The DEM requires an upward elevation axis.")
        axis = axes[0]
        scale = axis.unit_conversion_factor
        coordinate_system = component.coordinate_system
        unit_json = coordinate_system.to_json_dict()["axis"][0]["unit"]
        linear = unit_json == "metre" or (isinstance(unit_json, dict) and unit_json.get("type") == "LinearUnit")
        if not linear or not math.isfinite(scale) or scale <= 0:
            raise DemUnitError("DEM_VERTICAL_UNIT_UNSUPPORTED", "inputs.dem.crs", "The vertical CRS has no finite linear unit.")
        name = _ALIASES.get(axis.unit_name.casefold(), axis.unit_name)
        return name, scale
    return None


def resolve_dem_units(metadata: RasterMetadata, *, band: int = 1, vertical_unit: str | None = None) -> DemUnits:
    """Return vertical-to-horizontal scale, with the source of the height unit."""
    if type(band) is not int or not 1 <= band <= metadata.band_count:
        raise DemUnitError("DEM_BAND_INVALID", "parameters.band", "The selected elevation band does not exist.")
    field = "inputs.dem.crs"
    info = inspect_crs(metadata, field=field)
    horizontal = info.metres_per_unit
    if horizontal is None or info.is_web_mercator:
        raise CRSValidationError(
            "Terrain measurement requires a suitable projected CRS with linear axes.",
            code="MEASUREMENT_POLICY_REJECTED", field=field, observed=metadata.crs,
        )
    declared = metadata.band_units[band - 1]
    band_unit = _declared_unit(declared, "inputs.dem.band_units") if declared is not None else None
    crs_unit = _vertical_crs_unit(info.crs)
    explicit = _declared_unit(vertical_unit, _ELEVATION_FIELD) if vertical_unit is not None else None
    observations = [value for value in (explicit, crs_unit, band_unit) if value is not None]
    if not observations:
        raise DemUnitError("DEM_VERTICAL_UNIT_REQUIRED", _ELEVATION_FIELD, "Specify the elevation unit for this DEM.")
    vertical, factor = observations[0]
    if any(not math.isclose(factor, value[1], rel_tol=1e-12) for value in observations[1:]):
        raise DemUnitError("DEM_UNIT_CONFLICT", _ELEVATION_FIELD, "The recorded elevation units disagree.")
    ratio = factor / horizontal
    if not math.isfinite(ratio) or ratio <= 0:
        raise DemUnitError("DEM_UNIT_CONFLICT", _ELEVATION_FIELD, "The DEM unit ratio is outside the supported range.")
    origin = "explicit" if explicit else "vertical_crs" if crs_unit else "band"
    return DemUnits(info.horizontal_unit, vertical, horizontal, factor, ratio, origin)
