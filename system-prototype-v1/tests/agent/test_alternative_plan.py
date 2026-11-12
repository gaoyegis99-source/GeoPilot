from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.application.task_context import ContextCriteria, build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.llm.alternative_plan import compare_plan_alternatives
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.models import PlanStatus
from geopilot.plans.service import save_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
LOCAL_TOOL = "python-prepare-measurement-lines"
GEODESIC_TOOL = "python-prepare-wgs84-measurement-lines"
CRITERIA = ContextCriteria(object_types={"roads": "vector_line"}, expected_artifact="prepared_roads")


def _method(*, identifier="prepare-roads", version=3, variants=True, local_tool=LOCAL_TOOL, role="prepared_roads"):
    base = {
        "id": identifier, "version": version, "locked_order": True,
        "inputs": {"roads": "vector_line"},
        "critical_options": {
            "strategy": ["local_projected", "geodesic"] if variants else ["local_projected"],
            "target_crs": {"type": "reference", "required_for": "local_projected"},
        },
        "preconditions": ["required_input_roles_available"],
        "steps": [{
            "key": "prepare", "tool": {"id": local_tool, "version": 1}, "inputs": {"source": {"asset": "roads"}},
            "parameters": {"method": {"option": "strategy"}, "target_crs": {"option": "target_crs"}},
            "outputs": {"roads": "vector_line"},
        }],
        "expected_artifacts": [{"role": role, "step": "prepare", "output": "roads", "type": "vector_line"}],
        "check_rules": ["output_exists"],
    }
    if variants:
        base["step_variants"] = {"option": "strategy", "choices": {
            "local_projected": {},
            "geodesic": {"prepare": {
                "tool": {"id": GEODESIC_TOOL, "version": 1},
                "parameters": {"method": {"option": "strategy"}, "target_crs": {"literal": "EPSG:4326"}},
            }},
        }}
    return MethodDefinition.model_validate(base)


@pytest.fixture
def context(tmp_path):
    database = tmp_path / "plan.sqlite"
    initialize_database(database)
    roads = register_asset(database, ROOT / "tests/fixtures/vector/road-buffer.gpkg", layer="roads").ref
    source = _method()
    task = build_task_context(
        database, task_id="task", goal=TaskGoal(summary="准备道路测量输入"), method=source, selected={"roads": roads},
        criteria=CRITERIA,
    )
    options = {"strategy": "local_projected", "target_crs": "EPSG:32650"}
    baseline = build_draft(task, source, plan_id="baseline", options=options)
    assert not baseline.issues
    save_draft(database, baseline, context=task)
    return database, baseline, source


def _registry(*, geodesic=True):
    contracts = [
        ToolContract.model_validate(yaml.safe_load((ROOT / "config/tools" / f"{tool}.yaml").read_text(encoding="utf-8")))
        for tool in (LOCAL_TOOL, GEODESIC_TOOL)
    ]
    registry = ToolRegistry(python_implementations={
        item.implementation for item in contracts if geodesic or item.id == LOCAL_TOOL
    })
    for contract in contracts:
        registry.register(contract)
    return registry


def test_alternatives_compare_exact_eligible_versions_and_variants_without_writing(context):
    database, baseline, source = context
    previous = _method(version=2, variants=False)
    comparable = _method(identifier="comparable-roads", version=1, variants=False)
    unrelated = _method(identifier="unrelated-roads", version=1, variants=False, role="other_roads")
    unregistered = _method(version=4, variants=False, local_tool="unregistered-tool")
    disabled = _method(version=5, variants=False)
    catalog = MethodCatalog([previous, source, comparable, unrelated, unregistered, disabled], (), disabled=[disabled.ref])

    result = compare_plan_alternatives(
        database, baseline, catalog=catalog, registry=_registry(), registered_rules=["output_exists"],
    )
    assert result.baseline.plan == baseline
    assert [candidate.plan.method for candidate in result.alternatives] == [comparable.ref, previous.ref, source.ref]
    geographic = result.alternatives[-1]
    assert geographic.plan.status is PlanStatus.DRAFT and not geographic.plan.issues
    assert geographic.plan.inputs == baseline.inputs
    assert geographic.plan.critical_options == {"strategy": "geodesic"}
    assert geographic.plan.steps[0].tool.id == GEODESIC_TOOL
    assert geographic.plan.steps[0].parameters["target_crs"] == "EPSG:4326"
    assert geographic.input_types == {"roads": "vector_line"}
    assert geographic.criteria == CRITERIA
    assert geographic.option_specs["target_crs"]["required_for"] == "local_projected"
    assert geographic.preconditions == ("required_input_roles_available",)
    assert geographic.measurement["prepare"].distance == "not_applicable"
    with open_database(database, read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM analysis_plan").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 0


def test_unavailable_variant_is_excluded_and_missing_user_input_is_visible(context):
    database, baseline, source = context
    previous = _method(version=2, variants=False)
    catalog = MethodCatalog([previous, source], ())
    geographic = baseline.model_copy(update={"critical_options": {"strategy": "geodesic"}})
    result = compare_plan_alternatives(
        database, geographic, catalog=catalog, registry=_registry(geodesic=False), registered_rules=["output_exists"],
    )
    assert [candidate.plan.method for candidate in result.alternatives] == [previous.ref, source.ref]
    for candidate in result.alternatives:
        assert candidate.plan.critical_options == {"strategy": "local_projected"}
        assert candidate.plan.status is PlanStatus.DRAFT
        assert any(issue.field == "critical_options.target_crs" for issue in candidate.plan.issues)
    no_geodesic = compare_plan_alternatives(
        database, baseline, catalog=catalog, registry=_registry(geodesic=False), registered_rules=["output_exists"],
    )
    assert [candidate.plan.method for candidate in no_geodesic.alternatives] == [previous.ref]


def test_http_comparison_then_explicit_selection_keeps_prior_confirmation(context):
    database, baseline, source = context
    registry = _registry()
    catalog = MethodCatalog([source], ())
    route = f"/api/plans/{baseline.id}"
    with TestClient(create_app(database, catalog=catalog, registry=registry)) as client:
        response = client.post(f"{route}/confirm", json={"plan_version": baseline.version})
        assert response.status_code == 200, response.text
        confirmed = response.json()
        result = client.get(f"{route}/alternatives", params={"version": baseline.version})
        assert result.status_code == 200, result.text
        assert result.json()["baseline"]["plan"] == confirmed
        before = client.get(f"/api/tasks/{baseline.task_id}").json()
        assert len(before["plans"]) == 1 and not before["runs"]
        choice, = result.json()["alternatives"]
        proposal = choice["plan"]
        revision = client.post(f"/api/tasks/{baseline.task_id}/plans", json={
            "base_plan_id": baseline.id, "base_plan_version": baseline.version, "goal": confirmed["goal"],
            "method": proposal["method"], "inputs": proposal["inputs"], "critical_options": proposal["critical_options"],
            "criteria": choice["criteria"],
        })
        assert revision.status_code == 200, revision.text
        draft = revision.json()
        assert draft["status"] == "draft" and draft["version"] == 2 and draft["issues"] == []
        assert draft["critical_options"] == {"strategy": "geodesic"}
        assert client.get(f"{route}?version=1").json() == confirmed
        assert client.post(f"{route}/runs", json={"plan_version": 2}).status_code == 409
        after = client.get(f"/api/tasks/{baseline.task_id}").json()
        assert len(after["plans"]) == 2 and not after["runs"]
