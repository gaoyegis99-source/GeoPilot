import os
import shutil
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from geopilot.domain.tool_contract import ParameterSpec, ToolContract
from geopilot.methods.loader import load_method_catalog
from geopilot.methods.models import MethodDefinition
from geopilot.methods.validator import MethodIssue, validate_method
from geopilot.tools.registry import ToolRegistry


CONFIG = Path(__file__).resolve().parents[2] / "config"


def _tool(filename):
    return ToolContract.model_validate(yaml.safe_load((CONFIG / "tools" / filename).read_text(encoding="utf-8")))


@pytest.fixture
def area_contract():
    return _tool("python-area.yaml")


@pytest.fixture
def registry(area_contract):
    registry = ToolRegistry(python_implementations=(area_contract.implementation,))
    registry.register(area_contract)
    return registry


@pytest.fixture(scope="module")
def qgis_registry():
    executable = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if executable is None:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS to exercise the real QGIS tool registry")
    registry = ToolRegistry(qgis_process=executable)
    for filename in ("qgis-buffer.yaml", "qgis-extract-by-attribute.yaml"):
        contract = _tool(filename)
        registry.register(contract)
        registered = registry.get(contract.id, contract.version)
        assert registered.available, registered.reason
    return registry


@pytest.fixture
def method():
    return {
        "id": "parcel-area-report",
        "version": 1,
        "locked_order": True,
        "inputs": {"parcels": "vector_polygon"},
        "critical_options": {"area_crs": {"type": "string"}},
        "steps": [{
            "key": "calculate_area",
            "tool": {"id": "python-area", "version": 1},
            "inputs": {"source": {"asset": "parcels"}},
            "parameters": {"area_crs": {"option": "area_crs"}, "area_unit": {"literal": "square_meter"}},
            "outputs": {"statistics": "table_csv"},
        }],
        "expected_artifacts": [
            {"role": "parcel_areas", "step": "calculate_area", "output": "statistics", "type": "table_csv"},
        ],
        "check_rules": ["output_exists"],
    }


def _locations(issues):
    return [(issue.code, issue.field) for issue in issues]


def test_config_and_model_pass_with_exact_tool_and_registered_method_rule(method, registry):
    definition = MethodDefinition.model_validate(method)

    assert validate_method(method, registry=registry, registered_rules={"output_exists"}) == ()
    assert validate_method(definition, registry=registry, registered_rules={"output_exists": object()}) == ()


@pytest.mark.parametrize("missing", ["id", "steps", "expected_artifacts", "check_rules"])
def test_missing_config_fields_identify_file_and_field(method, registry, tmp_path, missing):
    del method[missing]
    path = tmp_path / "broken-method.yaml"
    path.write_text(yaml.safe_dump(method), encoding="utf-8")

    issues = validate_method(
        yaml.safe_load(path.read_text(encoding="utf-8")), registry=registry,
        registered_rules={"output_exists"}, source=str(path),
    )

    (issue,) = issues
    assert isinstance(issue, MethodIssue)
    assert (issue.code, issue.field, issue.path) == ("INVALID_METHOD_CONFIG", missing, path)
    assert "required" in issue.message


def test_model_is_revalidated_after_mutation_of_nested_config(method, registry):
    definition = MethodDefinition.model_validate(method)
    definition.inputs["parcels"] = None

    (issue,) = validate_method(definition, registry=registry, registered_rules={"output_exists"})

    assert (issue.code, issue.field, issue.path) == ("INVALID_METHOD_CONFIG", "inputs.parcels", None)


def test_empty_steps_and_duplicate_step_key_block_execution(method, registry):
    (first,) = method["steps"]
    method["steps"].append(deepcopy(first))

    assert _locations(validate_method(method, registry=registry, registered_rules={"output_exists"})) == [
        ("DUPLICATE_STEP_KEY", "steps.1.key"),
    ]

    method["steps"].clear()
    method["expected_artifacts"].clear()
    assert _locations(validate_method(method, registry=registry, registered_rules={"output_exists"})) == [
        ("MISSING_STEPS", "steps"),
    ]


