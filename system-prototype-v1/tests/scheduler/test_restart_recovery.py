import json
import os
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep

import pyogrio
import pytest
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.execution.worker_lease import managed_workers_stopped
from geopilot.persistence.run_repository import list_run_steps
from geopilot.persistence.schema import open_database


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "src/geopilot/gis/tools/prepare_measurement_lines.py"
SUPERVISOR = """import sys
from pathlib import Path
from time import sleep
import yaml
from geopilot.application.plan_runner import execute_claimed_run
from geopilot.domain.tool_contract import ToolContract
from geopilot.scheduler.loop import run_once
from geopilot.tools.registry import ToolRegistry

database, workspace, script, manifest_linked, report_linked = sys.argv[1:]
contract = ToolContract.model_validate(yaml.safe_load(Path('config/tools/python-prepare-measurement-lines.yaml').read_text()))
registry = ToolRegistry(python_implementations={contract.implementation, 'test.measurement_middle'})
registry.register(contract)
registry.register(contract.model_copy(update={
    'id': 'prepare-measurement-middle', 'implementation': 'test.measurement_middle',
}))
if manifest_linked or report_linked:
    hardlink = Path.hardlink_to

    def pause_after_link(destination, source):
        hardlink(destination, source)
        if manifest_linked and destination.name == 'manifest.json' and destination.parent.name == 'outputs':
            Path(manifest_linked).touch()
            sleep(12)
        if report_linked and destination.name == 'report.json' and destination.parent.name == 'checks':
            Path(report_linked).touch()
            sleep(12)

    Path.hardlink_to = pause_after_link

run_once(database, lambda run: execute_claimed_run(
    database, run, workspace_root=workspace, registry=registry,
    python_implementations={contract.implementation: Path(script), 'test.measurement_middle': Path(script)},
))
"""


def _wait_until(predicate, timeout=10):
    deadline = monotonic() + timeout
    while not predicate() and monotonic() < deadline:
        sleep(0.02)
    return predicate()


def _spawn_supervisor(database, workspace, worker, *, manifest_linked=None, report_linked=None):
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    markers = ["" if marker is None else str(marker) for marker in (manifest_linked, report_linked)]
    return subprocess.Popen(
        [sys.executable, "-c", SUPERVISOR, str(database), str(workspace), str(worker), *markers],
        cwd=ROOT, env=environment,
    )


def test_restart_closes_the_interrupted_gis_step_and_keeps_later_steps_queued(queued_run, tmp_path):
    database, workspace, _, _, run_id, workdir = queued_run
    ready = workdir / "worker.ready"
    release = workdir / "worker.finish"
    worker = tmp_path / "gated_geometry.py"
    worker.write_text(
        """from pathlib import Path
from time import sleep
from geopilot.gis.tools.prepare_measurement_lines import prepare_lines
from geopilot.gis.tools.tool_command import run_tool_command

def prepare(request):
    outputs = prepare_lines(request)
    Path('worker.ready').touch()
    for _ in range(500):
        if Path('worker.finish').is_file():
            return outputs
        sleep(0.02)
    raise TimeoutError('Worker gate expired')

raise SystemExit(run_tool_command(prepare, description='Gated geometry preparation'))
""",
        encoding="utf-8",
    )
    with _spawn_supervisor(database, workspace, worker) as process:
        try:
            assert _wait_until(lambda: ready.is_file() or process.poll() is not None)
            assert ready.is_file() and process.poll() is None
            with open_database(database, read_only=True) as connection:
                live, *pending = list_run_steps(connection, run_id)
            assert live.status == "RUNNING" and live.started_at is not None and live.request is not None
            assert [step.status for step in pending] == ["NOT_STARTED", "NOT_STARTED"]
            output = live.request.output_targets["roads"]
            prepared = pyogrio.read_dataframe(workdir / output.path, layer=output.layer)
            assert sorted(prepared.source_feature_id) == ["road-edge", "road-far", "road-main"]
            process.kill()
            process.wait(timeout=3)
            assert not managed_workers_stopped(workdir)
            with pytest.raises(RuntimeError, match="managed processes have stopped"):
                with TestClient(create_app(database, execute_run=lambda _: pytest.fail("unexpected dispatch"))):
                    pytest.fail("started while the old GIS worker still held its lease")
        finally:
            if process.poll() is None:
                process.kill()
            release.touch()

    assert _wait_until(lambda: managed_workers_stopped(workdir))
    with TestClient(create_app(database, execute_run=lambda _: pytest.fail("unexpected dispatch"))) as client:
        status = client.get(f"/api/runs/{run_id}").json()
        diagnostics = client.get(f"/api/runs/{run_id}/artifacts").json()
    assert (status["execution_status"], status["check_status"], status["current_step"]) == (
        "FAILED", "NOT_CHECKED", "prepare",
    )
    assert status["error"]["code"] == "RUN_INTERRUPTED"
    with open_database(database, read_only=True) as connection:
        interrupted, *pending = list_run_steps(connection, run_id)
    assert interrupted.status == "FAILED" and interrupted.started_at == live.started_at
    assert interrupted.finished_at >= interrupted.started_at
    assert interrupted.request == live.request and interrupted.result.exit_code is None
    assert interrupted.result.error.code == "RUN_INTERRUPTED" and interrupted.result.outputs == {}
    assert interrupted.log_path == live.request.log_path == interrupted.result.error.log_path
    log = (workdir / interrupted.log_path).read_text(encoding="utf-8")
    assert any(line.startswith("$ ") and "gated_geometry.py" in line for line in log.splitlines())
    assert diagnostics["artifacts"] == []
    assert any(item["path"] == interrupted.log_path and item["readable"] for item in diagnostics["diagnostics"])
    assert [step.status for step in pending] == ["NOT_STARTED", "NOT_STARTED"]
    assert all(step.result is None and step.request is None for step in pending)
    assert all(not (workdir / "steps" / step.step_key).exists() for step in pending)


