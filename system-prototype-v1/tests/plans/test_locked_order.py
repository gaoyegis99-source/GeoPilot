from pathlib import Path

import pytest

from geopilot.methods.loader import load_method_catalog
from geopilot.methods.models import MethodDefinition, MethodStep
from geopilot.plans.models import AnalysisPlan, PlanStep
from geopilot.plans.step_compiler import validate_step_order


CATALOG = Path(__file__).resolve().parents[2] / "config" / "methods"
ROAD_ORDER = (
    "expand_analysis_extent",
    "select_roads_from_expanded_extent",
    "choose_measurement_method",
    "create_buffer",
    "apply_spatial_relation",
    "clip_back_to_analysis_extent",
    "aggregate_by_zone",
)


@pytest.fixture
def road_method():
    return load_method_catalog(CATALOG).get("road-buffer-statistics", version=1)


def test_actual_road_template_passes_with_exact_seven_step_order(road_method):
    assert road_method.locked_order is True
    assert tuple(step.key for step in road_method.steps) == ROAD_ORDER
    assert validate_step_order(road_method, iter(road_method.steps)) == ()


@pytest.mark.parametrize(
    "change,index",
    [("swap", 1), ("omit", 2), ("insert", 2), ("truncate", 6), ("append", 7)],
)
def test_locked_order_rejects_first_divergence_and_keeps_candidate_unchanged(road_method, change, index):
    steps = list(road_method.steps)
    unexpected = MethodStep(key="model_added_step")
    if change == "swap":
        steps[1], steps[2] = steps[2], steps[1]
    elif change == "omit":
        steps.pop(2)
    elif change == "insert":
        steps.insert(2, unexpected)
    elif change == "truncate":
        steps.pop()
    else:
        steps.append(unexpected)

    before = tuple(step.key for step in steps)
    issue, = validate_step_order(road_method, steps)
    assert issue.code == "LOCKED_STEP_ORDER"
    assert issue.field == f"steps.{index}.key"
    if index < len(ROAD_ORDER):
        assert ROAD_ORDER[index] in issue.message
    if index < len(before):
        assert before[index] in issue.message
    assert tuple(step.key for step in steps) == before
    assert tuple(step.key for step in road_method.steps) == ROAD_ORDER


def _road_plan(method):
    steps = [{"key": key, "tool": None, "inputs": {}, "parameters": {}, "outputs": {}} for key in ROAD_ORDER]
    steps[2]["outputs"] = {"roads": {"type": "vector_line", "target": "intermediate/roads.gpkg"}}
    steps[3].update(
        tool={"id": "qgis-buffer", "version": 1},
        inputs={"source": {"step": "choose_measurement_method", "output": "roads"}},
        outputs={"buffer": {"type": "vector_polygon", "target": "intermediate/buffer.gpkg"}},
    )
    return AnalysisPlan.model_validate({
        "id": "plan-1", "task_id": "task-1", "version": 1, "status": "draft",
        "goal": {"summary": "按道路固定步骤计算缓冲"},
        "inputs": {"roads": {"asset_id": "road-1", "asset_version": 1}},
        "critical_options": {"distance_m": 500}, "method": method.ref.model_dump(mode="json"),
        "steps": steps, "expected_artifacts": [], "check_rules": [], "issues": [],
    })


def test_order_check_reuses_dependencies_for_a_real_plan_shape(road_method):
    plan = _road_plan(road_method)
    assert validate_step_order(road_method, plan.steps) == ()

    steps = list(plan.steps)
    invalid = steps[3].model_dump(mode="json")
    invalid["inputs"]["source"]["output"] = "absent"
    steps[3] = PlanStep.model_validate(invalid)
    issue, = validate_step_order(road_method, steps)
    assert (issue.code, issue.field) == ("UNKNOWN_OUTPUT", "steps.3.inputs.source.output")

    steps[1], steps[2] = steps[2], steps[1]
    issues = validate_step_order(road_method, steps)
    assert [issue.code for issue in issues] == ["LOCKED_STEP_ORDER", "UNKNOWN_OUTPUT"]
    assert [step.key for step in plan.steps] == list(ROAD_ORDER)


def test_unlocked_method_retains_valid_alternate_order_and_still_checks_dependencies():
    method = MethodDefinition.model_validate({
        "id": "independent-branches", "version": 1, "locked_order": False,
        "inputs": {"features": "vector_point"}, "critical_options": {},
        "steps": [
            {"key": "prepare", "inputs": {"source": {"asset": "features"}}, "outputs": {"features": "vector_point"}},
            {"key": "left", "inputs": {"source": {"step": "prepare", "output": "features"}}},
            {"key": "right", "inputs": {"source": {"step": "prepare", "output": "features"}}},
        ],
        "expected_artifacts": [], "check_rules": [],
    })
    prepare, left, right = method.steps
    candidate = [prepare, right, left]
    assert validate_step_order(method, candidate) == ()
    assert [step.key for step in candidate] == ["prepare", "right", "left"]
    issue, = validate_step_order(method, [right, prepare, left])
    assert (issue.code, issue.field) == ("FUTURE_STEP_REFERENCE", "steps.0.inputs.source.step")
