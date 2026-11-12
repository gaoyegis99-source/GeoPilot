import json

import pytest
import yaml
from pydantic import ValidationError

from geopilot.domain.data_asset import DataAssetRef
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolRef
from geopilot.methods.models import MethodRef
from geopilot.plans.models import AnalysisPlan, PlanIssue, PlanOutput, PlanStatus, PlanStep


ROAD_PLAN = """
id: plan-001
task_id: task-001
version: 1
status: draft
goal: {summary: 道路500米范围内建筑物分区计数}
inputs:
  roads: {asset_id: road-001, asset_version: 1}
  targets: {asset_id: building-001, asset_version: 1}
  zones: {asset_id: zone-001, asset_version: 1}
  analysis_extent: {asset_id: extent-001, asset_version: 1}
critical_options:
  distance_m: 500
  distance_method: local_projected
  distance_crs_ref: local_crs_001
  spatial_relation: intersects
  boundary_assignment: centroid
  boundary_tie: unassigned
  statistic: count
  output_scope: clipped_to_analysis_extent
method: {id: road-buffer-statistics, version: 1}
steps:
  - key: create_buffer
    tool: {id: qgis-buffer, version: 1}
    inputs:
      source: {step: choose_measurement_method, output: roads}
    parameters:
      distance: 500
      segments: 8
      dissolve: true
      end_cap_style: round
      join_style: round
      miter_limit: 2
    outputs:
      buffer: {type: vector_polygon, target: intermediate/buffer.gpkg}
expected_artifacts:
  - {role: buffer_extent, step: create_buffer, output: buffer, type: vector_polygon}
  - {role: selected_targets, step: clip_back_to_analysis_extent, output: targets, type: vector_point}
  - {role: zone_statistics, step: aggregate_by_zone, output: statistics, type: table}
check_rules:
  - measurement_policy_satisfied
  - outside_roads_within_influence_included
  - output_clipped_to_analysis_extent
  - detail_count_matches_summary
issues: []
"""


@pytest.fixture
def draft():
    return yaml.safe_load(ROAD_PLAN)


@pytest.fixture
def confirmed(draft):
    draft["status"] = "confirmed"
    draft["steps"].insert(0, {
        "key": "choose_measurement_method",
        "tool": {"id": "prepare-roads", "version": 2},
        "inputs": {"source": {"asset": "roads"}},
        "parameters": {"method": "local_projected", "crs_ref": "local_crs_001"},
        "outputs": {"roads": {"type": "vector_line", "target": "intermediate/roads.gpkg", "layer": "roads"}},
    })
    draft["expected_artifacts"] = draft["expected_artifacts"][:1]
    return draft


def test_v2_plan_shape_reuses_versioned_references_and_round_trips(draft):
    plan = AnalysisPlan.model_validate(draft)
    assert plan.id == "plan-001" and plan.task_id == "task-001" and plan.version == 1
    assert plan.status == PlanStatus("draft")
    assert plan.goal == TaskGoal(summary="道路500米范围内建筑物分区计数")
    assert plan.inputs["roads"] == DataAssetRef(asset_id="road-001", asset_version=1)
    assert plan.method == MethodRef(id="road-buffer-statistics", version=1)
    assert plan.critical_options == draft["critical_options"]

    step, = plan.steps
    assert isinstance(step, PlanStep)
    assert step.tool == ToolRef(id="qgis-buffer", version=1)
    assert step.outputs["buffer"] == PlanOutput(type="vector_polygon", target="intermediate/buffer.gpkg")
    assert step.parameters["dissolve"] is True
    assert list(plan.check_rules) == draft["check_rules"]
    assert [artifact.role for artifact in plan.expected_artifacts] == ["buffer_extent", "selected_targets", "zone_statistics"]

    saved = json.loads(plan.model_dump_json())
    assert saved["steps"][0]["inputs"] == {"source": {"step": "choose_measurement_method", "output": "roads"}}
    assert saved["expected_artifacts"] == draft["expected_artifacts"]
    assert AnalysisPlan.model_validate_json(plan.model_dump_json()) == plan


