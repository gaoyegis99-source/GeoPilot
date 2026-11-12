from pathlib import Path

import pytest
import yaml

from geopilot.domain.tool_contract import ToolContract
from geopilot.tools.parameter_normalizer import ParameterNormalizationError, normalize_parameters


EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "tools" / "qgis-buffer.yaml"


@pytest.fixture
def buffer_contract():
    return ToolContract.model_validate(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))


@pytest.fixture
def buffer_values():
    return {
        "distance": {"value": 0.5, "unit": "kilometer"}, "segments": 16,
        "dissolve": True, "end_cap_style": "round", "join_style": "miter", "miter_limit": 2,
    }


def test_confirmed_buffer_parameters_convert_explicit_units(buffer_contract, buffer_values):
    assert normalize_parameters(buffer_contract, buffer_values) == {**buffer_values, "distance": 500}
    assert buffer_values["distance"] == {"value": 0.5, "unit": "kilometer"}


@pytest.mark.parametrize(("field", "value"), [
    ("distance", "500"),
    ("distance", 0),
    ("distance", float("inf")),
    ("distance", {"value": 1, "unit": "hectare"}),
    ("distance", {"value": 1, "unit": "unspecified"}),
    ("distance", {"value": 1, "unit": "meter", "comment": "ignored?"}),
    ("segments", True),
    ("segments", 16.0),
    ("end_cap_style", "ROUND"),
    ("dissolve", "false"),
])
def test_invalid_values_point_to_the_parameter(buffer_contract, buffer_values, field, value):
    with pytest.raises(ParameterNormalizationError) as result:
        normalize_parameters(buffer_contract, {**buffer_values, field: value})
    assert result.value.code == "INVALID_PARAMETER"
    assert result.value.phase == "preflight"
    assert result.value.field == f"parameters.{field}"
    assert result.value.message


def test_missing_and_unexpected_keys_are_not_silently_ignored(buffer_contract, buffer_values):
    missing_distance = {key: value for key, value in buffer_values.items() if key != "distance"}
    with pytest.raises(ParameterNormalizationError) as missing:
        normalize_parameters(buffer_contract, missing_distance)
    assert missing.value.field == "parameters.distance"

    with pytest.raises(ParameterNormalizationError) as unexpected:
        normalize_parameters(buffer_contract, {**buffer_values, "radius": 1})
    assert unexpected.value.field == "parameters.radius"


def test_defaults_and_explicit_integer_unit_conversion(buffer_contract):
    contract_data = buffer_contract.model_dump(mode="json")
    contract_data["parameters"] = {
        "length": {"type": "integer", "unit": "meter"},
        "area": {"type": "number", "unit": "square_meter"},
        "dissolve": {"type": "boolean", "default": False},
        "caption": {"type": "string", "required": False},
        "field_values": {"type": "json", "default": {"class": ["A", "B"]}},
    }
    contract = ToolContract.model_validate(contract_data)
    values = {"length": {"value": 1.5, "unit": "kilometer"}, "area": {"value": 2, "unit": "hectare"}}
    normalized = normalize_parameters(contract, values)
    assert normalized == {"length": 1500, "area": 20000, "dissolve": False, "field_values": {"class": ["A", "B"]}}
    assert type(normalized["length"]) is int
    normalized["field_values"]["class"].clear()
    assert normalize_parameters(contract, values)["field_values"] == {"class": ["A", "B"]}

    with pytest.raises(ParameterNormalizationError) as fractional:
        normalize_parameters(contract, {**values, "length": {"value": 1, "unit": "centimeter"}})
    assert fractional.value.field == "parameters.length"
