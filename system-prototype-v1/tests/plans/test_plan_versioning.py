import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pyogrio
import pytest
import yaml

from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import MethodCatalog, load_method_catalog
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.models import AnalysisPlan, PlanStatus
from geopilot.plans.service import confirm_plan, load_plan, save_draft
from geopilot.plans.versioning import PlanVersionConflictError, revise_plan
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
TASK_ID = "task-revision"
PLAN_ID = "plan-revision"
GOAL = "准备道路测量输入"
OPTIONS = {"strategy": "local_projected", "target_crs": "EPSG:32650"}


def _contract(tool_id):
    path = ROOT / f"config/tools/{tool_id}.yaml"
    return ToolContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _context(database, method, selected, *, summary=GOAL, task_id=TASK_ID):
    return build_task_context(
        database, task_id=task_id, goal=TaskGoal(summary=summary), method=method, selected=selected,
    )


def _confirm(database, plan, context, method, registry):
    save_draft(database, plan, context=context)
    return confirm_plan(
        database, plan.id, plan.version, catalog=MethodCatalog([method], ()), registry=registry,
        registered_rules={"output_exists"},
    )


@pytest.fixture
def confirmed_plan(tmp_path):
    database = tmp_path / "plans.sqlite"
    initialize_database(database)
    source = tmp_path / "sample.gpkg"
    shutil.copyfile(SOURCE, source)
    roads = register_asset(database, source, layer="roads")
    contract = _contract("python-prepare-measurement-lines")
    registry = ToolRegistry(python_implementations={contract.implementation})
    registry.register(contract)
    method = MethodDefinition.model_validate({
        "id": "prepare-roads", "version": 1, "locked_order": True,
        "inputs": {"roads": "vector_line"},
        "critical_options": {"strategy": ["local_projected"], "target_crs": {"type": "reference"}},
        "steps": [{
            "key": "prepare", "tool": contract.ref, "inputs": {"source": {"asset": "roads"}},
            "parameters": {"method": {"option": "strategy"}, "target_crs": {"option": "target_crs"}},
            "outputs": contract.outputs,
        }],
        "expected_artifacts": [{"role": "prepared_roads", "step": "prepare", "output": "roads", "type": "vector_line"}],
        "check_rules": ["output_exists"],
    })
    context = _context(database, method, {"roads": roads.ref})
    draft = build_draft(context, method, plan_id=PLAN_ID, options=OPTIONS)
    confirmed = _confirm(database, draft, context, method, registry)
    return database, source, method, context, confirmed


def _versions(database):
    with open_database(database) as connection:
        rows = connection.execute(
            "SELECT version, status FROM analysis_plan WHERE id = ? ORDER BY version", (PLAN_ID,),
        ).fetchall()
    return [tuple(row) for row in rows]


