import shutil
from pathlib import Path

import pyogrio
import pytest
import yaml
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.application.task_context import ContextCriteria, build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.llm.clarification import require_goal_clarification
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.schema import initialize_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import save_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOAL = TaskGoal(summary="准备道路测量输入")
OPTIONS = {"strategy": "local_projected", "target_crs": "EPSG:32650"}
CRITERIA = ContextCriteria(object_types={"roads": "vector_line"}, expected_artifact="prepared_roads")


@pytest.fixture
def project(tmp_path):
    db = tmp_path / "project.sqlite"
    initialize_database(db)
    source = tmp_path / "roads.gpkg"
    shutil.copyfile(ROOT / "tests/fixtures/vector/road-buffer.gpkg", source)
    asset = register_asset(db, source, layer="roads")
    contract_path = ROOT / "config/tools/python-prepare-measurement-lines.yaml"
    contract = ToolContract.model_validate(yaml.safe_load(contract_path.read_text(encoding="utf-8")))
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
    app = create_app(db, catalog=MethodCatalog([method], ()), registry=registry)
    return db, source, asset.ref, method, registry, app


def _answer(client, path, version, field, value):
    return client.post(f"{path}/clarification", json={"plan_version": version, "field": field, "value": value})


def _confirm(client, path, version):
    return client.post(f"{path}/confirm", json={"plan_version": version})


def _assert_ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def test_partial_answers_keep_confirmed_history_and_review_new_asset_on_resume(project):
    db, source, original, method, _, app = project
    goal, method_ref = GOAL.model_dump(mode="json"), method.ref.model_dump(mode="json")
    criteria = CRITERIA.model_dump(mode="json")
    with TestClient(app) as client:
        task = client.post("/api/tasks", json={"goal": goal}).json()["task_id"]
        request = {"goal": goal, "method": method_ref, "inputs": {"roads": original.model_dump(mode="json")},
                   "criteria": criteria, "critical_options": OPTIONS}
        initial = _assert_ok(client.post(f"/api/tasks/{task}/plans", json=request))
        path = f"/api/plans/{initial['id']}"
        confirmed = _assert_ok(_confirm(client, path, 1))
        assert confirmed["status"] == "confirmed"

        additional = pyogrio.read_dataframe(source, layer="roads", max_features=1)
        additional["source_feature_id"] = ["road-added-after-confirmation"]
        pyogrio.write_dataframe(additional, source, layer="roads", append=True)
        updated = register_asset(db, source, layer="roads").ref.model_dump(mode="json")
        assert updated == {"asset_id": original.asset_id, "asset_version": 2}

        missing = _assert_ok(client.post(f"/api/tasks/{task}/plans", json={
            **request, "base_plan_id": confirmed["id"], "base_plan_version": 1,
            "critical_options": {"strategy": "local_projected"},
        }))
        assert missing["version"] == 2 and missing["status"] == "draft"
        questions = _assert_ok(client.get(f"{path}/clarification?version=2"))
        by_field = {item["field"]: item for item in questions["items"]}
        assert by_field["inputs.roads"]["code"] == "INPUT_CHANGED"
        assert updated in by_field["inputs.roads"]["options"]
        assert original.model_dump(mode="json") not in by_field["inputs.roads"]["options"]
        assert by_field["critical_options.target_crs"]["code"] == "OPTION_REQUIRED"

        option_answer = _assert_ok(_answer(client, path, 2, "critical_options.target_crs", "EPSG:32650"))
        assert option_answer["version"] == 3 and option_answer["critical_options"] == OPTIONS
        assert option_answer["inputs"] == missing["inputs"] and option_answer["goal"] == missing["goal"]
        assert _confirm(client, path, 3).status_code == 422
        conflict = _answer(client, path, 2, "critical_options.target_crs", "EPSG:32651")
        assert conflict.status_code == 409 and conflict.json()["detail"]["latest_version"] == 3
        assert _answer(client, path, 3, "inputs.roads", original.model_dump(mode="json")).status_code == 422

        ready = _assert_ok(_answer(client, path, 3, "inputs.roads", updated))
        assert ready["version"] == 4 and ready["status"] == "draft" and ready["issues"] == []
        assert ready["inputs"] == {"roads": updated} and ready["critical_options"] == OPTIONS
        assert client.post(f"{path}/runs", json={"plan_version": 4}).status_code == 409

    with TestClient(app) as fresh_client:
        restored = _assert_ok(fresh_client.get(f"/api/tasks/{task}"))
        assert [(entry["version"], entry["status"]) for entry in restored["plans"]] == [
            (4, "draft"), (3, "draft"), (2, "draft"), (1, "confirmed"),
        ]
        assert restored["context"]["criteria"] == criteria
        assert restored["context"]["roles"]["roads"]["asset"] == updated
        assert not restored["runs"]
        assert _assert_ok(fresh_client.get(f"{path}?version=4")) == ready
        assert _assert_ok(fresh_client.get(f"{path}/clarification?version=4"))["status"] == "ready_for_confirmation"
        assert _assert_ok(_confirm(fresh_client, path, 4))["status"] == "confirmed"
        assert _assert_ok(fresh_client.get(f"{path}?version=1")) == confirmed
        assert _assert_ok(fresh_client.get(f"{path}?version=2")) == missing
        assert _assert_ok(fresh_client.get(f"{path}?version=3")) == option_answer
        assert _answer(fresh_client, path, 4, "inputs.roads", updated).status_code == 409
        replacement = _assert_ok(fresh_client.post(f"/api/tasks/{task}/plans", json={
            **request, "inputs": {"roads": updated},
        }))
        assert replacement["id"] != initial["id"]
        historical_answer = _answer(fresh_client, path, 3, "inputs.roads", updated)
        assert historical_answer.status_code == 409
        assert historical_answer.json()["detail"]["code"] == "PLAN_DRAFT_REQUIRED"


