import os
import shutil
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

import pytest
import yaml
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.api.server import create_local_app
from geopilot.application.plan_runner import execute_claimed_run
from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.worker_lease import managed_workers_stopped
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.run_repository import get_run
from geopilot.persistence.schema import open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import save_draft
from geopilot.scheduler.loop import claim_next_run, run_once
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "src/geopilot/gis/tools/prepare_measurement_lines.py"


def test_confirmation_and_run_creation_are_separate_http_operations(project):
    database, _, workspace, draft, catalog, registry = project
    app = create_app(database, workspace_root=workspace, catalog=catalog, registry=registry)
    with TestClient(app) as client:
        base = f"/api/plans/{draft.id}"
        assert client.get(base, params={"version": 1}).json()["status"] == "draft"
        response = client.post(f"{base}/runs", json={"plan_version": 1})
        assert response.status_code == 409 and response.json()["detail"]["code"] == "PLAN_NOT_CONFIRMED"
        assert client.post(f"{base}/runs", json={"plan_version": True}).status_code == 422
        assert client.post(f"{base}/runs", json={"plan_version": 2}).status_code == 404

        response = client.post(f"{base}/confirm", json={"plan_version": 1})
        assert response.status_code == 200 and response.json()["status"] == "confirmed"
        assert not workspace.exists()
        with open_database(database, read_only=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0

        response = client.post(f"{base}/runs", json={"plan_version": 1})
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        assert client.post(f"{base}/runs", json={"plan_version": 1}).json() == {"run_id": run_id}
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200 and response.json()["execution_status"] == "NOT_STARTED"
        with open_database(database, read_only=True) as connection:
            row = connection.execute("SELECT plan_id, plan_version FROM run WHERE id=?", (run_id,)).fetchone()
        assert tuple(row) == (draft.id, 1)


def test_artifact_query_reports_registered_source_and_current_readability(project):
    database, _, workspace, draft, catalog, registry = project
    app = create_app(database, workspace_root=workspace, catalog=catalog, registry=registry)
    with TestClient(app) as client:
        base = f"/api/plans/{draft.id}"
        assert client.post(f"{base}/confirm", json={"plan_version": 1}).status_code == 200
        run_id = client.post(f"{base}/runs", json={"plan_version": 1}).json()["run_id"]
        endpoint = f"/api/runs/{run_id}/artifacts"
        assert client.get("/api/runs/missing/artifacts").status_code == 404
        response = client.get(endpoint)
        assert response.status_code == 200
        assert response.json() == {
            "run_id": run_id, "check_status": "NOT_CHECKED", "artifacts": [], "diagnostics": [],
        }

        step = draft.steps[0].tool
        implementation = registry.get(step.id, step.version).contract.implementation
        assert run_once(database, lambda run: execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry,
            python_implementations={implementation: WORKER},
        )) == run_id
        response = client.get(endpoint)
        assert response.status_code == 200
        data = response.json()
        assert data["check_status"] == "PASSED"
        artifact, = data["artifacts"]
        assert artifact["role"] == "roads" and artifact["kind"] == "vector"
        assert artifact["readable"] is True and artifact["metadata"]["feature_count"] == 3
        assert artifact["source"]["plan_version"] == 1 and artifact["source"]["plan_id"] == draft.id
        assert artifact["source"]["inputs"]["lines"] == draft.inputs["lines"].model_dump(mode="json")
        assert [entry["type"] for entry in data["diagnostics"]] == ["log"]

        with open_database(database, read_only=True) as connection:
            workdir = Path(get_run(connection, run_id).workdir)
        artifact_url = f"/api/artifacts/{artifact['id']}"
        raw = client.get(f"{artifact_url}/file")
        assert raw.status_code == 200 and raw.content == (workdir / artifact["uri"]).read_bytes()
        assert "attachment" in raw.headers["content-disposition"]
        preview = client.get(f"{artifact_url}/preview")
        assert preview.status_code == 200 and preview.headers["content-type"].startswith("application/geo+json")
        assert preview.json()["type"] == "FeatureCollection"
        assert {feature["properties"]["source_feature_id"] for feature in preview.json()["features"]} == {
            "road-edge", "road-far", "road-main",
        }
        steps = client.get(f"/api/runs/{run_id}/steps").json()["steps"]
        assert len(steps) == 1 and steps[0]["step_key"] == "prepare" and steps[0]["log_available"]
        log_url = f"/api/runs/{run_id}/steps/prepare/log"
        assert "prepare_measurement_lines.py" in client.get(log_url).text
        assert len(client.get(log_url, params={"tail": 2}).text.splitlines()) <= 2

        (workdir / artifact["uri"]).unlink()
        unchanged, = client.get(endpoint).json()["artifacts"]
        assert unchanged == {**artifact, "readable": False}
        assert client.get(f"{artifact_url}/file").status_code == 404
        assert client.get(f"{artifact_url}/preview").status_code == 404


