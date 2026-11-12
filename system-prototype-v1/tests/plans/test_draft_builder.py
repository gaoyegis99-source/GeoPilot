from pathlib import Path

import pytest
import yaml

from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import load_method_catalog
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.schema import initialize_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.models import AnalysisPlan, PlanOutput, PlanStatus
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
VECTOR_FIXTURE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"


@pytest.fixture
def assets(tmp_path):
    database = tmp_path / "assets.sqlite"
    initialize_database(database)
    roads = register_asset(database, VECTOR_FIXTURE, layer="roads").ref
    targets = register_asset(database, VECTOR_FIXTURE, layer="targets").ref
    return database, roads, targets


def _context(database, method, selected):
    return build_task_context(
        database, task_id="task-23", goal=TaskGoal(summary="检查明确选择的数据"),
        method=method, selected=selected,
    )


def _method(**fields):
    definition = {
        "id": "inspect-data", "version": 4, "locked_order": True,
        "inputs": {"features": "vector_point"}, "critical_options": {},
        "steps": [{"key": "inspect", "tool": {"id": "inspect", "version": 2}}],
        "expected_artifacts": [], "check_rules": [],
    }
    return MethodDefinition.model_validate({**definition, **fields})


def _issues(plan):
    return {(issue.code, issue.field) for issue in plan.issues}


def test_catalog_method_expands_a_real_selection_and_explicit_parameters(assets):
    database, _, targets = assets
    method = load_method_catalog(ROOT / "config/methods").get("attribute-filter-export", 1)
    context = _context(database, method, {"features": targets})

    draft = build_draft(context, method, plan_id="plan-23", version=3,
                        options={"field_name": "source_feature_id", "match_value": "target-01"})

    assert (draft.id, draft.task_id, draft.version, draft.status) == ("plan-23", "task-23", 3, PlanStatus.DRAFT)
    assert draft.goal == context.goal and draft.method == method.ref
    assert draft.inputs == {"features": targets}
    assert draft.issues == ()
    step, = draft.steps
    assert step.key == "filter_features" and step.tool == method.steps[0].tool
    assert step.inputs == method.steps[0].inputs
    assert step.parameters == {"field": "source_feature_id", "operator": "equals", "value": "target-01"}
    assert step.outputs["filtered"] == PlanOutput(
        type="vector_point", target="intermediate/filter_features/filtered.gpkg", layer="filtered"
    )
    assert draft.expected_artifacts == method.expected_artifacts
    assert draft.check_rules == method.check_rules
    assert AnalysisPlan.model_validate_json(draft.model_dump_json()) == draft


def test_partial_road_template_keeps_explicit_blocked_asset_and_never_selects_business_options(assets):
    database, roads, _ = assets
    method = load_method_catalog(ROOT / "config/methods").get("road-buffer-statistics", 1)
    context = _context(database, method, {"roads": roads, "targets": roads})
    provided = {"distance_m": 500, "distance_method": "local_projected"}

    draft = build_draft(context, method, plan_id="road-draft", options=provided)

    assert draft.version == 1 and draft.inputs == {"roads": roads, "targets": roads}
    assert draft.critical_options == provided
    assert [step.key for step in draft.steps] == [step.key for step in method.steps]
    assert all(step.tool is None and not step.parameters for step in draft.steps)
    assert draft.expected_artifacts == () and draft.check_rules == method.check_rules
    issues = _issues(draft)
    assert ("METHOD_PRECONDITION_FAILED", "inputs.targets") in issues
    assert ("INPUT_REQUIRED", "inputs.analysis_extent") in issues
    assert ("OPTION_REQUIRED", "critical_options.distance_crs_ref") in issues
    assert ("OPTION_REQUIRED", "critical_options.boundary_assignment") in issues
    assert ("OPTION_REQUIRED", "critical_options.area_crs_ref") not in issues
    assert ("TOOL_REQUIRED", "steps.3.tool") in issues
    assert ("ARTIFACT_UNBOUND", "expected_artifacts.0") in issues