@pytest.mark.parametrize("tool_id,version", [("not-registered", 1), ("python-area", 2)])
def test_unknown_tool_id_or_exact_version_is_reported(method, registry, tool_id, version):
    method["steps"][0]["tool"] = {"id": tool_id, "version": version}

    (issue,) = validate_method(method, registry=registry, registered_rules={"output_exists"})

    assert (issue.code, issue.field) == ("TOOL_NOT_REGISTERED", "steps.0.tool")
    assert tool_id in issue.message and str(version) in issue.message


def test_registered_but_unavailable_tool_reports_registry_reason(method, area_contract):
    registry = ToolRegistry()
    registry.register(area_contract)

    (issue,) = validate_method(method, registry=registry, registered_rules={"output_exists"})

    assert (issue.code, issue.field) == ("TOOL_UNAVAILABLE", "steps.0.tool")
    assert registry.get(area_contract.id, area_contract.version).reason in issue.message


def test_missing_extra_and_wrongly_typed_tool_ports_are_located(method, registry):
    first = method["steps"][0]
    first["inputs"] = {"wrong_name": {"asset": "parcels"}}
    first["outputs"] = {"wrong_name": "table_csv"}
    method["expected_artifacts"] = []

    issues = validate_method(method, registry=registry, registered_rules={"output_exists"})

    assert _locations(issues) == [
        ("MISSING_INPUT_PORT", "steps.0.inputs.source"),
        ("UNKNOWN_INPUT_PORT", "steps.0.inputs.wrong_name"),
        ("MISSING_OUTPUT_PORT", "steps.0.outputs.statistics"),
        ("UNKNOWN_OUTPUT_PORT", "steps.0.outputs.wrong_name"),
    ]

    first["inputs"] = {"source": {"asset": "parcels"}}
    first["outputs"] = {"statistics": "vector_polygon"}
    method["inputs"]["parcels"] = "vector_point_or_line"

    issues = validate_method(method, registry=registry, registered_rules={"output_exists"})

    assert _locations(issues) == [
        ("INPUT_TYPE_MISMATCH", "steps.0.inputs.source"),
        ("OUTPUT_TYPE_MISMATCH", "steps.0.outputs.statistics"),
    ]


@pytest.mark.parametrize("declared_type", ["vector", "vector_point_or_polygon", "vector_polygon"])
def test_compatible_declared_asset_types_do_not_require_a_selected_asset(method, registry, declared_type):
    method["inputs"]["parcels"] = declared_type

    assert validate_method(method, registry=registry, registered_rules={"output_exists"}) == ()


def test_named_step_output_type_is_checked_for_input_port(method, registry):
    follow_up = deepcopy(method["steps"][0])
    follow_up["key"] = "follow_up"
    follow_up["inputs"]["source"] = {"step": "calculate_area", "output": "statistics"}
    method["steps"].append(follow_up)

    (issue,) = validate_method(method, registry=registry, registered_rules={"output_exists"})

    assert (issue.code, issue.field) == ("INPUT_TYPE_MISMATCH", "steps.1.inputs.source")
    assert "table_csv" in issue.message and "vector_polygon" in issue.message


def test_missing_extra_and_unknown_option_bindings_are_located(method, registry):
    parameters = method["steps"][0]["parameters"]
    del parameters["area_unit"]
    parameters["area_crs"]["option"] = "unknown_crs"
    parameters["extra"] = {"literal": "extra"}

    issues = validate_method(method, registry=registry, registered_rules={"output_exists"})

    assert _locations(issues) == [
        ("MISSING_PARAMETER_BINDING", "steps.0.parameters.area_unit"),
        ("UNKNOWN_OPTION", "steps.0.parameters.area_crs.option"),
        ("UNKNOWN_PARAMETER", "steps.0.parameters.extra"),
    ]


@pytest.mark.parametrize("binding", ["literal", "option"])
def test_known_literal_or_option_default_must_fit_parameter_contract(method, registry, binding):
    parameters = method["steps"][0]["parameters"]
    if binding == "literal":
        parameters["area_unit"] = {"literal": "square_kilometer"}
    else:
        method["critical_options"]["unit"] = {"type": "string", "default": "square_kilometer"}
        parameters["area_unit"] = {"option": "unit"}

    (issue,) = validate_method(method, registry=registry, registered_rules={"output_exists"})

    assert (issue.code, issue.field) == ("INVALID_PARAMETER_BINDING", f"steps.0.parameters.area_unit.{binding}")
    assert "enum" in issue.message


