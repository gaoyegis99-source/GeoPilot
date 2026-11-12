from pathlib import Path

from geopilot.methods.loader import load_method_catalog
from geopilot.methods.models import MethodStep
from geopilot.plans.dependency_validator import DependencyIssue, validate_dependencies
from geopilot.plans.models import AnalysisPlan


CATALOG = Path(__file__).resolve().parents[2] / "config" / "methods"


def _step(key, inputs=None):
    return MethodStep.model_validate({
        "key": key,
        "inputs": inputs or {},
        "outputs": {"features": "vector_point"},
    })


def _from(step, output="features"):
    return {"step": step, "output": output}


def test_registered_method_catalog_declares_its_input_assets():
    method = load_method_catalog(CATALOG).get("attribute-filter-export", version=1)
    assert method.steps[0].inputs["source"].asset == "features"
    assert validate_dependencies(method.inputs, method.steps) == ()


def test_valid_method_dag_preserves_declaration_order_and_fan_out():
    steps = [
        _step("z_prepare", {"source": {"asset": "features"}}),
        _step("a_filter", {"source": _from("z_prepare")}),
        _step("b_combine", {"before": _from("z_prepare"), "after": _from("a_filter")}),
    ]
    before = [step.model_dump(mode="json") for step in steps]
    assert validate_dependencies(["features"], steps) == ()
    assert [step.model_dump(mode="json") for step in steps] == before


def test_same_validator_accepts_analysis_plan_steps_with_typed_output_targets():
    plan = AnalysisPlan.model_validate({
        "id": "plan-1", "task_id": "task-1", "version": 1, "status": "draft",
        "goal": {"summary": "读取并导出建筑点"},
        "inputs": {"features": {"asset_id": "asset-1", "asset_version": 2}},
        "critical_options": {},
        "method": {"id": "attribute-filter-export", "version": 1},
        "steps": [
            {
                "key": "filter", "tool": {"id": "qgis-extract-by-attribute", "version": 1},
                "inputs": {"source": {"asset": "features"}}, "parameters": {},
                "outputs": {"filtered": {"type": "vector_point", "target": "intermediate/filtered.gpkg"}},
            },
            {
                "key": "export", "tool": None,
                "inputs": {"source": _from("filter", "filtered")}, "parameters": {},
                "outputs": {"export": {"type": "vector_point", "target": "outputs/export.gpkg"}},
            },
        ],
        "expected_artifacts": [], "check_rules": [], "issues": [],
    })
    before = plan.model_dump_json()
    assert validate_dependencies(plan.inputs, plan.steps) == ()
    assert plan.model_dump_json() == before


def test_undeclared_references_report_ordered_precise_fields():
    steps = [
        _step("prepare", {"source": {"asset": "absent-asset"}}),
        _step("compute", {"left": _from("absent-step"), "right": _from("prepare", "absent-output")}),
    ]
    issues = validate_dependencies({"features": "vector_point"}, steps)
    assert isinstance(issues, tuple) and all(isinstance(issue, DependencyIssue) for issue in issues)
    assert [(issue.code, issue.field) for issue in issues] == [
        ("UNKNOWN_ASSET_ROLE", "steps.0.inputs.source.asset"),
        ("UNKNOWN_STEP", "steps.1.inputs.left.step"),
        ("UNKNOWN_OUTPUT", "steps.1.inputs.right.output"),
    ]
    for issue, missing in zip(issues, ("absent-asset", "absent-step", "absent-output"), strict=True):
        assert missing in issue.message


def test_duplicate_step_keys_report_the_later_declaration():
    issues = validate_dependencies([], [_step("prepare"), _step("prepare")])
    issue, = issues
    assert issue.code == "DUPLICATE_STEP_KEY" and issue.field == "steps.1.key"
    assert "prepare" in issue.message


def test_self_reference_is_rejected_at_its_input_port():
    issues = validate_dependencies([], [_step("prepare", {"source": _from("prepare")})])
    issue, = issues
    assert issue.code == "SELF_STEP_REFERENCE" and issue.field == "steps.0.inputs.source.step"
    assert "prepare" in issue.message


def test_acyclic_future_reference_is_rejected_without_reordering_steps():
    steps = [_step("consume", {"source": _from("prepare")}), _step("prepare")]
    issue, = validate_dependencies([], steps)
    assert issue.code == "FUTURE_STEP_REFERENCE" and issue.field == "steps.0.inputs.source.step"
    assert [step.key for step in steps] == ["consume", "prepare"]


def test_multi_step_cycle_is_distinguished_from_acyclic_forward_reference():
    steps = [
        _step("first", {"source": _from("third")}),
        _step("second", {"source": _from("first")}),
        _step("third", {"source": _from("second")}),
    ]
    issues = validate_dependencies([], steps)
    assert [(issue.code, issue.field) for issue in issues] == [
        ("FUTURE_STEP_REFERENCE", "steps.0.inputs.source.step"),
        ("DEPENDENCY_CYCLE", "steps.0.inputs.source.step"),
    ]
    assert all(key in issues[1].message for key in ("first", "second", "third"))


def test_missing_named_output_does_not_form_a_graph_edge():
    steps = [
        _step("first", {"source": _from("second", "missing")}),
        _step("second", {"source": _from("first")}),
    ]
    issue, = validate_dependencies([], steps)
    assert issue.code == "UNKNOWN_OUTPUT" and issue.field == "steps.0.inputs.source.output"