def test_restart_removes_uncommitted_artifact_manifest_and_keeps_diagnostics(queued_run, tmp_path):
    database, workspace, _, _, run_id, workdir = queued_run
    linked = tmp_path / "manifest-linked"
    manifest = workdir / "outputs/manifest.json"
    with _spawn_supervisor(database, workspace, WORKER, manifest_linked=linked) as process:
        try:
            assert _wait_until(lambda: linked.is_file() or process.poll() is not None)
            assert linked.is_file() and process.poll() is None
            published = json.loads(manifest.read_text(encoding="utf-8"))["artifacts"]
            assert set(published) == {"roads"}
            with open_database(database, read_only=True) as connection:
                assert connection.execute("SELECT count(*) FROM artifact WHERE run_id = ?", (run_id,)).fetchone()[0] == 0
        finally:
            if process.poll() is None:
                process.kill()
        process.wait(timeout=3)

    assert managed_workers_stopped(workdir)
    with TestClient(create_app(database, execute_run=lambda _: pytest.fail("unexpected dispatch"))) as client:
        state = client.get(f"/api/runs/{run_id}").json()
        queried = client.get(f"/api/runs/{run_id}/artifacts").json()
    assert (state["execution_status"], state["check_status"]) == ("FAILED", "NOT_CHECKED")
    assert state["error"]["code"] == "RUN_INTERRUPTED"
    assert queried["artifacts"] == [] and not manifest.exists()
    assert any(item["path"] == published["roads"]["path"] and item["readable"] for item in queried["diagnostics"])
    with open_database(database, read_only=True) as connection:
        assert [step.status for step in list_run_steps(connection, run_id)] == ["SUCCEEDED"] * 3
        assert connection.execute("SELECT count(*) FROM artifact WHERE run_id = ?", (run_id,)).fetchone()[0] == 0


def test_restart_finalizes_reserved_check_after_uncommitted_report_link(queued_run, tmp_path):
    database, workspace, _, _, run_id, workdir = queued_run
    linked = tmp_path / "report-linked"
    report_path = workdir / "checks/report.json"
    with _spawn_supervisor(database, workspace, WORKER, report_linked=linked) as process:
        try:
            assert _wait_until(lambda: linked.is_file() or process.poll() is not None)
            assert linked.is_file() and process.poll() is None
            uncommitted = json.loads(report_path.read_text(encoding="utf-8"))
            assert uncommitted["status"] == "PASSED"
            with open_database(database, read_only=True) as connection:
                row = connection.execute(
                    "SELECT id, status, report_json FROM check_report WHERE run_id = ?", (run_id,),
                ).fetchone()
                assert tuple(row) == (uncommitted["report_id"], "CHECKING", None)
        finally:
            if process.poll() is None:
                process.kill()
        process.wait(timeout=3)

    with TestClient(create_app(database, execute_run=lambda _: pytest.fail("unexpected dispatch"))) as client:
        status = client.get(f"/api/runs/{run_id}").json()
        checks = client.get(f"/api/runs/{run_id}/checks").json()
        artifacts = client.get(f"/api/runs/{run_id}/artifacts").json()
    assert (status["execution_status"], status["check_status"]) == ("SUCCEEDED", "NOT_CHECKED")
    assert status["error"]["code"] == "CHECK_INTERRUPTED"
    assert status["current_report_id"] == checks["current_report"]["id"] == uncommitted["report_id"]
    assert checks["current_report"]["rule_versions"] == {"output_exists": 1}
    assert checks["current_report"]["report"]["pending_rules"] == ["output_exists"]
    assert checks["current_report"]["report"]["completed_rules"] == []
    assert not report_path.exists()
    assert [artifact["role"] for artifact in artifacts["artifacts"]] == ["roads"]
    assert artifacts["artifacts"][0]["readable"]
    with open_database(database, read_only=True) as connection:
        assert [step.status for step in list_run_steps(connection, run_id)] == ["SUCCEEDED"] * 3
