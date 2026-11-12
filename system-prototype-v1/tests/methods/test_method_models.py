import pytest
import yaml
from pydantic import ValidationError

from geopilot.domain.tool_contract import ParameterSpec, ToolRef
from geopilot.methods.models import MethodDefinition, MethodRef, MethodStep


ROAD_METHOD = """
id: road-buffer-statistics
version: 1
locked_order: true
inputs:
  analysis_extent: vector_polygon
  roads: vector_line
  targets: vector_point_or_polygon
  zones: vector_polygon
critical_options:
  distance_m: {type: number, minimum_exclusive: 0}
  distance_method: [local_projected, geodesic]
  distance_crs_ref: {type: reference, required_for: local_projected}
  area_crs_ref: {type: reference, required_for: area_statistics}
  spatial_relation: [intersects, within]
  boundary_assignment: [split_by_area, centroid, largest_overlap]
  boundary_tie: [unassigned]
  statistic: [count, area]
  output_scope: [clipped_to_analysis_extent]
preconditions:
  - required_input_roles_available
  - selected_options_match_geometry_type
steps:
  - {key: expand_analysis_extent}
  - {key: select_roads_from_expanded_extent}
  - {key: choose_measurement_method}
  - {key: create_buffer}
  - {key: apply_spatial_relation}
  - {key: clip_back_to_analysis_extent}
  - {key: aggregate_by_zone}
expected_artifacts:
  - buffer_extent
  - selected_targets
  - zone_statistics
check_rules:
  - measurement_policy_satisfied
  - outside_roads_within_influence_included
  - output_clipped_to_analysis_extent
  - detail_count_matches_summary
"""

REMOTE_SENSING_METHOD = """
id: remote-sensing-index-change
version: 1
locked_order: true
inputs:
  first_product: raster_product
  second_product: raster_product
  analysis_extent: vector_polygon
critical_options:
  resampling: user_selected
  change_threshold: user_defined
steps:
  - {key: read_product_metadata}
  - {key: lookup_sensor_configuration}
  - {key: map_band_roles}
  - {key: apply_scale_and_offset}
  - {key: build_quality_mask}
  - {key: calculate_ndvi}
  - {key: align_two_dates}
  - {key: calculate_change}
expected_artifacts: [first_ndvi, second_ndvi, change_raster]
preconditions:
  - sensor_configuration_is_unique
  - required_bands_exist
  - scale_and_offset_are_known
check_rules:
  - product_configuration_matches
  - common_valid_pixels_aligned
  - change_direction_is_second_minus_first
"""


@pytest.fixture
def road_template():
    return yaml.safe_load(ROAD_METHOD)


def test_v2_road_method_preserves_order_and_reuses_parameter_contract(road_template):
    method = MethodDefinition.model_validate(road_template)
    assert MethodRef(id=method.id, version=method.version) == MethodRef(id="road-buffer-statistics", version=1)
    assert method.locked_order is True
    assert method.inputs == road_template["inputs"]
    assert all(isinstance(step, MethodStep) for step in method.steps)
    assert [step.key for step in method.steps] == [step["key"] for step in road_template["steps"]]
    assert list(method.preconditions) == road_template["preconditions"]
    assert list(method.expected_artifacts) == road_template["expected_artifacts"]
    assert list(method.check_rules) == road_template["check_rules"]

    distance = method.critical_options["distance_m"]
    assert isinstance(distance, ParameterSpec)
    distance.validate_value(500)
    with pytest.raises(ValueError):
        distance.validate_value(0)
    configured = method.model_dump(mode="json")["critical_options"]
    assert configured["distance_crs_ref"] == {"type": "reference", "required_for": "local_projected"}
    assert configured["area_crs_ref"] == {"type": "reference", "required_for": "area_statistics"}
    relation = method.critical_options["spatial_relation"]
    choices = relation.values if isinstance(relation, ParameterSpec) else relation
    assert list(choices) == ["intersects", "within"]
    assert MethodDefinition.model_validate_json(method.model_dump_json()) == method


def test_different_method_accepts_both_example_placeholders_and_omitted_preconditions():
    template = yaml.safe_load(REMOTE_SENSING_METHOD)
    method = MethodDefinition.model_validate(template)
    assert method.id == "remote-sensing-index-change"
    assert list(method.inputs) == ["first_product", "second_product", "analysis_extent"]
    assert method.critical_options == {"resampling": "user_selected", "change_threshold": "user_defined"}
    assert [step.key for step in method.steps] == [step["key"] for step in template["steps"]]
    assert list(method.preconditions) == template["preconditions"]
    assert list(method.expected_artifacts) == ["first_ndvi", "second_ndvi", "change_raster"]
    assert list(method.check_rules) == template["check_rules"]
    assert MethodDefinition.model_validate_json(method.model_dump_json()) == method

    del template["preconditions"]
    assert not MethodDefinition.model_validate(template).preconditions


