import pytest
import yaml
from pydantic import ValidationError

from geopilot.domain.tool_contract import MeasurementPolicy, ParameterSpec, ToolContract, ToolRef


QGIS_BUFFER_YAML = """
id: qgis-buffer
version: 1
runner: qgis
implementation: native:buffer
inputs: {source: vector_line}
outputs: {buffer: vector_polygon}
parameters:
  distance: {type: number, unit: meter, minimum_exclusive: 0}
  segments: {type: integer, minimum: 1}
  dissolve: {type: boolean}
  end_cap_style: {type: enum, values: [round, flat, square]}
  join_style: {type: enum, values: [round, miter, bevel]}
  miter_limit: {type: number, minimum: 1}
measurement_policy:
  distance: local_projected
  area: not_applicable
  forbid_web_mercator: true
  forbid_geographic_planar: true
preconditions:
  - crs_is_known
  - projected_crs_suitable_for_extent
  - horizontal_unit_is_meter
  - geometry_is_valid
check_rules:
  - output_exists
  - output_crs_expected
"""


@pytest.fixture
def qgis_buffer():
    return yaml.safe_load(QGIS_BUFFER_YAML)


def test_v2_qgis_buffer_contract_yaml_and_json_round_trip(qgis_buffer):
    contract = ToolContract.model_validate(qgis_buffer)
    assert contract.ref == ToolRef(id="qgis-buffer", version=1)
    assert contract.runner == "qgis"
    assert contract.implementation == "native:buffer"
    assert contract.inputs == {"source": "vector_line"}
    assert contract.outputs == {"buffer": "vector_polygon"}
    assert isinstance(contract.parameters["distance"], ParameterSpec)
    assert contract.parameters["distance"].required is True
    assert contract.parameters["distance"].unit == "meter"
    assert contract.parameters["distance"].minimum_exclusive == 0
    assert list(contract.parameters["end_cap_style"].values) == ["round", "flat", "square"]
    assert isinstance(contract.measurement_policy, MeasurementPolicy)
    assert contract.measurement_policy.distance == "local_projected"
    assert contract.measurement_policy.area == "not_applicable"
    assert list(contract.preconditions) == qgis_buffer["preconditions"]
    assert list(contract.check_rules) == qgis_buffer["check_rules"]
    assert ToolContract.model_validate_json(contract.model_dump_json()) == contract


def test_python_contract_supports_explicit_empty_collections_and_no_measurement():
    contract = ToolContract.model_validate({
        "id": "python-copy",
        "version": 2,
        "runner": "python",
        "implementation": "geopilot.copy",
        "inputs": {},
        "outputs": {},
        "parameters": {
            "label": {"type": "string", "required": False, "default": "result"},
            "options": {"type": "json", "required": False, "default": {"levels": [1, None], "clip": True}},
            "limit": {"type": "integer", "required": False, "default": None},
        },
        "measurement_policy": {"distance": "not_applicable", "area": "not_applicable"},
        "preconditions": [],
        "check_rules": [],
    })
    assert contract.ref == ToolRef(id="python-copy", version=2)
    assert contract.runner == "python"
    assert contract.inputs == contract.outputs == {}
    assert not contract.preconditions and not contract.check_rules
    assert contract.parameters["label"].default == "result"
    assert contract.parameters["options"].default == {"levels": [1, None], "clip": True}
    assert contract.parameters["limit"].default is None
    assert contract.measurement_policy.forbid_web_mercator is True
    assert contract.measurement_policy.forbid_geographic_planar is True
    assert ToolContract.model_validate_json(contract.model_dump_json()) == contract


@pytest.mark.parametrize(
    "field",
    ["id", "version", "runner", "implementation", "inputs", "outputs", "parameters",
     "measurement_policy", "preconditions", "check_rules"],
)
def test_frozen_contract_fields_must_be_explicit(qgis_buffer, field):
    del qgis_buffer[field]
    with pytest.raises(ValidationError):
        ToolContract.model_validate(qgis_buffer)