def test_order_defaults_literals_and_prior_output_are_preserved(assets):
    database, _, targets = assets
    method = _method(critical_options={
        "limit": {"type": "integer", "default": 0},
        "include_empty": {"type": "boolean", "default": False},
        "metadata": {"type": "json", "required": False},
        "optional": {"type": "string", "required": False},
    }, steps=[
        {"key": "prepare", "tool": {"id": "prepare", "version": 5},
         "inputs": {"features": {"asset": "features"}},
         "parameters": {"limit": {"option": "limit"}, "settings": {"literal": {"keys": [None, True]}}},
         "outputs": {"points": "vector_point"}},
        {"key": "collect", "tool": {"id": "collect", "version": 2},
         "inputs": {"points": {"step": "prepare", "output": "points"}},
         "parameters": {"include_empty": {"option": "include_empty"}, "note": {"option": "metadata"}},
         "outputs": {"statistics": "table_csv"}},
    ])
    context = _context(database, method, {"features": targets})

    draft = build_draft(context, method, plan_id="ordered", options={"metadata": None})

    assert draft.issues == ()
    assert draft.critical_options == {"limit": 0, "include_empty": False, "metadata": None}
    assert [step.key for step in draft.steps] == ["prepare", "collect"]
    assert [step.tool.version for step in draft.steps] == [5, 2]
    assert draft.steps[0].parameters == {"limit": 0, "settings": {"keys": [None, True]}}
    assert draft.steps[1].parameters == {"include_empty": False, "note": None}
    assert draft.steps[1].inputs["points"] == method.steps[1].inputs["points"]
    assert draft.steps[1].outputs["statistics"].target == "intermediate/collect/statistics.csv"


def test_reference_requirements_follow_declarations_and_step_bindings(assets):
    database, _, targets = assets
    method = _method(critical_options={
        "strategy": ["automatic", "specified"],
        "target_ref": {"type": "reference", "required_for": "specified"},
        "always_ref": {"type": "reference"},
        "step_ref": {"type": "reference", "required_for": "another_strategy"},
    }, steps=[{"key": "inspect", "tool": {"id": "inspect", "version": 2},
               "parameters": {"source_ref": {"option": "step_ref"}}}])
    context = _context(database, method, {"features": targets})

    automatic = build_draft(context, method, plan_id="automatic", options={"strategy": "automatic"})
    assert ("OPTION_REQUIRED", "critical_options.target_ref") not in _issues(automatic)
    assert ("OPTION_REQUIRED", "critical_options.always_ref") in _issues(automatic)
    assert ("OPTION_REQUIRED", "critical_options.step_ref") in _issues(automatic)
    assert automatic.steps[0].parameters == {}

    specified = build_draft(context, method, plan_id="specified", options={
        "strategy": "specified", "target_ref": "  ", "always_ref": "collection-04", "step_ref": "crs-17",
    })
    assert ("INVALID_OPTION", "critical_options.target_ref") in _issues(specified)
    assert "target_ref" not in specified.critical_options
    assert specified.steps[0].parameters == {"source_ref": "crs-17"}

    missing = build_draft(context, method, plan_id="missing", options={"strategy": "specified"})
    assert ("OPTION_REQUIRED", "critical_options.target_ref") in _issues(missing)


@pytest.mark.parametrize("value", [False, "12", 0, float("inf"), {"value": object()}])
def test_invalid_values_do_not_fall_back_or_leak_to_step_parameters(assets, value):
    database, _, targets = assets
    method = _method(critical_options={"distance": {"type": "number", "minimum_exclusive": 0, "default": 5}},
                     steps=[{"key": "inspect", "tool": {"id": "inspect", "version": 1},
                             "parameters": {"distance": {"option": "distance"}, "extra": {"option": "extra"}}}])
    context = _context(database, method, {"features": targets})

    draft = build_draft(context, method, plan_id="invalid", options={"distance": value, "extra": 4})

    assert not draft.critical_options and not draft.steps[0].parameters
    assert ("INVALID_OPTION", "critical_options.distance") in _issues(draft)
    assert ("UNKNOWN_OPTION", "critical_options.extra") in _issues(draft)
    assert ("UNKNOWN_OPTION", "steps.0.parameters.extra.option") in _issues(draft)


