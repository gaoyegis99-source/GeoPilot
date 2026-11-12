from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep

import pyogrio
import pytest
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.application.plan_runner import execute_claimed_run
from geopilot.persistence.run_repository import list_run_steps
from geopilot.persistence.schema import open_database
from geopilot.scheduler import step_executor
from geopilot.scheduler.loop import run_once


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "src/geopilot/gis/tools/prepare_measurement_lines.py"


def test_queued_cancel_is_never_dispatched(queued_run):
    database, _, _, _, run_id, path = queued_run
    with TestClient(create_app(database)) as client:
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 202 and response.json()["cancel_requested"] is True
        assert run_once(database, lambda run: pytest.fail("cancelled run dispatched")) is None
        state = client.get(f"/api/runs/{run_id}").json()
        assert (state["execution_status"], state["check_status"]) == ("CANCELLED", "NOT_CHECKED")
        again = client.post(f"/api/runs/{run_id}/cancel")
        assert again.status_code == 200 and again.json() == state
    assert not list((path / "steps").iterdir())


def test_active_cancel_waits_for_real_gis_worker_exit_and_stops_dispatch(queued_run, tmp_path):
    database, workspace, registry, implementation, run_id, path = queued_run
    worker = tmp_path / "delayed_measurement.py"
    worker.write_text(
        "import os, runpy, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"try:\n    runpy.run_path({str(WORKER)!r}, run_name='__main__')\n"
        "except SystemExit as error:\n    assert error.code == 0\n"
        "print('Actual GIS output written', flush=True)\n"
        "Path('active.pid').write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    def execute(run):
        return execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry,
            python_implementations={implementation: worker}, timeout=10,
        )

    with TestClient(create_app(database)) as client, ThreadPoolExecutor() as pool:
        call = pool.submit(run_once, database, execute)
        pid_file = path / "active.pid"
        deadline = monotonic() + 5
        while (not pid_file.exists() or not pid_file.stat().st_size) and monotonic() < deadline:
            sleep(0.02)
        pid = int(pid_file.read_text(encoding="utf-8"))
        process_stat = Path(f"/proc/{pid}/stat")
        assert process_stat.exists()
        response = client.post(f"/api/runs/{run_id}/cancel")
        assert response.status_code == 202
        assert response.json()["cancel_requested"] is True
        assert response.json()["execution_status"] == "RUNNING"
        assert call.result(timeout=8) == run_id
        assert not process_stat.exists()
        state = client.get(f"/api/runs/{run_id}").json()
    assert (state["execution_status"], state["check_status"]) == ("CANCELLED", "NOT_CHECKED")
    assert state["current_step"] == "prepare" and state["error"]["code"] == "PROCESS_CANCELLED"
    assert (path / "cancel.flag").is_file()
    with open_database(database) as connection:
        first, second, third = list_run_steps(connection, run_id)
    assert first.status == "CANCELLED" and first.result.exit_code != 0
    assert "Actual GIS output written" in (path / first.log_path).read_text(encoding="utf-8")
    result = first.request.output_targets["roads"]
    assert len(pyogrio.read_dataframe(path / result.path, layer=result.layer)) == 3
    assert second.status == "NOT_STARTED" and not (path / "steps/consume").exists()
    assert third.status == "NOT_STARTED" and not (path / "steps/finalize").exists()
    assert not (path / "outputs/manifest.json").exists()


def test_cancel_between_successful_steps_preserves_the_first_result(queued_run, monkeypatch):
    database, workspace, registry, implementation, run_id, path = queued_run
    with TestClient(create_app(database)) as client:
        run_step = step_executor.run_step

        def run_then_cancel(request, **kwargs):
            assert request.step_key == "prepare"
            result = run_step(request, **kwargs)
            assert result.status == "SUCCEEDED"
            assert client.post(f"/api/runs/{run_id}/cancel").status_code == 202
            return result

        monkeypatch.setattr(step_executor, "run_step", run_then_cancel)
        assert run_once(database, lambda run: execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry,
            python_implementations={implementation: WORKER},
        )) == run_id
        state = client.get(f"/api/runs/{run_id}").json()
    assert state["execution_status"] == "CANCELLED" and state["error"]["code"] == "RUN_CANCELLED"
    with open_database(database) as connection:
        first, second, third = list_run_steps(connection, run_id)
    assert first.status == "SUCCEEDED" and second.status == "NOT_STARTED"
    output = first.result.outputs["roads"]
    assert len(pyogrio.read_dataframe(path / output.path, layer=output.layer)) == 3
    assert not (path / "steps/consume").exists()
    assert third.status == "NOT_STARTED" and not (path / "steps/finalize").exists()
    assert not (path / "outputs/manifest.json").exists()