def test_executable_template_bindings_reuse_tool_ref_and_remain_serializable(road_template):
    configured_steps = [
        {
            "key": "prepare_measurement_inputs",
            "tool": {"id": "prepare-roads", "version": 2},
            "inputs": {"features": {"asset": "roads"}},
            "parameters": {"projection": {"option": "distance_crs_ref"}},
            "outputs": {"projected": "vector_line"},
        },
        {
            "key": "create_buffer",
            "tool": {"id": "qgis-buffer", "version": 1},
            "inputs": {"source": {"step": "prepare_measurement_inputs", "output": "projected"}},
            "parameters": {"distance": {"option": "distance_m"}, "segments": {"literal": 16},
                           "dissolve": {"literal": True}},
            "outputs": {"buffer": "vector_polygon"},
        },
    ]
    road_template["steps"] = configured_steps
    artifact = {"role": "buffer_extent", "step": "create_buffer", "output": "buffer", "type": "vector_polygon"}
    road_template["expected_artifacts"] = [artifact, "zone_statistics"]

    method = MethodDefinition.model_validate(road_template)
    assert [step.tool for step in method.steps] == [ToolRef(id="prepare-roads", version=2), ToolRef(id="qgis-buffer", version=1)]
    saved = method.model_dump(mode="json")
    assert saved["steps"] == configured_steps
    assert saved["expected_artifacts"] == [artifact, "zone_statistics"]
    assert MethodDefinition.model_validate_json(method.model_dump_json()) == method


@pytest.mark.parametrize(
    "field", ["id", "version", "locked_order", "inputs", "critical_options", "steps", "expected_artifacts", "check_rules"],
)
def test_required_method_fields(road_template, field):
    del road_template[field]
    with pytest.raises(ValidationError):
        MethodDefinition.model_validate(road_template)


@pytest.mark.parametrize("version", [0, -1, 1.0, "1", True])
def test_strict_positive_version_in_both_method_and_reference(road_template, version):
    road_template["version"] = version
    with pytest.raises(ValidationError):
        MethodDefinition.model_validate(road_template)
    with pytest.raises(ValidationError):
        MethodRef(id="method", version=version)


@pytest.mark.parametrize("invalid", [1, "true", None])
def test_locked_order_requires_actual_boolean(road_template, invalid):
    road_template["locked_order"] = invalid
    with pytest.raises(ValidationError):
        MethodDefinition.model_validate(road_template)


def test_unknown_business_fields_are_rejected(road_template):
    road_template["distance_m"] = 500
    with pytest.raises(ValidationError):
        MethodDefinition.model_validate(road_template)
    with pytest.raises(ValidationError):
        MethodStep.model_validate({"key": "clip", "clip_distance_m": 500})


@pytest.mark.parametrize(
    "extra",
    [
        {"inputs": {"source": {"asset": "roads", "step": "prepare", "output": "roads"}}},
        {"inputs": {"source": {"step": "prepare"}}},
        {"parameters": {"distance": {"option": "distance_m", "literal": 500}}},
        {"parameters": {"distance": 500}},
        {"parameters": {"distance": {"literal": {"value": object()}}}},
        {"outputs": {"buffer": 3}},
        {"tool": {"id": "qgis-buffer", "version": True}},
    ],
)
def test_step_bindings_must_match_one_explicit_structure(extra):
    with pytest.raises(ValidationError):
        MethodStep.model_validate({"key": "buffer", **extra})


@pytest.mark.parametrize("option", ["unknown_placeholder", [], ["count", "count"], {"type": "number", "default": "500"}])
def test_critical_options_reject_unsupported_shapes(road_template, option):
    road_template["critical_options"]["sample"] = option
    with pytest.raises(ValidationError):
        MethodDefinition.model_validate(road_template)


def test_expected_artifact_binding_requires_step_output_and_type(road_template):
    road_template["expected_artifacts"] = [{"role": "buffer_extent", "step": "create_buffer"}]
    with pytest.raises(ValidationError):
        MethodDefinition.model_validate(road_template)