def test_invalid_answer_keeps_draft_and_unrelated_answer_retains_goal_question(project):
    db, _, asset, method, registry, app = project
    context = build_task_context(db, task_id="goal-task", goal=GOAL, method=method, selected={"roads": asset},
                                 criteria=CRITERIA, registry=registry, options=OPTIONS)
    initial = require_goal_clarification(build_draft(
        context, method, plan_id="goal-plan", options={"target_crs": False}, registry=registry,
    ))
    save_draft(db, initial, context=context)
    path = "/api/plans/goal-plan"
    with TestClient(app) as client:
        assert _answer(client, path, 1, "status", "confirmed").status_code == 422
        assert _answer(client, path, 1, "critical_options.target_crs", False).status_code == 422
        assert _answer(client, path, 1, "goal.summary", " ").status_code == 422
        assert _answer(client, path, 1, "goal.summary", GOAL.summary).status_code == 422
        assert _answer(client, path, 1, "inputs.roads", {"asset_id": asset.asset_id, "asset_version": 2}).status_code == 422
        assert _assert_ok(client.get(f"{path}?version=1")) == initial.model_dump(mode="json")
        assert len(_assert_ok(client.get("/api/tasks/goal-task"))["plans"]) == 1

        strategy_answer = _assert_ok(_answer(client, path, 1, "critical_options.strategy", "local_projected"))
        assert strategy_answer["version"] == 2 and strategy_answer["goal"] == initial.goal.model_dump(mode="json")
        assert {issue["code"] for issue in strategy_answer["issues"]} >= {"GOAL_AMBIGUOUS", "INVALID_OPTION"}
        assert _answer(client, path, 1, "critical_options.target_crs", "EPSG:32650").status_code == 409

        goal_answer = _assert_ok(_answer(client, path, 2, "goal.summary", "把道路转换为米制测量线"))
        assert goal_answer["version"] == 3
        assert {issue["code"] for issue in goal_answer["issues"]} >= {"INVALID_OPTION"}
        assert "GOAL_AMBIGUOUS" not in {issue["code"] for issue in goal_answer["issues"]}

        ready = _assert_ok(_answer(client, path, 3, "critical_options.target_crs", "EPSG:32650"))
        assert ready["version"] == 4 and ready["status"] == "draft" and not ready["issues"]
        assert ready["goal"] == goal_answer["goal"]
