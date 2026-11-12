"""Apply a tool contract to confirmed plan parameters and explicit quantities."""

import math
from collections.abc import Mapping
from copy import deepcopy
from fractions import Fraction

from pydantic import JsonValue

from geopilot.domain.tool_contract import ParameterSpec, ToolContract


_UNIT_SCALES = {
    "meter": ("length", "1"),
    "kilometer": ("length", "1000"),
    "centimeter": ("length", "0.01"),
    "millimeter": ("length", "0.001"),
    "foot": ("length", "0.3048"),
    "us_survey_foot": ("length", "1200/3937"),
    "square_meter": ("area", "1"),
    "square_kilometer": ("area", "1000000"),
    "square_foot": ("area", "0.09290304"),
    "hectare": ("area", "10000"),
    "radian": ("angle", "1"),
    "degree": ("angle", str(math.pi / 180)),
    "ratio": ("ratio", "1"),
    "percent": ("ratio", "0.01"),
}


class ParameterNormalizationError(ValueError):
    code = "INVALID_PARAMETER"
    phase = "preflight"
    suggestion = "Use a value and unit declared by the selected tool contract."

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def _convert_quantity(spec: ParameterSpec, quantity: Mapping) -> int | float:
    if set(quantity) != {"value", "unit"}:
        raise ValueError("An explicit quantity must contain only value and unit")
    if spec.unit is None:
        raise ValueError("This parameter does not declare a unit")

    value, unit = quantity["value"], quantity["unit"]
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValueError("A quantity requires a finite numeric value")
    if not isinstance(unit, str) or not unit:
        raise ValueError("A quantity requires an explicit unit name")
    if unit == spec.unit:
        return value

    origin = _UNIT_SCALES.get(unit)
    target = _UNIT_SCALES.get(spec.unit)
    if origin is None or target is None or origin[0] != target[0]:
        raise ValueError(f"Cannot convert {unit!r} to {spec.unit!r}")

    converted = Fraction(str(value)) * Fraction(origin[1]) / Fraction(target[1])
    if converted.denominator == 1:
        return converted.numerator
    if spec.type == "integer":
        raise ValueError("The converted quantity is not an exact integer")
    result = float(converted)
    if result == 0 and converted != 0:
        raise ValueError("The converted quantity is too small to represent")
    return result


def normalize_parameters(
    contract: ToolContract, values: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    """Return validated JSON values in the contract's units and parameter order."""
    if not isinstance(values, Mapping):
        raise ParameterNormalizationError("parameters", "Expected an object of tool parameters")
    for name in values:
        if name not in contract.parameters:
            raise ParameterNormalizationError(f"parameters.{name}", f"Unknown parameter: {name!r}")

    normalized = {}
    for name, spec in contract.parameters.items():
        field = f"parameters.{name}"
        if name in values:
            value = values[name]
        elif spec.default is not None:
            value = spec.default
        elif spec.required:
            raise ParameterNormalizationError(field, "Required parameter is missing")
        else:
            continue

        try:
            if spec.type in {"number", "integer"} and isinstance(value, Mapping):
                value = _convert_quantity(spec, value)
            spec.validate_value(value)
        except (ValueError, OverflowError) as error:
            raise ParameterNormalizationError(field, str(error)) from error
        normalized[name] = deepcopy(value)
    return normalized
