import json
import site
import time
from pathlib import Path
from threading import Event

import geopandas as gpd
import pyogrio
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box

from geopilot.api.server import create_local_app
from geopilot.artifacts import dem_area_cache
from geopilot.checks.raster_checks import DEM_ARTIFACT_RULES
from geopilot.persistence.run_repository import get_run_step
from geopilot.persistence.schema import open_database


_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/raster/dem-plane.tif"
_AREA = "calculate_area_with_equal_area_projection"


@pytest.mark.parametrize("module_path", ["src/sitecustomize.py", "src/affine.py", "site/dynamic.pth"])
def test_runtime_cache_bypasses_python_import_overrides(tmp_path, monkeypatch, module_path):
    source = tmp_path / "src/geopilot"
    source.mkdir(parents=True)
    packages = tmp_path / "site"
    packages.mkdir()
    monkeypatch.setenv("PYTHONPATH", "src")
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(packages)])
    monkeypatch.setattr(dem_area_cache, "_SOURCE", source)
    workdir = tmp_path / "run"
    workdir.mkdir()
    assert dem_area_cache._runtime_identity(workdir) is not None
    (tmp_path / module_path).write_text("import os\n", encoding="utf-8")
    assert dem_area_cache._runtime_identity(workdir) is None


def test_runtime_cache_bypasses_worker_relative_import_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "local")
    assert dem_area_cache._runtime_identity(tmp_path) is not None
    (tmp_path / "local").mkdir()
    assert dem_area_cache._runtime_identity(tmp_path) is None


def _post(client, path, data, code=200):
    response = client.post(path, json=data)
    assert response.status_code == code, response.text
    return response.json()


def _await_report(client, run, report=None, expected="PASSED"):
    endpoint = f"/api/runs/{run}"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        response = client.get(endpoint)
        if response.status_code == 200:
            state = response.json()
            assert state["execution_status"] not in {"FAILED", "CANCELLED"}, state
            if state["current_report_id"] and (report is None or state["current_report_id"] == report):
                assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", expected), state
                return client.get(endpoint + "/checks").json()["current_report"]["report"]
        time.sleep(0.1)
    pytest.fail(f"Run did not publish its passing check report: {run}")


def test_real_dem_cache_revalidates_provenance_and_recovers_from_bad_entry(tmp_path, monkeypatch):
    monkeypatch.delenv("GTIFF_POINT_GEO_IGNORE", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    database = tmp_path / "project.sqlite"
    aoi = tmp_path / "analysis.gpkg"
    frame = gpd.GeoDataFrame(geometry=[box(500020, 4400020, 500050, 4400050)], crs="EPSG:32650")
    pyogrio.write_dataframe(frame, aoi, layer="analysis_extent", driver="GPKG")
    with TestClient(create_local_app(database, tmp_path / "workspace", enable_step_cache=True)) as client:
        assets = {}
        for role, path, layer in (("dem", _FIXTURE, None), ("analysis_extent", aoi, "analysis_extent")):
            asset = _post(client, "/api/assets", {"path": str(path), "layer": layer})
            assets[role] = {key: asset[key] for key in ("asset_id", "asset_version")}
        task = _post(client, "/api/tasks", {"goal": {"summary": "验证 DEM 面积缓存"}}, 201)["task_id"]

        def plan(area_crs="EPSG:6933", class_id=7):
            draft = _post(client, f"/api/tasks/{task}/plans", {
                "method": {"id": "dem-terrain-analysis", "version": 1},
                "inputs": assets,
                "critical_options": {
                    "terrain_output": "slope", "slope_unit": "degree", "area_crs": area_crs,
                    "threshold": {"intervals": [{
                        "class_id": class_id, "lower": 45, "upper": 45, "include_lower": True, "include_upper": True,
                    }]},
                },
            })
            endpoint = f"/api/plans/{draft['id']}"
            _post(client, endpoint + "/confirm", {"plan_version": draft["version"]})
            return endpoint, draft["version"]

        def start(confirmed):
            endpoint, version = confirmed
            return _post(client, endpoint + "/runs", {"plan_version": version}, 202)["run_id"]

        def run(confirmed):
            run_id = start(confirmed)
            report = _await_report(client, run_id)
            with open_database(database, read_only=True) as connection:
                step = get_run_step(connection, run_id, _AREA)
            artifact, = (entry for entry in client.get(f"/api/runs/{run_id}/artifacts").json()["artifacts"]
                         if entry["role"] == "area_statistics")
            payload = client.get(f"/api/artifacts/{artifact['id']}/file").content
            return run_id, step.result, payload, report

        confirmed = plan()
        first, cold, expected, _ = run(confirmed)
        assert cold.exit_code == 0 and cold.reuse is None
        second, hit, result, report = run(confirmed)
        assert second != first and hit.exit_code is None and hit.reuse.source_run_id == first
        assert result == expected and json.loads(result)["total_area_m2"] == pytest.approx(900.7204322, abs=1e-5)
        steps = client.get(f"/api/runs/{second}/steps/{_AREA}/log")
        assert "cache reuse:" in steps.text and "exit_code:" not in steps.text
        assert any(finding["rule"] == "step_cache_reuse_matches" and finding["passed"]
                   for finding in report["phase_findings"]["process"])

        entered, resume = Event(), Event()
        rule = "dem_covers_expanded_extent"
        original = DEM_ARTIFACT_RULES[rule]

        def pause(context):
            entered.set()
            assert resume.wait(20)
            return original(context)

        with monkeypatch.context() as checks:
            checks.setitem(DEM_ARTIFACT_RULES, rule, pause)
            interrupted = start(confirmed)
            try:
                assert entered.wait(90)
                _post(client, f"/api/runs/{interrupted}/cancel", {}, 202)
            finally:
                resume.set()
            _await_report(client, interrupted, expected="NOT_CHECKED")
        queued = _post(client, f"/api/runs/{interrupted}/checks", {}, 202)
        recheck = _await_report(client, interrupted, queued["report_id"])
        assert any(finding["rule"] == "step_cache_reuse_matches" and finding["passed"]
                   for finding in recheck["phase_findings"]["process"])

        entry, = tmp_path.glob(".geopilot-step-cache-*/entries/*.cache")
        entry.write_bytes(b"invalid receipt")
        third, fallback, result, _ = run(confirmed)
        assert fallback.exit_code == 0 and fallback.reuse is None and result == expected
        _, repaired, _, _ = run(confirmed)
        assert repaired.reuse.source_run_id == third

        monkeypatch.setenv("GTIFF_POINT_GEO_IGNORE", "TRUE")
        _, configured, _, _ = run(confirmed)
        assert configured.exit_code == 0 and configured.reuse is None
        _, changed, changed_payload, _ = run(plan(class_id=9))
        assert changed.exit_code == 0 and changed.reuse is None
        assert json.loads(changed_payload)["classes"][0]["class_id"] == 9
        _, projected, projected_payload, _ = run(plan(area_crs="EPSG:8857"))
        assert projected.exit_code == 0 and projected.reuse is None
        assert json.loads(projected_payload)["area_crs"] == "EPSG:8857"

    assert not list(tmp_path.glob(".geopilot-step-cache-*"))
