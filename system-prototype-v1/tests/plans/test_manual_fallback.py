import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.data.assets import register_asset
from geopilot.domain.tool_contract import ToolContract
from geopilot.llm.base import ModelErrorCode
from geopilot.llm.providers.mock import MockModelProvider
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
METHOD = {"id": "attribute-filter-export", "version": 1}
OPTIONS = {"field_name": "source_feature_id", "match_value": "building-west"}
GOAL = {"summary": "手工筛选点要素"}


@pytest.fixture(scope="module")
def registry():
    qgis = ROOT / ".environments/qgis-deb/qgis_process"
    registry = ToolRegistry(qgis_process=qgis if qgis.is_file() else None)
    contract = ROOT / "config/tools/qgis-extract-by-attribute.yaml"
    registry.register(ToolContract.model_validate(yaml.safe_load(contract.read_text())))
    if not registry.get("qgis-extract-by-attribute", 1).available:
        pytest.skip("A real QGIS Processing entry point is required")
    return registry


def _project(tmp_path, registry, provider):
    database = tmp_path / "catalog.sqlite"
    initialize_database(database)
    vector = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
    asset = register_asset(database, vector, layer="targets")
    app = create_app(
        database, workspace_root=tmp_path / "workspace", catalog=load_method_catalog(ROOT / "config/methods"),
        registry=registry, registered_rules=RUN_CHECKS, model_provider=provider,
    )
    return database, app, {"features": asset.ref.model_dump(mode="json")}


def _counts(database):
    with open_database(database, read_only=True) as connection:
        return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("task", "analysis_plan", "run"))


def test_missing_model_keeps_the_manual_plan_path_available(tmp_path, registry):
    database, app, inputs = _project(tmp_path, registry, None)
    with TestClient(app) as client:
        assert client.get("/api/model-assistance").json() == {"available": False}
        no_model = client.post("/api/model-assistance", json={"goal": GOAL})
        assert no_model.status_code == 503 and no_model.json()["detail"]["code"] == "MODEL_UNAVAILABLE"
        assert _counts(database) == (0, 0, 0)
        task = client.post("/api/tasks", json={"goal": GOAL}).json()["task_id"]
        draft = client.post(f"/api/tasks/{task}/plans", json={
            "goal": GOAL, "method": METHOD, "inputs": inputs, "critical_options": OPTIONS,
        })
        assert draft.status_code == 200 and draft.json()["status"] == "draft" and not draft.json()["issues"]
        confirmation = client.post(f"/api/plans/{draft.json()['id']}/confirm", json={"plan_version": 1})
        assert confirmation.status_code == 200 and confirmation.json()["status"] == "confirmed"
        assert _counts(database) == (1, 1, 0)


def test_suggestion_is_previewed_and_uses_the_same_draft_endpoint(tmp_path, registry):
    provider = MockModelProvider(response_text=json.dumps({
        "method": METHOD, "goal": {"summary": "候选点筛选"}, "options": OPTIONS, "missing": [],
    }))
    database, app, inputs = _project(tmp_path, registry, provider)
    with TestClient(app) as client:
        assert client.get("/api/model-assistance").json() == {"available": True}
        suggestion = client.post("/api/model-assistance", json={"goal": GOAL, "method": METHOD, "inputs": inputs})
        assert suggestion.status_code == 200, suggestion.text
        preview = suggestion.json()
        assert preview["method"] == METHOD and preview["options"] == OPTIONS and preview["issues"] == []
        assert _counts(database) == (0, 0, 0)
        task = client.post("/api/tasks", json={"goal": GOAL}).json()["task_id"]
        draft = client.post(f"/api/tasks/{task}/plans", json={
            "goal": GOAL, "method": preview["method"], "inputs": inputs, "critical_options": preview["options"],
        }).json()
        assert draft["goal"] == GOAL and draft["steps"][0]["key"] == "filter_features"
        assert _counts(database) == (1, 1, 0)


def test_provider_failure_leaves_existing_draft_unchanged(tmp_path, registry):
    database, app, inputs = _project(tmp_path, registry, MockModelProvider(error_code=ModelErrorCode.TIMEOUT))
    with TestClient(app) as client:
        task = client.post("/api/tasks", json={"goal": GOAL}).json()["task_id"]
        draft = client.post(f"/api/tasks/{task}/plans", json={
            "goal": GOAL, "method": METHOD, "inputs": inputs, "critical_options": OPTIONS,
        }).json()
        failure = client.post("/api/model-assistance", json={"goal": GOAL, "method": METHOD, "inputs": inputs})
        assert failure.status_code == 504 and failure.json()["detail"]["code"] == "MODEL_TIMEOUT"
        saved = client.get(f"/api/plans/{draft['id']}?version=1").json()
        assert saved == draft and _counts(database) == (1, 1, 0)
