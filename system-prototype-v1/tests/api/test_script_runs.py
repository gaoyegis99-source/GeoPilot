import json
from pathlib import Path
from shutil import copyfile
from textwrap import dedent

import pyogrio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from geopilot.api.script_routes import script_routes
from geopilot.data.assets import register_asset
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.scripts.runner import run_script_snapshot
from geopilot.scripts.snapshot import create_script_snapshot, save_script_draft


SOURCE = Path(__file__).resolve().parents[1] / "fixtures/vector/road-buffer.gpkg"


def _app(workspace):
    app = FastAPI()
    app.include_router(script_routes(workspace))
    return app


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("script-browse")
    workspace = root / "workspace"
    database = root / "project.sqlite"
    initialize_database(database)
    asset = register_asset(database, SOURCE, layer="targets")
    draft = save_script_draft(
        workspace, "filter-buildings", inputs={"source": asset.ref}, parameters={"feature": "building-west"},
        script_text=dedent("""
            import argparse
            import json
            import sys
            from pathlib import Path

            import pyogrio

            parser = argparse.ArgumentParser()
            parser.add_argument('--request')
            request = json.loads(Path(parser.parse_args().request).read_text(encoding='utf-8'))
            reference = request['asset_files']['source']
            source = pyogrio.read_dataframe(reference['path'], layer=reference['layer'])
            selected = source[source.source_feature_id == request['parameters']['feature']]
            Path('outputs/spatial').mkdir()
            pyogrio.write_dataframe(selected, 'outputs/spatial/filtered.gpkg', layer='filtered')
            Path('outputs/summary.txt').write_text(str(len(selected)), encoding='utf-8')
            print('真实筛选完成')
            print('diagnostic stderr', file=sys.stderr)
        """),
    )
    for run_id in ("completed", "ready", "unfinished"):
        create_script_snapshot(database, workspace, draft.id, draft.version, script_run_id=run_id)
    result = run_script_snapshot(workspace, "completed")
    assert result.status == "SUCCEEDED"
    (workspace / "scripts/unfinished/process.log").write_text("$ python snapshot/script.py\n", encoding="utf-8")
    failure = save_script_draft(
        workspace, "filter-buildings", script_text="print('stopped')\nraise SystemExit(9)\n", inputs={}, parameters={},
    )
    create_script_snapshot(database, workspace, failure.id, failure.version, script_run_id="failed")
    assert run_script_snapshot(workspace, "failed").status == "FAILED"
    formal = create_run_workspace(workspace, "formal-task", "formal-only")
    copyfile(SOURCE, formal.path / "outputs/formal.gpkg")
    (workspace / "scripts/unpublished").mkdir()
    return database, workspace


