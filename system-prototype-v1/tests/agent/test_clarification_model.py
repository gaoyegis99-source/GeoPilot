import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from geopilot.application.task_context import TaskContext
from geopilot.llm.clarification import (
    ClarificationItem,
    ClarificationReport,
    build_clarification_report,
    require_goal_clarification,
)
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.models import AnalysisPlan, PlanStatus
from geopilot.plans.service import PlanConfirmationError, confirm_plan, load_plan, save_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def catalog():
    return load_method_catalog(ROOT / "config/methods")


@pytest.fixture
def road_draft(catalog):
    method = catalog.get("road-buffer-statistics", 3)
    ready = {"asset": {"asset_id": "roads", "asset_version": 2}, "status": "READY"}
    context = TaskContext.model_validate({
        "task_id": "task", "goal": {"summary": "Analyze road proximity"}, "method": method.ref, "roles": {},
        "candidates": {"roads": [
            ready, ready, {"asset": {"asset_id": "roads", "asset_version": 1}, "status": "BLOCKED"},
            {"asset": {"asset_id": "other-roads", "asset_version": 1}, "status": "CONDITIONAL"},
        ]},
    })
    return build_draft(context, method, plan_id="road-plan", version=2), method, context


@pytest.fixture
def ready_draft(catalog):
    method = catalog.get("attribute-filter-export", 1)
    context = TaskContext.model_validate({
        "task_id": "task", "goal": {"summary": "Select matching points"}, "method": method.ref,
        "roles": {"features": {"asset": {"asset_id": "points", "asset_version": 2}, "status": "READY"}},
    })
    plan = build_draft(context, method, plan_id="filter", options={"field_name": "class_name", "match_value": "school"})
    assert not plan.issues
    return plan, method, context


def test_draft_reuses_issues_and_offers_only_ready_assets_and_declared_choices(road_draft):
    plan, method, context = road_draft
    report = build_clarification_report(plan, method, context=context)

    assert (report.task_id, report.plan_id, report.plan_version, report.method) == ("task", "road-plan", 2, method.ref)
    assert report.status == "blocked"
    assert [(item.code, item.field, item.reason) for item in report.items] == [
        (issue.code, issue.field, issue.message) for issue in plan.issues
    ]
    items = {item.field: item for item in report.items}
    assert items["inputs.roads"].kind == "input"
    assert [value.model_dump() for value in items["inputs.roads"].options] == [{"asset_id": "roads", "asset_version": 2}]
    assert not items["inputs.targets"].options
    assert items["critical_options.distance_method"].options == ("local_projected", "geodesic")
    assert not items["critical_options.distance_m"].options
    assert all(item.required and item.blocks == "confirmation" for item in report.items)
    assert ClarificationReport.model_validate_json(report.model_dump_json()) == report


def test_local_goal_issue_is_persisted_and_blocks_v1_confirmation(tmp_path, ready_draft, catalog):
    plan, method, context = ready_draft
    flagged = require_goal_clarification(plan)
    assert not plan.issues
    assert require_goal_clarification(flagged) == flagged
    (item,) = build_clarification_report(flagged, method).items
    assert (item.kind, item.code, item.field, item.blocks, item.options) == (
        "goal", "GOAL_AMBIGUOUS", "goal.summary", "confirmation", (),
    )

    database = tmp_path / "project.sqlite"
    initialize_database(database)
    save_draft(database, flagged, context=context)
    assert load_plan(database, flagged.id, flagged.version) == flagged
    with pytest.raises(PlanConfirmationError) as error:
        confirm_plan(database, flagged.id, flagged.version, catalog=catalog, registry=ToolRegistry(), registered_rules=())
    assert error.value.issues == flagged.issues


def test_ready_draft_still_requires_user_confirmation(ready_draft):
    plan, method, context = ready_draft
    report = build_clarification_report(plan, method, context=context)
    (item,) = report.items
    assert report.status == "ready_for_confirmation"
    assert (item.kind, item.code, item.blocks) == ("confirmation", "USER_CONFIRMATION_REQUIRED", "execution")
    assert plan.status == "draft" and not plan.issues

    confirmed = AnalysisPlan.model_validate({**plan.model_dump(mode="python"), "status": PlanStatus.CONFIRMED})
    complete = build_clarification_report(confirmed, method)
    assert complete.status == "confirmed" and not complete.items
    with pytest.raises(ValueError, match="Only a draft"):
        require_goal_clarification(confirmed)


def test_stale_method_or_context_cannot_supply_choices(road_draft, catalog):
    plan, method, context = road_draft
    with pytest.raises(ValueError, match="exact method"):
        build_clarification_report(plan, catalog.get(method.id, 2), context=context)
    with pytest.raises(ValueError, match="context does not match"):
        build_clarification_report(plan, method, context=context.model_copy(update={"task_id": "different-task"}))


def test_context_with_other_selected_assets_cannot_supply_choices(ready_draft):
    plan, method, context = ready_draft
    with pytest.raises(ValueError, match="context does not match"):
        build_clarification_report(plan, method, context=context.model_copy(update={"roles": {}}))


@pytest.mark.parametrize(("kind", "blocks", "options"), [
    ("input", "confirmation", ("roads.gpkg",)),
    ("goal", "confirmation", ("invented answer",)),
    ("option", "confirmation", ({"asset_id": "wrong", "asset_version": 1},)),
    ("confirmation", "confirmation", ()),
])
def test_invalid_item_scope_is_rejected(kind, blocks, options):
    with pytest.raises(ValidationError):
        ClarificationItem(kind=kind, code="CODE", field="field", reason="Reason", blocks=blocks, options=options)


def test_schema_matches_the_versioned_report():
    schema = json.loads((ROOT / "artifacts/schemas/clarification-report.schema.json").read_bytes())
    assert schema == ClarificationReport.model_json_schema()