def test_api_lifespan_dispatches_a_confirmed_run_without_manual_claim(project):
    database, _, workspace, draft, catalog, registry = project
    tool = draft.steps[0].tool
    implementation = registry.get(tool.id, tool.version).contract.implementation

    def execute(run):
        return execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry,
            python_implementations={implementation: WORKER},
        )

    app = create_app(
        database, workspace_root=workspace, catalog=catalog, registry=registry, execute_run=execute,
    )
    with TestClient(app) as client:
        base = f"/api/plans/{draft.id}"
        assert client.post(f"{base}/confirm", json={"plan_version": 1}).status_code == 200
        assert not workspace.exists()
        result = client.post(f"{base}/runs", json={"plan_version": 1})
        assert result.status_code == 202
        run_id = result.json()["run_id"]
        deadline = monotonic() + 10
        while True:
            state = client.get(f"/api/runs/{run_id}").json()
            if state["check_status"] == "PASSED" or monotonic() >= deadline:
                break
            sleep(0.05)
        assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "PASSED")
        assert client.get(f"/api/runs/{run_id}/artifacts").json()["artifacts"][0]["readable"]


def test_startup_recovers_only_after_the_actual_previous_worker_stops(project):
    database, _, workspace, draft, catalog, registry = project
    api_options = {"workspace_root": workspace, "catalog": catalog, "registry": registry}
    with TestClient(create_app(database, **api_options)) as client:
        base = f"/api/plans/{draft.id}"
        assert client.post(f"{base}/confirm", json={"plan_version": 1}).status_code == 200
        run_id = client.post(f"{base}/runs", json={"plan_version": 1}).json()["run_id"]
    run = claim_next_run(database)
    assert run.id == run_id
    workdir = Path(run.workdir)
    child = (
        "import time\nfrom pathlib import Path\n"
        "Path('worker.ready').touch()\n"
        "for _ in range(500):\n"
        "    if Path('worker.finish').is_file():\n"
        "        break\n"
        "    time.sleep(0.02)\n"
    )
    supervisor = (
        "import sys\nfrom pathlib import Path\nfrom geopilot.execution.process_manager import capture_process\n"
        f"capture_process([sys.executable, '-c', {child!r}], cwd=Path({str(workdir)!r}), timeout=20)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(WORKER.parents[3])}
    with subprocess.Popen([sys.executable, "-c", supervisor], env=env) as process:
        try:
            deadline = monotonic() + 5
            while not (workdir / "worker.ready").is_file() and monotonic() < deadline:
                sleep(0.02)
            assert (workdir / "worker.ready").is_file()
            process.kill()
            process.wait(timeout=3)
            assert not managed_workers_stopped(workdir)
            app = create_app(database, **api_options, execute_run=lambda _: pytest.fail("unexpected dispatch"))
            with pytest.raises(RuntimeError, match="managed processes have stopped"):
                with TestClient(app):
                    pytest.fail("started while the previous worker was active")
            with open_database(database, read_only=True) as connection:
                assert get_run(connection, run_id).execution_status == "RUNNING"
        finally:
            (workdir / "worker.finish").touch()

    deadline = monotonic() + 5
    while not managed_workers_stopped(workdir) and monotonic() < deadline:
        sleep(0.02)
    assert managed_workers_stopped(workdir)
    with TestClient(app) as client:
        status = client.get(f"/api/runs/{run_id}").json()
    assert (status["execution_status"], status["check_status"]) == ("FAILED", "NOT_CHECKED")
    assert status["error"]["code"] == "RUN_INTERRUPTED"


def test_local_app_confirms_builtin_qgis_method_with_its_registered_precondition(tmp_path):
    qgis = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if qgis is None:
        pytest.skip("Real qgis_process is required")
    database = tmp_path / "app.sqlite"
    app = create_local_app(database, tmp_path / "workspace", qgis_process=Path(qgis))
    config = ROOT / "config"
    method = load_method_catalog(config / "methods").get("attribute-filter-export", 1)
    registry = ToolRegistry(qgis_process=qgis)
    contract = ToolContract.model_validate(yaml.safe_load(
        (config / "tools/qgis-extract-by-attribute.yaml").read_text(encoding="utf-8")
    ))
    registry.register(contract)
    target = register_asset(database, ROOT / "tests/fixtures/vector/road-buffer.gpkg", layer="targets")
    context = build_task_context(
        database, task_id="task-filter", goal=TaskGoal(summary="Filter one named building"),
        method=method, selected={"features": target.ref}, registry=registry,
    )
    draft = build_draft(
        context, method, plan_id="plan-filter",
        options={"field_name": "source_feature_id", "match_value": "building-west"},
    )
    assert not draft.issues
    save_draft(database, draft, context=context)
    with TestClient(app) as client:
        response = client.post("/api/plans/plan-filter/confirm", json={"plan_version": 1})
        assert response.status_code == 200, response.json()
        assert response.json()["status"] == "confirmed"
        response = client.post("/api/plans/plan-filter/runs", json={"plan_version": 1})
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        deadline = monotonic() + 20
        while True:
            run = client.get(f"/api/runs/{run_id}").json()
            if run["execution_status"] == "FAILED" or run["check_status"] == "PASSED" or monotonic() >= deadline:
                break
            sleep(0.1)
        assert (run["execution_status"], run["check_status"]) == ("SUCCEEDED", "PASSED"), run
        artifact, = client.get(f"/api/runs/{run_id}/artifacts").json()["artifacts"]
        assert artifact["readable"] and artifact["metadata"]["feature_count"] == 1