def test_option_edit_creates_draft_and_keeps_historical_run_snapshot(confirmed_plan):
    database, _, method, context, initial = confirmed_plan
    with open_database(database) as connection, connection:
        original = connection.execute(
            "SELECT plan_json FROM analysis_plan WHERE id = ? AND version = 1", (initial.id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO run (id, plan_id, plan_version, execution_status, check_status, workdir, created_at)
               VALUES ('historical-run', ?, 1, 'SUCCEEDED', 'PASSED', 'old-run-workdir', '2026-11-11T00:00:00+00:00')""",
            (initial.id,),
        )

    options = {**OPTIONS, "target_crs": "EPSG:32651"}
    revised = revise_plan(database, initial.id, 1, context=context, method=method, options=options)

    assert (revised.id, revised.task_id, revised.version, revised.status) == (initial.id, TASK_ID, 2, PlanStatus.DRAFT)
    assert revised.inputs == initial.inputs and revised.method == initial.method
    assert revised.critical_options == options and revised.steps[0].parameters["target_crs"] == "EPSG:32651"
    assert not revised.issues
    assert load_plan(database, initial.id, 2) == revised
    assert load_plan(database, initial.id, 1) == initial
    assert _versions(database) == [(1, "confirmed"), (2, "draft")]
    with open_database(database) as connection:
        runs = connection.execute(
            """SELECT run.plan_version, run.execution_status, run.check_status, plan.plan_json
               FROM run JOIN analysis_plan AS plan ON plan.id = run.plan_id AND plan.version = run.plan_version""",
        ).fetchall()
    assert [tuple(row) for row in runs] == [(1, "SUCCEEDED", "PASSED", original)]


def test_changed_asset_version_inherits_choices_and_keeps_old_asset_ref(confirmed_plan):
    database, source, method, _, initial = confirmed_plan
    extra = pyogrio.read_dataframe(source, layer="roads", max_features=1)
    extra["source_feature_id"] = ["road-added-after-confirmation"]
    pyogrio.write_dataframe(extra, source, layer="roads", append=True)
    updated = register_asset(database, source, layer="roads")
    assert updated.id == initial.inputs["roads"].asset_id and updated.version == 2
    context = _context(database, method, {"roads": updated.ref})

    revised = revise_plan(database, initial.id, 1, context=context, method=method)

    assert revised.status is PlanStatus.DRAFT and revised.version == 2
    assert revised.inputs == {"roads": updated.ref}
    assert revised.critical_options == OPTIONS and not revised.issues
    assert load_plan(database, initial.id, 1).inputs["roads"].asset_version == 1


def test_method_edit_uses_new_bindings_and_exposes_its_missing_choices(confirmed_plan):
    database, source, _, _, initial = confirmed_plan
    method = load_method_catalog(ROOT / "config/methods").get("attribute-filter-export", 1)
    points = register_asset(database, source, layer="targets")
    context = _context(database, method, {"features": points.ref})

    revised = revise_plan(database, initial.id, 1, context=context, method=method)

    assert revised.version == 2 and revised.status is PlanStatus.DRAFT and revised.method == method.ref
    assert revised.inputs == {"features": points.ref} and not revised.critical_options
    assert [step.key for step in revised.steps] == ["filter_features"]
    assert {(issue.code, issue.field) for issue in revised.issues} == {
        ("OPTION_REQUIRED", "critical_options.field_name"), ("OPTION_REQUIRED", "critical_options.match_value"),
    }
    assert load_plan(database, initial.id, 1) == initial


def test_goal_edit_is_idempotent_and_stale_distinct_edit_conflicts(confirmed_plan):
    database, _, method, context, initial = confirmed_plan
    assert revise_plan(database, initial.id, 1, context=context, method=method) == initial
    assert _versions(database) == [(1, "confirmed")]
    changed = _context(database, method, initial.inputs, summary="复核道路测量输入")

    revised = revise_plan(database, initial.id, 1, context=changed, method=method)

    assert revised.goal == changed.goal and revised.version == 2 and revised.status is PlanStatus.DRAFT
    assert revise_plan(database, initial.id, 1, context=changed, method=method) == revised
    assert revise_plan(database, initial.id, 2, context=changed, method=method) == revised
    with pytest.raises(PlanVersionConflictError) as raised:
        revise_plan(database, initial.id, 1, context=context, method=method, options={**OPTIONS, "target_crs": "EPSG:32651"})
    assert (raised.value.code, raised.value.field) == ("PLAN_VERSION_CONFLICT", "version")
    assert _versions(database) == [(1, "confirmed"), (2, "draft")]

    further = revise_plan(database, initial.id, 2, context=changed, method=method, options={**OPTIONS, "target_crs": "EPSG:32651"})
    assert further.version == 3 and further.status is PlanStatus.DRAFT
    assert load_plan(database, initial.id, 2) == revised
    assert _versions(database) == [(1, "confirmed"), (2, "draft"), (3, "draft")]


@pytest.mark.parametrize("same_content", [True, False], ids=["retry", "conflict"])
def test_concurrent_editors_only_allocate_one_version_from_same_baseline(confirmed_plan, same_content):
    database, _, method, context, initial = confirmed_plan
    ready = Barrier(2, timeout=10)

    def edit(crs):
        ready.wait()
        try:
            return revise_plan(database, initial.id, 1, context=context, method=method, options={**OPTIONS, "target_crs": crs})
        except PlanVersionConflictError as error:
            return error

    choices = ("EPSG:32651", "EPSG:32651" if same_content else "EPSG:32652")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(edit, choices))

    drafts = [result for result in outcomes if isinstance(result, AnalysisPlan)]
    assert len(drafts) == (2 if same_content else 1)
    stored = load_plan(database, initial.id, 2)
    assert all(result == stored for result in drafts)
    assert stored.status is PlanStatus.DRAFT and stored.critical_options["target_crs"] in choices
    assert _versions(database) == [(1, "confirmed"), (2, "draft")]


def test_missing_baseline_and_other_task_leave_existing_versions_unchanged(confirmed_plan):
    database, _, method, context, initial = confirmed_plan
    with pytest.raises(LookupError):
        revise_plan(database, initial.id, 9, context=context, method=method)
    another_task = _context(database, method, initial.inputs, task_id="other-task")
    with pytest.raises(ValueError, match="task"):
        revise_plan(database, initial.id, 1, context=another_task, method=method)
    assert _versions(database) == [(1, "confirmed")]


def test_failed_draft_insert_rolls_back_the_task_context_change(confirmed_plan):
    database, _, method, _, initial = confirmed_plan
    with open_database(database) as connection, connection:
        original_task = connection.execute("SELECT * FROM task WHERE id = ?", (initial.task_id,)).fetchone()
        connection.execute("""
            CREATE TRIGGER block_new_draft BEFORE INSERT ON analysis_plan
            WHEN NEW.version = 2
            BEGIN SELECT RAISE(ABORT, 'revision blocked'); END
        """)
    edited = _context(database, method, initial.inputs, summary="尝试更新任务目标")

    with pytest.raises(sqlite3.IntegrityError, match="revision blocked"):
        revise_plan(database, initial.id, 1, context=edited, method=method)

    assert load_plan(database, initial.id, 1) == initial
    assert _versions(database) == [(1, "confirmed")]
    with open_database(database) as connection:
        assert connection.execute("SELECT * FROM task WHERE id = ?", (initial.task_id,)).fetchone() == original_task


def test_edit_inherits_a_confirmed_explicit_quantity_and_keeps_its_unit(tmp_path):
    executable = os.getenv("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    bundled = ROOT / ".environments/qgis-deb/qgis_process"
    if executable is None and bundled.is_file():
        executable = str(bundled)
    if executable is None:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS for actual buffer tool discovery")
    database = tmp_path / "quantity.sqlite"
    initialize_database(database)
    roads = register_asset(database, SOURCE, layer="roads")
    contract = _contract("qgis-buffer")
    registry = ToolRegistry(qgis_process=executable)
    registry.register(contract)
    method = MethodDefinition.model_validate({
        "id": "revise-buffer", "version": 1, "locked_order": True, "inputs": {"roads": "vector_line"},
        "critical_options": {"distance": {"type": "number", "unit": "meter", "minimum_exclusive": 0}},
        "steps": [{
            "key": "buffer", "tool": contract.ref, "inputs": {"source": {"asset": "roads"}},
            "parameters": {
                "distance": {"option": "distance"}, "segments": {"literal": 16},
                "dissolve": {"literal": True}, "end_cap_style": {"literal": "round"},
                "join_style": {"literal": "round"}, "miter_limit": {"literal": 2},
            },
            "outputs": contract.outputs,
        }],
        "expected_artifacts": [], "check_rules": ["output_exists"],
    })
    context = _context(database, method, {"roads": roads.ref})
    saved = build_draft(context, method, plan_id=PLAN_ID, options={"distance": 500}).model_dump(mode="python")
    quantity = {"value": 0.5, "unit": "kilometer"}
    saved["critical_options"]["distance"] = saved["steps"][0]["parameters"]["distance"] = quantity
    original = _confirm(database, AnalysisPlan.model_validate(saved), context, method, registry)
    assert original.steps[0].parameters["distance"] == 500
    assert revise_plan(database, original.id, 1, context=context, method=method) == original
    edited = _context(database, method, original.inputs, summary="以相同距离复核道路缓冲")

    revised = revise_plan(database, original.id, 1, context=edited, method=method)

    assert revised.version == 2 and revised.status is PlanStatus.DRAFT and not revised.issues
    assert revised.critical_options["distance"] == quantity
    assert revised.steps[0].parameters["distance"] == quantity
    assert load_plan(database, original.id, 1) == original
