import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pyogrio
import pytest
import yaml

from geopilot.application.method_preconditions import METHOD_PRECONDITIONS
from geopilot.application.task_context import TaskContext, build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import MethodCatalog, load_method_catalog
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.models import AnalysisPlan, PlanIssue, PlanStatus
from geopilot.plans.service import PlanConfirmationError, confirm_plan, load_plan, save_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"


def _contract(tool):
    return ToolContract.model_validate(yaml.safe_load((ROOT / f"config/tools/{tool}.yaml").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def registry():
    bundled = ROOT / ".environments/qgis-deb/qgis_process"
    executable = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if executable is None and bundled.is_file():
        executable = str(bundled)
    if executable is None:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS for actual tool discovery")
    registry = ToolRegistry(qgis_process=executable)
    for tool in ("qgis-extract-by-attribute", "qgis-buffer"):
        registry.register(_contract(tool))
    return registry


@pytest.fixture
def draft(tmp_path):
    database = tmp_path / "analysis.sqlite"
    initialize_database(database)
    source = tmp_path / "features.gpkg"
    shutil.copyfile(SAMPLE, source)
    asset = register_asset(database, source, layer="targets")
    catalog = load_method_catalog(ROOT / "config/methods")
    method = catalog.get("attribute-filter-export", 1)
    context = build_task_context(
        database, task_id="task-confirmation", goal=TaskGoal(summary="筛选明确指定的目标"),
        method=method, selected={"features": asset.ref},
    )
    plan = build_draft(context, method, plan_id="plan-confirmation",
                       options={"field_name": "source_feature_id", "match_value": "building-west"})
    assert plan.issues == ()
    return database, source, catalog, context, plan


def _row(database, plan):
    with open_database(database) as connection:
        row = connection.execute("SELECT * FROM analysis_plan WHERE id = ? AND version = ?",
                                 (plan.id, plan.version)).fetchone()
    return dict(row)


def _confirm(draft, registry, **overrides):
    database, _, catalog, _, plan = draft
    options = dict(catalog=catalog, registry=registry, registered_rules={"output_exists"},
                   precondition_checks=METHOD_PRECONDITIONS)
    return confirm_plan(database, plan.id, plan.version, **{**options, **overrides})


def test_save_confirm_and_reopen_exact_sqlite_snapshot_without_execution(draft, registry):
    database, source, _, context, plan = draft
    original = source.read_bytes()
    save_draft(database, plan, context=context)
    saved = _row(database, plan)
    assert saved["status"] == "draft" and load_plan(database, plan.id, 1) == plan
    with open_database(database) as connection:
        task = connection.execute("SELECT goal_json, context_json FROM task WHERE id = ?", (plan.task_id,)).fetchone()
    assert TaskGoal.model_validate_json(task["goal_json"]) == plan.goal
    assert TaskContext.model_validate_json(task["context_json"]) == context

    confirmed = _confirm(draft, registry)

    snapshot = _row(database, plan)
    assert snapshot["status"] == "confirmed" and snapshot["created_at"] == saved["created_at"]
    assert AnalysisPlan.model_validate_json(snapshot["plan_json"]) == confirmed == load_plan(database, plan.id, 1)
    assert confirmed.status is PlanStatus.CONFIRMED and confirmed.method == plan.method
    assert [step.tool for step in confirmed.steps] == [step.tool for step in plan.steps]
    assert confirmed.inputs == plan.inputs and confirmed.steps[0].parameters == plan.steps[0].parameters
    assert source.read_bytes() == original
    with open_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM analysis_plan").fetchone()[0] == 1
    with pytest.raises(LookupError):
        load_plan(database, plan.id, 2)


def test_new_draft_version_and_repeated_confirmation_preserve_confirmed_bytes(draft, registry):
    database, _, _, context, plan = draft
    save_draft(database, plan, context=context)
    confirmed = _confirm(draft, registry)
    snapshot = _row(database, plan)

    with pytest.raises(ValueError, match="already exists"):
        save_draft(database, plan)
    later = plan.model_copy(update={"version": 2})
    save_draft(database, later)

    assert load_plan(database, plan.id, 2) == later
    assert _confirm(draft, registry) == confirmed and _row(database, plan) == snapshot
    with open_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 0


@pytest.mark.parametrize(("overrides", "code", "field"), [
    ({"registered_rules": ()}, "UNKNOWN_CHECK_RULE", "check_rules.0"),
    ({"precondition_checks": None}, "UNKNOWN_PRECONDITION", "preconditions.1"),
])
def test_unregistered_checks_keep_draft(draft, registry, overrides, code, field):
    database, _, _, _, plan = draft
    save_draft(database, plan)
    previous = _row(database, plan)

    with pytest.raises(PlanConfirmationError) as error:
        _confirm(draft, registry, **overrides)

    assert (code, field) in {(issue.code, issue.field) for issue in error.value.issues}
    assert _row(database, plan) == previous


def test_empty_check_rule_configuration_blocks_confirmation(draft, registry):
    database, _, catalog, _, plan = draft
    method = catalog.get(plan.method.id, plan.method.version).model_copy(update={"check_rules": ()})
    plan = plan.model_copy(update={"check_rules": ()})
    save_draft(database, plan)
    without_rules = ToolRegistry(qgis_process=registry._qgis_process)
    without_rules.register(_contract("qgis-extract-by-attribute").model_copy(update={"check_rules": ()}))

    with pytest.raises(PlanConfirmationError) as error:
        _confirm(draft, without_rules, catalog=MethodCatalog([method], ()), registered_rules=())

    assert ("MISSING_CHECK_RULES", "check_rules") in {(issue.code, issue.field) for issue in error.value.issues}
    assert load_plan(database, plan.id, 1) == plan


def test_existing_issues_are_returned_and_preserved(draft, registry):
    database, source, catalog, context, plan = draft
    issue = PlanIssue(code="INPUT_REQUIRED", field="inputs.features", message="请选择目标数据")
    plan = plan.model_copy(update={"issues": (issue,)})
    save_draft(database, plan)

    with pytest.raises(PlanConfirmationError) as error:
        _confirm((database, source, catalog, context, plan), registry)

    assert error.value.issues == (issue,)
    assert load_plan(database, plan.id, 1) == plan


def test_real_source_content_change_blocks_confirmation(draft, registry):
    database, source, _, _, plan = draft
    save_draft(database, plan)
    previous = _row(database, plan)
    points = pyogrio.read_dataframe(source, layer="targets", max_features=1)
    pyogrio.write_dataframe(points, source, layer="targets", append=True)

    with pytest.raises(PlanConfirmationError) as error:
        _confirm(draft, registry)

    assert ("INPUT_CHANGED", "inputs.features") in {(issue.code, issue.field) for issue in error.value.issues}
    assert _row(database, plan) == previous


def test_real_wrong_geometry_and_missing_field_cannot_be_confirmed(draft, registry):
    database, source, _, _, plan = draft
    lines = register_asset(database, source, layer="roads").ref
    wrong_geometry = plan.model_copy(update={"inputs": {"features": lines}})
    save_draft(database, wrong_geometry)
    with pytest.raises(PlanConfirmationError) as error:
        _confirm(draft, registry)
    assert ("METHOD_PRECONDITION_FAILED", "inputs.features") in {(issue.code, issue.field) for issue in error.value.issues}

    data = plan.model_dump(mode="python")
    data.update(version=2)
    data["critical_options"]["field_name"] = "absent_field"
    data["steps"][0]["parameters"]["field"] = "absent_field"
    missing_field = AnalysisPlan.model_validate(data)
    save_draft(database, missing_field)
    with pytest.raises(PlanConfirmationError) as error:
        _confirm((*draft[:-1], missing_field), registry)
    assert ("METHOD_PRECONDITION_FAILED", "critical_options.field_name") in {
        (issue.code, issue.field) for issue in error.value.issues
    }
    assert load_plan(database, plan.id, 2) == missing_field


def test_tool_unavailable_and_stored_parameter_drift_keep_draft(draft, registry):
    database, _, _, _, plan = draft
    data = plan.model_dump(mode="python")
    data["steps"][0]["parameters"]["value"] = "different-target"
    changed = AnalysisPlan.model_validate(data)
    save_draft(database, changed)
    unavailable = ToolRegistry(qgis_process="/definitely-missing-qgis-process")
    unavailable.register(_contract("qgis-extract-by-attribute"))

    with pytest.raises(PlanConfirmationError) as error:
        _confirm(draft, unavailable)
    assert error.value.issues[0].code == "TOOL_UNAVAILABLE"
    with pytest.raises(PlanConfirmationError) as error:
        _confirm(draft, registry)
    assert (error.value.issues[0].code, error.value.issues[0].field) == ("PARAMETER_MISMATCH", "steps.0.parameters.value")
    assert load_plan(database, plan.id, 1) == changed


def test_confirm_preserves_explicit_quantity_and_persists_normalized_tool_value(draft, registry):
    database, source, _, _, _ = draft
    road = register_asset(database, source, layer="roads").ref
    contract = _contract("qgis-buffer")
    method = MethodDefinition.model_validate({
        "id": "buffer-confirmation", "version": 1, "locked_order": True,
        "inputs": {"roads": "vector_line"}, "critical_options": {
            "distance": {"type": "number", "unit": "meter", "minimum_exclusive": 0},
        }, "steps": [{"key": "create_buffer", "tool": contract.ref, "inputs": {"source": {"asset": "roads"}},
                      "parameters": {"distance": {"option": "distance"}, "segments": {"literal": 16},
                                     "dissolve": {"literal": True}, "end_cap_style": {"literal": "round"},
                                     "join_style": {"literal": "round"}, "miter_limit": {"literal": 2}},
                      "outputs": contract.outputs}], "expected_artifacts": [], "check_rules": ["output_exists"],
    })
    context = build_task_context(database, task_id="buffer-task", goal=TaskGoal(summary="道路缓冲"),
                                 method=method, selected={"roads": road}, registry=registry)
    plan = build_draft(context, method, plan_id="buffer-plan", options={"distance": 500})
    data = plan.model_dump(mode="python")
    quantity = {"value": 0.5, "unit": "kilometer"}
    data["critical_options"]["distance"] = quantity
    data["steps"][0]["parameters"]["distance"] = quantity
    plan = AnalysisPlan.model_validate(data)
    save_draft(database, plan, context=context)

    confirmed = confirm_plan(database, plan.id, 1, catalog=MethodCatalog([method], ()), registry=registry,
                             registered_rules={"output_exists"})

    assert confirmed.critical_options["distance"] == quantity
    assert confirmed.steps[0].parameters["distance"] == 500
    assert load_plan(database, plan.id, 1) == confirmed


def test_parallel_confirmation_publishes_one_identical_snapshot(draft, registry):
    database, _, _, _, plan = draft
    save_draft(database, plan)
    rendezvous = Barrier(2, timeout=15)

    def check(plan, assets):
        issues = METHOD_PRECONDITIONS["selected_field_exists"](plan, assets)
        rendezvous.wait()
        return issues

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_confirm, draft, registry, precondition_checks={"selected_field_exists": check}) for _ in range(2)]
        first, second = [future.result() for future in futures]
    assert first == second == load_plan(database, plan.id, 1)
    with open_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM analysis_plan WHERE status = 'confirmed'").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 0


def test_database_update_failure_rolls_back(draft, registry):
    database, _, _, _, plan = draft
    save_draft(database, plan)
    previous = _row(database, plan)
    with open_database(database) as connection, connection:
        connection.execute("""
            CREATE TRIGGER reject_confirmation BEFORE UPDATE OF status ON analysis_plan
            WHEN NEW.status = 'confirmed' BEGIN SELECT RAISE(ABORT, 'write blocked'); END
        """)

    with pytest.raises(sqlite3.IntegrityError, match="write blocked"):
        _confirm(draft, registry)

    assert _row(database, plan) == previous