@pytest.mark.parametrize("field,value", [("runner", "shell"), ("inputs", ["vector_line"]), ("check_rules", [1])])
def test_invalid_contract_types_are_rejected(qgis_buffer, field, value):
    qgis_buffer[field] = value
    with pytest.raises(ValidationError):
        ToolContract.model_validate(qgis_buffer)


@pytest.mark.parametrize("version", [0, -1, True, 1.0, "1"])
def test_version_is_a_strict_positive_integer(qgis_buffer, version):
    qgis_buffer["version"] = version
    with pytest.raises(ValidationError):
        ToolContract.model_validate(qgis_buffer)
    with pytest.raises(ValidationError):
        ToolRef(id="qgis-buffer", version=version)


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "list"},
        {"type": "number", "default": "500"},
        {"type": "number", "default": float("inf")},
        {"type": "integer", "default": 1.5},
        {"type": "integer", "default": True},
        {"type": "boolean", "default": "true"},
        {"type": "string", "default": 9},
        {"type": "json", "default": {"invalid": object()}},
        {"type": "string", "required": "false"},
        {"type": "boolean", "unit": "meter"},
        {"type": "number", "values": ["low", "high"]},
        {"type": "enum"},
        {"type": "enum", "values": []},
        {"type": "enum", "values": ["round", "round"]},
        {"type": "enum", "values": ["round", 0]},
        {"type": "enum", "values": ["round", "flat"], "default": "square"},
        {"type": "number", "minimun_exclusive": 0},
        {"type": "number", "minimum": "1"},
        {"type": "number", "minimum": 0, "minimum_exclusive": 0},
        {"type": "number", "maximum": 2, "maximum_exclusive": 2},
        {"type": "number", "minimum": 2, "maximum": 1},
        {"type": "number", "minimum_exclusive": 0, "default": 0},
        {"type": "integer", "maximum_exclusive": 5, "default": 5},
        {"type": "number", "minimum": 0, "maximum": 5, "default": 6},
    ],
)
def test_parameter_definition_rejects_invalid_constraints_and_defaults(spec):
    with pytest.raises(ValidationError):
        ParameterSpec.model_validate(spec)


def test_parameter_defaults_accept_declared_values_and_inclusive_limits():
    count = ParameterSpec(type="integer", minimum=1, maximum=5, default=5)
    style = ParameterSpec(type="enum", values=["round", "flat"], default="round")
    switch = ParameterSpec(type="boolean", required=False, default=False)
    assert count.default == 5
    assert style.default == "round"
    assert switch.default is False
    assert switch.required is False


def test_validate_value_applies_the_contract_to_values_in_declared_units():
    distance = ParameterSpec(type="number", unit="meter", minimum_exclusive=0, maximum=500)
    distance.validate_value(0.5)
    distance.validate_value(500)
    for invalid in (True, "500", float("nan"), float("inf"), 0, 501):
        with pytest.raises(ValueError):
            distance.validate_value(invalid)

    style = ParameterSpec(type="enum", values=["round", "flat"])
    style.validate_value("round")
    for invalid in (0, "ROUND", "square"):
        with pytest.raises(ValueError):
            style.validate_value(invalid)


@pytest.mark.parametrize(
    "policy",
    [
        {"area": "not_applicable"},
        {"distance": "not_applicable"},
        {"distance": "euclidean_degrees", "area": "not_applicable"},
        {"distance": "not_applicable", "area": "web_mercator"},
        {"distance": "local_projected", "area": "not_applicable", "forbid_web_mercator": False},
        {"distance": "local_projected", "area": "not_applicable", "forbid_geographic_planar": False},
        {"distance": "not_applicable", "area": "equal_area", "forbid_web_mercator": False},
    ],
)
def test_measurement_policy_rejects_unknown_modes_and_unsafe_planar_flags(qgis_buffer, policy):
    qgis_buffer["measurement_policy"] = policy
    with pytest.raises(ValidationError):
        ToolContract.model_validate(qgis_buffer)


def test_supported_geodesic_and_equal_area_strategies_can_be_declared():
    distance = MeasurementPolicy(distance="geodesic", area="not_applicable")
    area = MeasurementPolicy(distance="not_applicable", area="equal_area")
    assert distance.distance == "geodesic"
    assert area.area == "equal_area"
