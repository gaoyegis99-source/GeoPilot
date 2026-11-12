import json
import time
from pathlib import Path
from threading import Event

import pytest
import yaml
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.application.plan_runner import execute_claimed_run
from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition
from geopilot.observability import telemetry
from geopilot.observability.telemetry import SIDECAR_NAME, record_run
from geopilot.persistence.run_repository import get_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import save_draft
from geopilot.runs.retry_service import retry_run
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]


def _read_sidecar(workdir):
    deadline = time.monotonic() + 10
    while True:
        try:
            return json.loads((workdir / SIDECAR_NAME).read_text(encoding="utf-8"))
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def test_disabled_telemetry_does_not_write_a_sidecar(tmp_path, monkeypatch):
    monkeypatch.delenv("GEOPILOT_TELEMETRY", raising=False)
    with record_run(tmp_path, "run-disabled") as run:
        assert run is None
    assert not list(tmp_path.iterdir())


def test_publish_never_waits_for_filesystem_and_only_exposes_complete_json(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOPILOT_TELEMETRY", "1")
    publishing, release = Event(), Event()
    publish = telemetry._publish

    def stalled(run):
        publishing.set()
        assert release.wait(6)
        publish(run)

    monkeypatch.setattr(telemetry, "_publish", stalled)
    try:
        with record_run(tmp_path, "run-stalled") as run:
            run.complete("SUCCEEDED", "PASSED")
        assert publishing.wait(2)
        assert not (tmp_path / SIDECAR_NAME).exists()
    finally:
        release.set()
    sidecar = _read_sidecar(tmp_path)
    assert sidecar["execution_status"] == "SUCCEEDED" and sidecar["run_id"] == "run-stalled"
    assert sidecar["resources"]["cpu_ns"] == {
        "status": "unavailable", "value": None, "reason": "no_subprocess", "observed_processes": 0, "processes": 0,
    }


@pytest.mark.parametrize("collision", ["file", "symlink"])
def test_existing_sidecar_path_is_never_replaced(tmp_path, collision):
    workdir = tmp_path / "run"
    workdir.mkdir()
    target = workdir / SIDECAR_NAME
    outside = tmp_path / "outside"
    outside.write_text("original")
    if collision == "file":
        target.write_text("existing")
    else:
        target.symlink_to(outside)
    with pytest.raises(FileExistsError):
        telemetry._publish(telemetry.RunTelemetry(workdir, "run", None))
    assert target.read_text() == ("existing" if collision == "file" else "original")
    assert outside.read_text() == "original"
    assert list(workdir.iterdir()) == [target]


def test_publisher_continues_after_storage_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOPILOT_TELEMETRY", "1")
    publish = telemetry._publish

    def denied(run):
        if run.run_id == "run-denied":
            raise PermissionError("read-only diagnostics")
        publish(run)

    monkeypatch.setattr(telemetry, "_publish", denied)
    for run_id in ("run-denied", "run-next"):
        workdir = tmp_path / run_id
        workdir.mkdir()
        with record_run(workdir, run_id) as run:
            run.complete("SUCCEEDED", "PASSED")
    assert _read_sidecar(tmp_path / "run-next")["run_id"] == "run-next"
    assert not list((tmp_path / "run-denied").iterdir())


def _application(tmp_path):
    database = tmp_path / "project.sqlite"
    workspace = tmp_path / "workspace"
    initialize_database(database)
    asset = register_asset(database, ROOT / "tests/fixtures/vector/road-buffer.gpkg", layer="roads")
    tool = ToolContract.model_validate(yaml.safe_load(
        (ROOT / "config/tools/python-prepare-measurement-lines.yaml").read_text(encoding="utf-8")
    ))
    worker = ROOT / "src/geopilot/gis/tools/prepare_measurement_lines.py"
    registry = ToolRegistry(python_implementations={tool.implementation})
    registry.register(tool)
    parameters = {"method": {"literal": "local_projected"}, "target_crs": {"literal": "EPSG:32650"}}
    method = MethodDefinition.model_validate({
        "id": "prepare-lines", "version": 1, "locked_order": True, "inputs": {"lines": "vector_line"},
        "critical_options": {},
        "steps": [
            {"key": "prepare", "tool": tool.ref, "inputs": {"source": {"asset": "lines"}},
             "parameters": parameters, "outputs": {"roads": "vector_line"}},
            {"key": "repeat", "tool": tool.ref, "inputs": {"source": {"step": "prepare", "output": "roads"}},
             "parameters": parameters, "outputs": {"roads": "vector_line"}},
        ],
        "expected_artifacts": [{"role": "roads", "step": "repeat", "output": "roads", "type": "vector_line"}],
        "check_rules": ["output_exists"],
    })
    context = build_task_context(
        database, task_id="task-lines", goal=TaskGoal(summary="Prepare the measured road input"),
        method=method, selected={"lines": asset.ref}, registry=registry,
    )
    plan = build_draft(context, method, plan_id="plan-lines")
    assert not plan.issues
    save_draft(database, plan, context=context)

    def execute(run):
        return execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry, python_implementations={tool.implementation: worker},
        )

    return create_app(
        database, workspace_root=workspace, catalog=MethodCatalog([method], ()), registry=registry, execute_run=execute,
    ), database, plan.id