def test_lists_only_published_script_snapshots_and_truthful_statuses(project):
    database, workspace = project
    with TestClient(_app(workspace)) as client:
        listed = client.get("/api/script-runs")
        assert listed.status_code == 200, listed.text
        runs = {run["script_run_id"]: run for run in listed.json()["runs"]}
        assert {name: item["status"] for name, item in runs.items()} == {
            "completed": "SUCCEEDED", "ready": "NOT_STARTED", "unfinished": "RESULT_PENDING", "failed": "FAILED",
        }
        assert all(item["script_id"] == "filter-buildings" for item in runs.values())
        assert runs["failed"]["version"] == 2 and runs["completed"]["version"] == 1
        assert runs["unfinished"]["log_available"] and not runs["ready"]["log_available"]
        assert str(workspace) not in listed.text
        assert client.get("/api/script-runs/formal-only").status_code == 404
        assert client.get("/api/script-runs/unpublished").status_code == 404
        assert client.get("/api/script-runs/drafts").status_code == 404
        assert client.post("/api/script-runs/ready").status_code == 405
        assert not (workspace / "scripts/ready/process.log").exists()
    with open_database(database, read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM artifact").fetchone()[0] == 0


def test_reads_actual_log_geopackage_download_and_wgs84_preview(project):
    _, workspace = project
    endpoint = "/api/script-runs/completed"
    actual = workspace / "scripts/completed/outputs/spatial/filtered.gpkg"
    log = workspace / "scripts/completed/process.log"
    with TestClient(_app(workspace)) as client:
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        detail = response.json()
        assert (detail["script_run_id"], detail["version"], detail["status"], detail["exit_code"]) == ("completed", 1, "SUCCEEDED", 0)
        assert detail["timed_out"] is False and detail["error"] is None
        spatial, text = detail["outputs"]
        assert spatial == {"index": 0, "path": "outputs/spatial/filtered.gpkg", "size_bytes": actual.stat().st_size,
                           "readable": True, "preview_available": True}
        assert text == {"index": 1, "path": "outputs/summary.txt", "size_bytes": 1,
                        "readable": True, "preview_available": False}
        assert str(workspace) not in response.text

        log_response = client.get(f"{endpoint}/log")
        assert log_response.status_code == 200 and log_response.content == log.read_bytes()
        assert "stdout:\n真实筛选完成" in log_response.text and "stderr:\ndiagnostic stderr" in log_response.text
        tail = client.get(f"{endpoint}/log", params={"tail": 2})
        assert tail.status_code == 200 and "exit_code: 0" in tail.text and "stdout:" not in tail.text

        download = client.get(f"{endpoint}/outputs/0/file")
        assert download.status_code == 200 and download.content == actual.read_bytes()
        assert "attachment" in download.headers["content-disposition"] and "filtered.gpkg" in download.headers["content-disposition"]
        assert download.headers["content-type"] == "application/octet-stream"
        assert client.get(f"{endpoint}/outputs/1/file").content == b"1"
        assert client.get(f"{endpoint}/outputs/1/preview").status_code == 422
        assert client.get(f"{endpoint}/outputs/99/file").status_code == 404

        preview = client.get(f"{endpoint}/outputs/0/preview")
        assert preview.status_code == 200 and preview.headers["content-type"].startswith("application/geo+json")
        feature, = preview.json()["features"]
        assert feature["properties"]["source_feature_id"] == "building-west"
        output = pyogrio.read_dataframe(actual, layer="filtered")
        assert output.crs.to_epsg() == 32650
        assert feature["geometry"]["coordinates"] == pytest.approx(output.to_crs(4326).geometry.iloc[0].coords[0])


def test_failed_run_retains_exit_code_and_actual_log_without_output_registration(project):
    _, workspace = project
    with TestClient(_app(workspace)) as client:
        endpoint = "/api/script-runs/failed"
        result = client.get(endpoint).json()
        assert result["status"] == "FAILED" and result["exit_code"] == 9
        assert result["error"]["code"] == "PROCESS_FAILED" and result["outputs"] == []
        assert "stopped" in client.get(f"{endpoint}/log").text
        assert client.get("/api/script-runs/ready/log").status_code == 404


def test_lost_or_linked_outputs_keep_manifest_and_cannot_be_opened(project, tmp_path):
    _, workspace = project
    actual = workspace / "scripts/completed/outputs/spatial/filtered.gpkg"
    backup = tmp_path / "outside.gpkg"
    actual.rename(backup)
    try:
        with TestClient(_app(workspace)) as client:
            endpoint = "/api/script-runs/completed"
            assert client.get(f"{endpoint}/outputs/0/file").status_code == 404
            detail = client.get(endpoint).json()
            assert detail["status"] == "SUCCEEDED" and detail["outputs"][0]["readable"] is False
            actual.symlink_to(backup)
            assert client.get(f"{endpoint}/outputs/0/file").status_code == 422
            assert client.get(f"{endpoint}/outputs/0/preview").status_code == 422
            assert client.get(endpoint).json()["outputs"][0]["readable"] is False
    finally:
        actual.unlink(missing_ok=True)
        backup.rename(actual)


def test_result_identity_must_match_exact_snapshot_version(project):
    _, workspace = project
    path = workspace / "scripts/completed/result.json"
    saved = path.read_bytes()
    path.chmod(0o600)
    try:
        changed = {**json.loads(saved), "version": 999}
        path.write_text(json.dumps(changed), encoding="utf-8")
        with TestClient(_app(workspace)) as client:
            response = client.get("/api/script-runs/completed")
            assert response.status_code == 422 and response.json()["detail"]["code"] == "SCRIPT_RUN_UNREADABLE"
            assert client.get("/api/script-runs/completed/outputs/0/file").status_code == 422
    finally:
        path.write_bytes(saved)
        path.chmod(0o400)


def test_empty_workspace_returns_an_empty_catalog(tmp_path):
    workspace = tmp_path / "workspace"
    with TestClient(_app(workspace)) as client:
        assert client.get("/api/script-runs").json() == {"runs": []}
    assert not workspace.exists()
