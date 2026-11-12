from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]


def test_task_context_draft_and_resume_keep_exact_asset_plan_and_run_versions(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    config = ROOT / "config"
    catalog = load_method_catalog(config / "methods")
    registry = ToolRegistry(python_implementations={"geopilot.select_spatial_relation", "geopilot.clip_vector_to_extent"})
    for name in ("python-select-spatial-relation", "python-clip-vector-to-extent"):
        contract = ToolContract.model_validate(yaml.safe_load((config / f"tools/{name}.yaml").read_text()))
        registry.register(contract)
    app = create_app(database, workspace_root=tmp_path / "workspace", catalog=catalog, registry=registry)
    source = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
    with TestClient(app) as client:
        goal = {"summary": "筛选范围内的建筑点"}
        response = client.post("/api/tasks", json={"goal": goal})
        assert response.status_code == 201
        task_id = response.json()["task_id"]
        base = f"/api/tasks/{task_id}"
        method = {"id": "spatial-select-and-clip", "version": 1}
        context = client.post(f"{base}/context", json={"method": method}).json()
        assert context["task_id"] == task_id
        assert context["roles"]["targets"]["asset"] is None and context["roles"]["targets"]["code"]

        def register(layer):
            result = client.post("/api/assets", json={"path": str(source), "layer": layer}).json()
            return {key: result[key] for key in ("asset_id", "asset_version")}

        targets, extent = register("targets"), register("analysis_extent")
        inputs = {"targets": targets, "relation_area": extent, "analysis_extent": extent}
        request = {"method": method, "inputs": inputs}
        response = client.post(f"{base}/plans", json=request)
        assert response.status_code == 200
        incomplete = response.json()
        assert incomplete["status"] == "draft" and any(issue["field"].endswith("spatial_relation") for issue in incomplete["issues"])
        assert client.get(base).json()["context"]["roles"]["targets"]["asset"] == targets

        response = client.post(f"{base}/plans", json={
            **request, "critical_options": {"spatial_relation": "within"},
            "base_plan_id": incomplete["id"], "base_plan_version": incomplete["version"],
        })
        assert response.status_code == 200
        draft = response.json()
        assert draft["id"] == incomplete["id"] and draft["version"] == 2 and draft["issues"] == []
        plan_url = f"/api/plans/{draft['id']}"
        response = client.post(f"{plan_url}/confirm", json={"plan_version": 2})
        assert response.status_code == 200, response.json()
        run_id = client.post(f"{plan_url}/runs", json={"plan_version": 2}).json()["run_id"]
        resumed = client.get(base).json()
        assert resumed["goal"] == goal
        assert [(entry["version"], entry["status"]) for entry in resumed["plans"]] == [(2, "confirmed"), (1, "draft")]
        assert resumed["runs"][0]["run_id"] == run_id and resumed["runs"][0]["plan_version"] == 2
        assert resumed["context"]["roles"]["targets"]["asset"] == targets
        assert client.get("/api/tasks/missing").status_code == 404
