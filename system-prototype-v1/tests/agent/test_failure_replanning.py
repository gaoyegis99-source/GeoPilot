import hashlib
import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from geopilot.api.server import create_local_app
from geopilot.persistence.run_repository import get_run, update_run
from geopilot.persistence.schema import open_database
from geopilot.runs.models import CheckStatus


ROOT = Path(__file__).resolve().parents[2]
METHOD = {"id": "prepare-roads", "version": 1}


@pytest.fixture
def project(tmp_path):
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    local = {"id": "python-prepare-measurement-lines", "version": 1}
    geographic = {"id": "python-prepare-wgs84-measurement-lines", "version": 1}
    definition = {
        **METHOD, "locked_order": True, "inputs": {"roads": "vector_line"},
        "critical_options": {
            "strategy": ["local_projected", "geodesic"],
            "target_crs": {"type": "reference", "required_for": "local_projected"},
        },
        "steps": [{
            "key": "prepare", "tool": local, "inputs": {"source": {"asset": "roads"}},
            "parameters": {"method": {"option": "strategy"}, "target_crs": {"option": "target_crs"}},
            "outputs": {"roads": "vector_line"},
        }],
        "step_variants": {"option": "strategy", "choices": {
            "local_projected": {},
            "geodesic": {"prepare": {"tool": geographic, "parameters": {
                "method": {"option": "strategy"}, "target_crs": {"literal": "EPSG:4326"},
            }}},
        }},
        "expected_artifacts": [{"role": "prepared_roads", "step": "prepare", "output": "roads", "type": "vector_line"}],
        "check_rules": ["output_exists"],
    }
    (config / "methods/prepare-roads.v1.yaml").write_text(json.dumps(definition), encoding="utf-8")
    database, workspace = tmp_path / "project.sqlite", tmp_path / "workspace"
    app = create_local_app(database, workspace, config=config, qgis_process=tmp_path / "unused-qgis")
    return app, database


def _ok(response, expected=200):
    assert response.status_code == expected, response.text
    return response.json()


def _await_terminal(client, run_id):
    deadline = time.monotonic() + 30
    while True:
        state = _ok(client.get(f"/api/runs/{run_id}"))
        if state["execution_status"] in {"FAILED", "CANCELLED"} or (
            state["execution_status"] == "SUCCEEDED" and state["check_status"] in {"PASSED", "WARNING", "FAILED"}
        ):
            return state
        assert time.monotonic() < deadline, state
        time.sleep(0.05)