def test_claimed_run_and_retry_publish_distinct_versioned_sidecars_without_artifact_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOPILOT_TELEMETRY", "1")
    app, database, plan_id = _application(tmp_path)
    run_ids = []
    with TestClient(app) as client:
        plan_path = f"/api/plans/{plan_id}"
        assert client.post(plan_path + "/confirm", json={"plan_version": 1}).status_code == 200
        for attempt in range(2):
            if attempt == 0:
                response = client.post(plan_path + "/runs", json={"plan_version": 1})
                assert response.status_code == 202, response.text
                run_id = response.json()["run_id"]
            else:
                run_id = retry_run(database, run_ids[-1], workspace_root=tmp_path / "workspace")
            assert run_id not in run_ids
            run_ids.append(run_id)
            with open_database(database, read_only=True) as connection:
                workdir = Path(get_run(connection, run_id).workdir)
            document = _read_sidecar(workdir)
            assert document["schema_version"] == 1 and document["run_id"] == run_id
            assert document["execution_status"] == "SUCCEEDED" and document["check_status"] == "PASSED"
            assert document["exit_code"] == 0 and document["wall_ns"] > 0
            assert document["queue_wall_ns"]["status"] == "recorded"
            assert document["wall_ns"] == document["execute_steps_wall_ns"] + document["finalize_wall_ns"]
            assert [row["step_key"] for row in document["steps"]] == ["prepare", "repeat"]
            for row in document["steps"]:
                assert row["tool"] == {"id": "python-prepare-measurement-lines", "version": 1, "runner": "python"}
                assert row["exit_code"] == 0 and row["runtime_versions"]["values"]["python"]
                process, = row["processes"]
                assert row["wall_ns"] >= process["wall_ns"] > 0 and process["phase"] == "execute"
            cpu = document["resources"]["cpu_ns"]
            assert cpu["processes"] == 2
            if cpu["observed_processes"]:
                assert cpu["value"] >= 0 and cpu["status"] == "partial"
            else:
                assert cpu["value"] is None and cpu["status"] == "unavailable"

            persisted = json.loads((workdir / "outputs/manifest.json").read_text(encoding="utf-8"))
            entry, = persisted["artifacts"].values()
            assert entry["path"] != SIDECAR_NAME
            artifacts = client.get(f"/api/runs/{run_id}/artifacts").json()
            artifact, = artifacts["artifacts"]
            assert artifact["uri"] == entry["path"] and artifact["metadata"]["feature_count"] == 3
            assert all(item.get("uri") != SIDECAR_NAME for item in artifacts["diagnostics"])
            assert (workdir / SIDECAR_NAME).stat().st_mode & 0o777 == 0o600
    assert len(run_ids) == 2