def test_contract_default_and_optional_parameter_can_be_omitted(method, area_contract, registry):
    updated = area_contract.model_copy(update={
        "version": 2,
        "parameters": {
            **area_contract.parameters,
            "area_unit": ParameterSpec(type="enum", values=("square_meter",), default="square_meter"),
            "label": ParameterSpec(type="string", required=False),
        },
    })
    registry.register(updated)
    step = method["steps"][0]
    step["tool"]["version"] = 2
    del step["parameters"]["area_unit"]

    assert validate_method(method, registry=registry, registered_rules={"output_exists"}) == ()


def test_artifact_role_binding_and_registered_rules_have_independent_issues(method, registry):
    artifact = method["expected_artifacts"][0]
    method["expected_artifacts"] = [
        "parcel_areas",
        artifact,
        {**artifact, "role": "bad_step", "step": "unlisted"},
        {**artifact, "role": "bad_port", "output": "unlisted"},
        {**artifact, "role": "bad_type", "type": "vector_polygon"},
    ]
    method["check_rules"].append("unregistered_check")

    issues = validate_method(method, registry=registry, registered_rules={"output_exists"})

    assert _locations(issues) == [
        ("MISSING_ARTIFACT_BINDING", "expected_artifacts.0"),
        ("DUPLICATE_ARTIFACT_ROLE", "expected_artifacts.1.role"),
        ("UNKNOWN_ARTIFACT_STEP", "expected_artifacts.2.step"),
        ("UNKNOWN_ARTIFACT_OUTPUT", "expected_artifacts.3.output"),
        ("ARTIFACT_TYPE_MISMATCH", "expected_artifacts.4.type"),
        ("UNKNOWN_CHECK_RULE", "check_rules.1"),
    ]


def test_road_structural_example_reports_which_bindings_are_missing(registry):
    directory = CONFIG / "methods"
    method = load_method_catalog(directory).get("road-buffer-statistics", 1)
    path = directory / "road-buffer-statistics.v1.yaml"

    issues = validate_method(method, registry=registry, registered_rules=method.check_rules, source=path)

    assert _locations(issues) == [
        *(("MISSING_TOOL_BINDING", f"steps.{index}.tool") for index in range(7)),
        *(("MISSING_ARTIFACT_BINDING", f"expected_artifacts.{index}") for index in range(3)),
    ]
    assert all(issue.path == path for issue in issues)


def test_real_qgis_registry_accepts_attribute_method_and_unit_bearing_literal(method, qgis_registry):
    attribute_filter = load_method_catalog(CONFIG / "methods").get("attribute-filter-export", 1)
    assert validate_method(attribute_filter, registry=qgis_registry, registered_rules={"output_exists"}) == ()

    method["inputs"] = {"roads": "vector_line"}
    method["steps"] = [{
        "key": "buffer_roads",
        "tool": {"id": "qgis-buffer", "version": 1},
        "inputs": {"source": {"asset": "roads"}},
        "parameters": {
            "distance": {"literal": {"value": 0.5, "unit": "kilometer"}},
            "segments": {"literal": 16},
            "dissolve": {"literal": True},
            "end_cap_style": {"literal": "round"},
            "join_style": {"literal": "round"},
            "miter_limit": {"literal": 2},
        },
        "outputs": {"buffer": "vector_polygon"},
    }]
    method["expected_artifacts"] = [
        {"role": "roads_buffer", "step": "buffer_roads", "output": "buffer", "type": "vector_polygon"},
    ]

    assert validate_method(method, registry=qgis_registry, registered_rules={"output_exists"}) == ()

    method["steps"][0]["parameters"]["distance"]["literal"]["unit"] = "square_meter"

    (issue,) = validate_method(method, registry=qgis_registry, registered_rules={"output_exists"})

    assert (issue.code, issue.field) == ("INVALID_PARAMETER_BINDING", "steps.0.parameters.distance.literal")
    assert "Cannot convert" in issue.message