def _digest_files(directory):
    return {path.relative_to(directory): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


def test_actual_failure_revision_keeps_run_and_requires_new_confirmation(project, tmp_path):
    app, database = project
    fixture = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
    source = tmp_path / "roads.gpkg"
    original_bytes = fixture.read_bytes()
    source.write_bytes(original_bytes)
    input_digest = hashlib.sha256(original_bytes).hexdigest()
    with TestClient(app) as client:
        asset = _ok(client.post("/api/assets", json={"path": str(source), "layer": "roads"}))
        inputs = {"roads": {"asset_id": asset["asset_id"], "asset_version": asset["asset_version"]}}
        goal = {"summary": "准备道路测量输入"}
        task = _ok(client.post("/api/tasks", json={"goal": goal}), 201)["task_id"]
        draft = _ok(client.post(f"/api/tasks/{task}/plans", json={
            "goal": goal, "method": METHOD, "inputs": inputs,
            "critical_options": {"strategy": "local_projected", "target_crs": "EPSG:4326"},
        }))
        path = f"/api/plans/{draft['id']}"
        confirmed = _ok(client.post(f"{path}/confirm", json={"plan_version": 1}))
        original = _ok(client.post(f"{path}/runs", json={"plan_version": 1}), 202)["run_id"]
        failure = _await_terminal(client, original)
        assert (failure["execution_status"], failure["check_status"]) == ("FAILED", "NOT_CHECKED")
        assert failure["error"]["code"] == "PROCESS_FAILED"
        assert "Geographic CRS axes are angular" in client.get(f"/api/runs/{original}/steps/prepare/log").text
        with open_database(database, read_only=True) as connection:
            failed_run = get_run(connection, original)
        old_dir = Path(failed_run.workdir)
        old_files = _digest_files(old_dir)

        endpoint = f"/api/runs/{original}/replanning"
        options = _ok(client.get(endpoint))
        assert options["run_id"] == original and options["baseline"]["plan"] == confirmed
        alternative, = options["alternatives"]
        assert alternative["plan"]["critical_options"] == {"strategy": "geodesic"}
        assert alternative["plan"]["steps"][0]["tool"]["id"] == "python-prepare-wgs84-measurement-lines"
        assert alternative["plan"]["inputs"] == inputs and alternative["plan"]["issues"] == []
        assert client.post(endpoint, json={"proposal_id": "0" * 64}).status_code == 409
        selection = {"proposal_id": alternative["proposal_id"]}

        # A changed local copy invalidates the preview and exposes the original
        # method's input issue for clarification instead of guessing a version.
        source.write_bytes(b"source replaced after review")
        changed = _ok(client.get(endpoint))
        repair = next(item for item in changed["alternatives"] if item["plan"]["critical_options"] == confirmed["critical_options"])
        assert any(issue["field"] == "inputs.roads" for issue in repair["plan"]["issues"])
        assert client.post(endpoint, json=selection).status_code == 409
        source.write_bytes(original_bytes)

        state = _ok(client.get(f"/api/tasks/{task}"))
        assert len(state["plans"]) == len(state["runs"]) == 1

        revised = _ok(client.post(endpoint, json=selection))
        assert revised["version"] == 2 and revised["status"] == "draft"
        assert revised["critical_options"] == {"strategy": "geodesic"} and revised["issues"] == []
        assert _ok(client.post(endpoint, json=selection)) == revised
        assert client.post(f"{path}/runs", json={"plan_version": 2}).status_code == 409
        assert len(_ok(client.get(f"/api/tasks/{task}"))["runs"]) == 1

        assert _ok(client.post(f"{path}/confirm", json={"plan_version": 2}))["status"] == "confirmed"
        fresh = _ok(client.post(f"{path}/runs", json={"plan_version": 2}), 202)["run_id"]
        assert fresh != original
        success = _await_terminal(client, fresh)
        assert (success["execution_status"], success["check_status"]) == ("SUCCEEDED", "PASSED")
        artifact, = _ok(client.get(f"/api/runs/{fresh}/artifacts"))["artifacts"]
        assert artifact["role"] == "prepared_roads" and artifact["metadata"]["crs"] == "EPSG:4326"
        assert artifact["metadata"]["feature_count"] == asset["metadata"]["feature_count"]
        assert _ok(client.get(f"{path}?version=1")) == confirmed
        assert _ok(client.get(f"/api/runs/{original}")) == failure
        assert _digest_files(old_dir) == old_files
        assert hashlib.sha256(source.read_bytes()).hexdigest() == input_digest
        assert hashlib.sha256(fixture.read_bytes()).hexdigest() == input_digest
        assert client.post(endpoint, json=selection).status_code == 409
        assert client.get(f"/api/runs/{fresh}/replanning").status_code == 409
        assert client.get("/api/runs/unknown/replanning").status_code == 404

        # A persisted check failure can also request comparison. Successful recheck
        # invalidates its earlier proposal, independent of the execution status.
        with open_database(database) as connection, connection:
            successful_run = get_run(connection, fresh)
            update_run(connection, successful_run.model_copy(update={"check_status": CheckStatus.FAILED}))
        check_endpoint = f"/api/runs/{fresh}/replanning"
        check_revision = _ok(client.get(check_endpoint))
        assert check_revision["execution_status"] == "SUCCEEDED" and check_revision["check_status"] == "FAILED"
        check_proposal, = check_revision["alternatives"]
        with open_database(database) as connection, connection:
            update_run(connection, successful_run)
        assert client.post(check_endpoint, json={"proposal_id": check_proposal["proposal_id"]}).status_code == 409
        assert len(_ok(client.get(f"/api/tasks/{task}"))["plans"]) == 2