def test_output_formats_paths_and_unresolved_bindings_are_visible(assets):
    database, _, targets = assets
    method = _method(steps=[
        {"key": "../produce|成果", "tool": {"id": "produce", "version": 1},
         "outputs": {"left/edge": "vector_polygon", "left edge": "vector_polygon", "grid": "raster_mask",
                     "log": "json", "table": "table", "unknown": "animation"}},
        {"key": "consume", "tool": {"id": "consume", "version": 1},
         "inputs": {"source": {"step": "../produce|成果", "output": "missing"}}},
    ], expected_artifacts=[
        {"role": "edge", "step": "../produce|成果", "output": "left/edge", "type": "vector_polygon"},
        {"role": "absent", "step": "../produce|成果", "output": "unknown", "type": "animation"},
        {"role": "wrong_type", "step": "../produce|成果", "output": "grid", "type": "vector_polygon"},
    ])
    context = _context(database, method, {"features": targets})

    draft = build_draft(context, method, plan_id="outputs")
    repeated = build_draft(context, method, plan_id="same-output-layout", version=2)

    outputs = draft.steps[0].outputs
    assert outputs == repeated.steps[0].outputs
    assert list(outputs) == ["left/edge", "left edge", "grid", "log", "table"]
    assert len({output.target for output in outputs.values()}) == 5
    assert [Path(output.target).suffix for output in outputs.values()] == [".gpkg", ".gpkg", ".tif", ".json", ".csv"]
    assert all(output.target.startswith("intermediate/") and ".." not in output.target for output in outputs.values())
    assert all(output.layer and "/" not in output.layer for output in list(outputs.values())[:2])
    assert all(output.layer is None for output in list(outputs.values())[2:])
    assert draft.expected_artifacts == method.expected_artifacts
    assert ("UNSUPPORTED_OUTPUT_TYPE", "steps.0.outputs.unknown") in _issues(draft)
    assert ("UNKNOWN_OUTPUT", "steps.1.inputs.source.output") in _issues(draft)
    assert ("ARTIFACT_UNBOUND", "expected_artifacts.1.output") in _issues(draft)
    assert ("ARTIFACT_TYPE_MISMATCH", "expected_artifacts.2.type") in _issues(draft)


def test_context_for_a_different_method_version_is_rejected(assets):
    database, _, targets = assets
    method = _method()
    context = _context(database, method, {"features": targets})

    with pytest.raises(ValueError, match="method"):
        build_draft(context, _method(version=5), plan_id="stale")


def test_draft_preserves_order_while_reporting_future_output(assets):
    database, _, targets = assets
    method = _method(steps=[
        {"key": "consume", "tool": {"id": "consume", "version": 1},
         "inputs": {"source": {"step": "produce", "output": "points"}}},
        {"key": "produce", "tool": {"id": "produce", "version": 1},
         "outputs": {"points": "vector_point"}},
    ])
    context = _context(database, method, {"features": targets})

    draft = build_draft(context, method, plan_id="future")

    assert [step.key for step in draft.steps] == ["consume", "produce"]
    assert ("FUTURE_STEP_REFERENCE", "steps.0.inputs.source.step") in _issues(draft)


def test_real_conditional_asset_keeps_its_exact_version_and_reason(assets):
    database, _, _ = assets
    zones = register_asset(database, VECTOR_FIXTURE, layer="zones").ref
    contract = ToolContract.model_validate(
        yaml.safe_load((ROOT / "config/tools/python-area.yaml").read_text(encoding="utf-8"))
    )
    registry = ToolRegistry(python_implementations=[contract.implementation])
    registry.register(contract)
    method = _method(inputs={"features": "vector_polygon"}, steps=[
        {"key": "area", "tool": contract.ref, "inputs": {"source": {"asset": "features"}}},
    ])
    context = build_task_context(
        database, task_id="area-task", goal=TaskGoal(summary="计算图斑面积"),
        method=method, selected={"features": zones}, registry=registry,
    )

    draft = build_draft(context, method, plan_id="area-draft")

    assert context.roles["features"].status == "CONDITIONAL"
    assert draft.inputs == {"features": zones}
    issue, = draft.issues
    assert (issue.code, issue.field) == ("MEASUREMENT_POLICY_REJECTED", "inputs.features")
    assert issue.message == context.roles["features"].message