def test_confirmed_snapshot_preserves_order_and_complete_tool_versions(confirmed):
    plan = AnalysisPlan.model_validate(confirmed)
    assert plan.status == PlanStatus("confirmed") and not plan.issues
    assert [step.key for step in plan.steps] == ["choose_measurement_method", "create_buffer"]
    assert [step.tool for step in plan.steps] == [ToolRef(id="prepare-roads", version=2), ToolRef(id="qgis-buffer", version=1)]
    assert plan.model_dump(mode="json")["steps"][0]["inputs"] == {"source": {"asset": "roads"}}
    assert AnalysisPlan.model_validate_json(plan.model_dump_json()) == plan


def test_draft_can_record_incomplete_inputs_unbound_steps_and_issues(draft):
    draft["inputs"] = {"roads": draft["inputs"]["roads"]}
    draft["critical_options"] = {}
    draft["steps"][0]["tool"] = None
    draft["issues"] = [{"code": "MISSING_OPTION", "field": "critical_options.distance_m", "message": "填写缓冲距离"}]

    plan = AnalysisPlan.model_validate(draft)
    assert plan.steps[0].tool is None
    assert list(plan.inputs) == ["roads"]
    assert not plan.critical_options
    assert list(plan.issues) == [PlanIssue(code="MISSING_OPTION", field="critical_options.distance_m", message="填写缓冲距离")]
    assert AnalysisPlan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.parametrize("incomplete", ["no-steps", "unbound-tool", "unresolved-issues"])
def test_confirmed_snapshot_refuses_structurally_incomplete_draft(confirmed, incomplete):
    if incomplete == "no-steps":
        confirmed["steps"] = []
    elif incomplete == "unbound-tool":
        confirmed["steps"][0]["tool"] = None
    else:
        confirmed["issues"] = [{"code": "MISSING_INPUT", "field": "inputs.roads", "message": "缺少道路数据"}]
    with pytest.raises(ValidationError):
        AnalysisPlan.model_validate(confirmed)


@pytest.mark.parametrize("field", ["id", "task_id", "version", "status", "goal", "inputs", "method"])
def test_required_identity_and_reference_fields(draft, field):
    del draft[field]
    with pytest.raises(ValidationError):
        AnalysisPlan.model_validate(draft)


@pytest.mark.parametrize("version", [0, -1, 1.0, "1", True])
def test_plan_version_is_strict_positive_integer(draft, version):
    draft["version"] = version
    with pytest.raises(ValidationError):
        AnalysisPlan.model_validate(draft)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("status", "executing"),
        ("goal", {"summary": 42}),
        ("goal", {"summary": "valid", "location": "not part of TaskGoal"}),
        ("inputs", {"roads": {"asset_id": "road-001"}}),
        ("inputs", {"roads": {"asset_id": "road-001", "asset_version": "1"}}),
        ("method", {"id": "road-buffer-statistics", "version": True}),
        ("critical_options", {"weights": [float("inf")]}),
        ("expected_artifacts", [{"role": "buffer_extent", "step": "create_buffer", "type": "vector_polygon"}]),
        ("check_rules", {"measurement_policy_satisfied": True}),
        ("issues", [{"code": "MISSING_INPUT", "message": "缺少字段位置"}]),
        ("distance_m", 500),
    ],
)
def test_plan_rejects_wrong_types_unversioned_references_and_extra_fields(draft, field, bad):
    draft[field] = bad
    with pytest.raises(ValidationError):
        AnalysisPlan.model_validate(draft)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("tool", {"id": "qgis-buffer", "version": 1.0}),
        ("inputs", {"source": {"asset": "roads", "step": "prepare", "output": "roads"}}),
        ("inputs", {"source": {"step": "prepare"}}),
        ("parameters", {"nested": {"invalid": float("nan")}}),
    ],
)
def test_step_rejects_invalid_tool_binding_or_json_parameters(draft, field, bad):
    step = draft["steps"][0]
    step[field] = bad
    with pytest.raises(ValidationError):
        PlanStep.model_validate(step)


@pytest.mark.parametrize(
    "path", ["/tmp/buffer.gpkg", "../buffer.gpkg", "intermediate/../buffer.gpkg", "C:\\buffer.gpkg",
             "https://example.invalid/buffer.gpkg", "intermediate//buffer.gpkg", 5],
)
def test_output_target_is_a_local_relative_run_path(path):
    with pytest.raises(ValidationError):
        PlanOutput(type="vector_polygon", target=path)
