import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
from rasterio.transform import from_origin

from geopilot.domain.step_io import ExecutionStatus, StepFileRef, StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.output_staging import OUTPUT_STAGING_ENV
from geopilot.execution.runner import run_step
from geopilot.execution.step_support import OUTPUT_STAGING_RECEIPT
from geopilot.execution.worker_lease import managed_workers_stopped
from geopilot.persistence.run_repository import get_run, get_run_step, insert_run, save_run_step
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.runs.models import Run, RunStep
from geopilot.scheduler.loop import recover_interrupted_runs
from geopilot.tools.registry import ToolRegistry


IMPLEMENTATION = "geopilot.test_output_staging"
ROOT = Path(__file__).resolve().parents[2]
STEP = "steps/change"
SENTINELS = {
    "inputs/source.json": "source content",
    "outputs/already-published.json": "published content",
    "intermediate/.land-use-unrelated/history.json": "older run content",
}
CHILD = '''
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.methods.land_use_overlay import _stage_outputs


def write(request):
    mode = os.environ["TEST_STAGE_MODE"]
    outputs = request["output_targets"]
    with _stage_outputs(outputs) as staged, TemporaryDirectory(prefix=".ndvi-classification-",
                                                               dir=Path(staged["change"]["path"]).parent) as nested:
        records = []
        for role, reference in staged.items():
            path = Path(reference["path"])
            path.write_text(json.dumps({"role": role}))
            directory = path.parent.parent
            target_parent = Path(outputs[role]["path"]).parent.resolve()
            assert directory.parent == target_parent
            assert directory.stat().st_dev == target_parent.stat().st_dev
            assert directory.stat().st_mode & 0o777 == 0o700
            records.append(str(directory))
        assert len(set(records)) == 2
        (Path(nested) / "classes.tif").write_bytes(b"classified")
        Path(".ready-pending").write_text(json.dumps({"pid": os.getpid(), "stages": records}))
        Path(".ready-pending").replace("ready.json")
        if mode in {"timeout", "cancel"}:
            time.sleep(60)
        if mode == "partial":
            link = os.link

            def pause_after_link(source, target):
                link(source, target)
                print(f"partially linked {target}", flush=True)
                Path("partial-published").touch()
                time.sleep(60)

            os.link = pause_after_link
    return outputs


raise SystemExit(run_tool_command(write, description="Exercise the real staging and process protocol"))
'''
SUPERVISOR = '''
import os
from pathlib import Path

from geopilot.domain.step_io import StepRequest
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.tools.registry import ToolRegistry

work = Path.cwd()
contract = ToolContract.model_validate_json(Path("contract.json").read_bytes())
registry = ToolRegistry(python_implementations=(contract.implementation,))
registry.register(contract)
request = StepRequest.model_validate_json(Path("supervisor-request.json").read_bytes())
run_step(request, registry=registry, workdir=work,
         python_implementations={contract.implementation: work / "worker.py"}, timeout=30)
'''


@pytest.fixture
def staging(tmp_path):
    work = tmp_path / "run"
    for folder in ("inputs", "outputs", "intermediate/.land-use-unrelated", STEP):
        (work / folder).mkdir(parents=True)
    for path, content in SENTINELS.items():
        (work / path).write_text(content)
    script = work / "worker.py"
    script.write_text(CHILD, encoding="utf-8")
    contract = ToolContract.model_validate({
        "id": "python-test-output-staging", "version": 1, "runner": "python", "implementation": IMPLEMENTATION,
        "inputs": {"source": "json"}, "outputs": {"change": "json", "report": "json"},
        "parameters": {}, "preconditions": [],
        "measurement_policy": {"area": "not_applicable", "distance": "not_applicable"},
        "check_rules": ["output_exists"],
    })
    registry = ToolRegistry(python_implementations=(IMPLEMENTATION,))
    registry.register(contract)
    request = StepRequest(
        run_id="staging-test", step_key="change", tool=contract.ref, workdir_ref="staging-test",
        inputs={"source": {"path": "inputs/source.json"}}, parameters={},
        output_targets={"change": {"path": "intermediate/change.json"}, "report": {"path": "outputs/report.json"}},
        log_path=f"{STEP}/process.log", cancel_path="cancel.flag",
    )
    return work, request, script, contract, registry


def _ready(work):
    return json.loads((work / "ready.json").read_text())


def _assert_sentinels(work):
    for path, content in SENTINELS.items():
        assert (work / path).read_text() == content


@pytest.mark.parametrize("collision", ["log_path", "cancel_path", "output"])
def test_receipt_uses_alternate_name_for_previously_supported_paths(staging, monkeypatch, collision):
    work, request, script, _, registry = staging
    receipt = f"{STEP}/{OUTPUT_STAGING_RECEIPT}"
    monkeypatch.setenv("TEST_STAGE_MODE", "success")
    if collision == "output":
        request = request.model_copy(update={"output_targets": {
            **request.output_targets, "report": StepFileRef(path=receipt),
        }})
    else:
        request = request.model_copy(update={collision: receipt})
    result = run_step(request, registry=registry, workdir=work, python_implementations={IMPLEMENTATION: script})
    assert result.status is ExecutionStatus.SUCCEEDED and result.exit_code == 0, result
    assert not (work / STEP / ".python-output-staging-1.json").exists()
    assert not any(path.is_dir() for path in work.rglob(".python-output-*"))
    if collision == "output":
        assert json.loads((work / receipt).read_text()) == {"role": "report"}
    elif collision == "log_path":
        assert "exit_code: 0" in (work / receipt).read_text()
    else:
        assert not (work / receipt).exists()


def test_existing_receipt_stops_before_opening_log(staging):
    work, request, script, _, registry = staging
    receipt = work / STEP / OUTPUT_STAGING_RECEIPT
    receipt.write_text("previous receipt")
    result = run_step(request, registry=registry, workdir=work, python_implementations={IMPLEMENTATION: script})
    assert result.error.code == "INVALID_STEP_REQUEST" and result.error.phase == "preflight"
    assert result.error.message == "The Python staging receipt location is already in use"
    assert result.log_path is None and not (work / STEP / "process.log").exists()
    assert not (work / STEP / "request.json").exists() and not (work / "ready.json").exists()
    assert receipt.read_text() == "previous receipt"


def test_production_remote_change_retains_all_three_checked_outputs(tmp_path):
    for folder in ("inputs", "outputs", "intermediate", STEP):
        (tmp_path / folder).mkdir(parents=True)
    aligned = tmp_path / "inputs/aligned.tif"
    with rasterio.open(
        aligned, "w", driver="GTiff", width=4, height=1, count=2, dtype="float64",
        crs="EPSG:32632", transform=from_origin(500000, 5100000, 10, 10), nodata=np.nan,
    ) as dataset:
        dataset.descriptions = ("first_ndvi", "second_ndvi")
        dataset.units = ("1", "1")
        dataset.write(np.asarray([[[0.5, 0.2, 0.25, np.nan]], [[0.25, 0.2, 0.75, np.nan]]]))
    source = aligned.read_bytes()
    tool_path = ROOT / "config/tools/python-remote-sensing-calculate-change.yaml"
    contract = ToolContract.model_validate(yaml.safe_load(tool_path.read_text()))
    registry = ToolRegistry(python_implementations=(contract.implementation,))
    registry.register(contract)
    request = StepRequest(
        run_id="change", step_key="change", tool=contract.ref, workdir_ref="change",
        inputs={"aligned_ndvi": {"path": "inputs/aligned.tif"}},
        parameters={"change_threshold": 0.1, "area_crs": "EPSG:6933"},
        output_targets={
            "change_raster": {"path": "intermediate/change.tif"},
            "change_classes": {"path": "intermediate/classes.tif"},
            "area_statistics": {"path": "outputs/statistics.json"},
        },
        log_path=f"{STEP}/process.log", cancel_path="cancel.flag",
    )
    worker = ROOT / "src/geopilot/gis/tools/remote_sensing_workflow.py"
    result = run_step(request, registry=registry, workdir=tmp_path, python_implementations={contract.implementation: worker})
    assert (result.status, result.exit_code) == (ExecutionStatus.SUCCEEDED, 0), result
    with rasterio.open(tmp_path / result.outputs["change_raster"].path) as change:
        np.testing.assert_array_equal(change.read(1), [[-0.25, 0, 0.5, np.nan]])
    with rasterio.open(tmp_path / result.outputs["change_classes"].path) as classes:
        assert classes.read(1).tolist() == [[1, 2, 3, 255]]
    report = json.loads((tmp_path / result.outputs["area_statistics"].path).read_text())
    assert [entry["pixel_count"] for entry in report["classes"]] == [1, 1, 1]
    assert aligned.read_bytes() == source
    assert not list(tmp_path.rglob(".python-output-*")) and not list(tmp_path.rglob(".land-use-*"))
    assert not (tmp_path / STEP / OUTPUT_STAGING_RECEIPT).exists()
    print(json.dumps({"mode": "production-remote-change", "status": result.status, "exit": result.exit_code,
                      "classes": [1, 2, 3, 255], "outputs": sorted(result.outputs)}))


@pytest.mark.parametrize(("mode", "status", "exit_code", "code"), [
    ("success", ExecutionStatus.SUCCEEDED, 0, None),
    ("timeout", ExecutionStatus.FAILED, -signal.SIGTERM, "PROCESS_TIMEOUT"),
    ("cancel", ExecutionStatus.CANCELLED, -signal.SIGTERM, "PROCESS_CANCELLED"),
    ("partial", ExecutionStatus.CANCELLED, -signal.SIGTERM, "PROCESS_CANCELLED"),
])
def test_real_python_step_reclaims_only_its_staging(staging, monkeypatch, mode, status, exit_code, code):
    work, request, script, _, registry = staging
    monkeypatch.setenv("TEST_STAGE_MODE", mode)
    monkeypatch.setenv(OUTPUT_STAGING_ENV, '{"stale": true}')
    cancel_marker = {"cancel": "ready.json", "partial": "partial-published"}.get(mode)

    result = run_step(
        request, registry=registry, workdir=work, python_implementations={IMPLEMENTATION: script},
        timeout=2 if mode == "timeout" else 8,
        cancel_requested=None if cancel_marker is None else (work / cancel_marker).is_file,
    )

    assert (result.status, result.exit_code) == (status, exit_code), result
    assert (None if result.error is None else result.error.code) == code
    assert StepResult.model_validate_json((work / STEP / "result.json").read_bytes()) == result
    ready = _ready(work)
    assert len(ready["stages"]) == 2 and all(not Path(path).exists() for path in ready["stages"])
    assert not list(work.rglob(".python-output-*"))
    assert not (work / STEP / OUTPUT_STAGING_RECEIPT).exists()
    assert managed_workers_stopped(work)
    expected_published = {"change", "report"} if mode == "success" else {"change"} if mode == "partial" else set()
    published = {
        role for role, ref in request.output_targets.items() if (work / ref.path).exists()
    }
    assert published == expected_published
    for role in published:
        assert json.loads((work / request.output_targets[role].path).read_text()) == {"role": role}
    assert set(result.outputs) == (expected_published if mode == "success" else set())
    _assert_sentinels(work)
    print(json.dumps({"mode": mode, "status": status, "exit": exit_code, "error": code,
                      "stage_paths_removed": ready["stages"], "published_preserved": sorted(published)}))


def _persist_active_run(database, work, request):
    initialize_database(database)
    with open_database(database) as connection, connection:
        connection.execute("INSERT INTO task VALUES ('task', '{}', NULL, '2026-11-12')")
        connection.execute("INSERT INTO analysis_plan VALUES ('plan', 1, 'task', 'confirmed', '{}', '2026-11-12')")
        insert_run(connection, Run(
            id=request.run_id, plan_id="plan", plan_version=1, workdir=str(work), created_at=datetime.now(UTC),
            execution_status=ExecutionStatus.RUNNING, current_step=request.step_key,
        ))
        save_run_step(connection, RunStep(
            run_id=request.run_id, step_key=request.step_key, position=0,
            status=ExecutionStatus.RUNNING, request=request, log_path=request.log_path,
        ))


def _wait_for(condition):
    deadline = time.monotonic() + 6
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_real_startup_recovery_waits_for_inherited_lease_after_supervisor_sigkill(staging):
    work, request, _, contract, _ = staging
    request = request.model_copy(update={"log_path": f"{STEP}/{OUTPUT_STAGING_RECEIPT}"})
    (work / "contract.json").write_text(contract.model_dump_json())
    (work / "supervisor-request.json").write_text(request.model_dump_json())
    database = work.parent / "project.sqlite"
    _persist_active_run(database, work, request)
    receipt = work / STEP / ".python-output-staging-1.json"
    child_pid = None
    with subprocess.Popen([sys.executable, "-c", SUPERVISOR], cwd=work,
                          env={**os.environ, "TEST_STAGE_MODE": "partial"}) as supervisor:
        try:
            _wait_for(lambda: (work / "partial-published").is_file() or supervisor.poll() is not None)
            assert supervisor.poll() is None
            ready = _ready(work)
            child_pid = ready["pid"]
            stages = {Path(path) for path in ready["stages"]}
            recorded = json.loads(receipt.read_text())
            assert {work / entry["path"] for entry in recorded} == stages
            for entry in recorded:
                info = (work / entry["path"]).stat()
                assert (entry["device"], entry["inode"]) == (info.st_dev, info.st_ino)

            supervisor.kill()
            assert supervisor.wait(timeout=3) == -signal.SIGKILL
            with pytest.raises(RuntimeError, match="until its managed processes have stopped"):
                # The recovery code must still test the real worker lease even if this verifier says yes.
                recover_interrupted_runs(database, processes_stopped=lambda run: True)
            assert receipt.is_file() and all(path.is_dir() for path in stages)
            with open_database(database) as connection:
                assert get_run(connection, request.run_id).execution_status is ExecutionStatus.RUNNING

            os.kill(child_pid, signal.SIGTERM)
            _wait_for(lambda: managed_workers_stopped(work))
            child_pid = None
            assert recover_interrupted_runs(database, processes_stopped=lambda run: managed_workers_stopped(work)) == (
                request.run_id,
            )
            assert not receipt.exists() and all(not path.exists() for path in stages)
            assert "worker.py --request" in (work / request.log_path).read_text()
            assert json.loads((work / "intermediate/change.json").read_text()) == {"role": "change"}
            assert not (work / "outputs/report.json").exists()
            _assert_sentinels(work)
            with open_database(database) as connection:
                run = get_run(connection, request.run_id)
                step = get_run_step(connection, request.run_id, request.step_key)
            assert run.execution_status is ExecutionStatus.FAILED and run.error.code == "RUN_INTERRUPTED"
            assert step.status is ExecutionStatus.FAILED and step.result.error.code == "RUN_INTERRUPTED"
            print(json.dumps({"mode": "restart", "supervisor_exit": -signal.SIGKILL, "lease_blocked_recovery": True,
                              "run_error": run.error.code, "partial_hard_link_preserved": True,
                              "stage_paths_removed": sorted(map(str, stages))}))
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
            if child_pid is None and not managed_workers_stopped(work) and (work / "ready.json").is_file():
                child_pid = _ready(work)["pid"]
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize("case", ["different-inode", "unapproved-parent", "symlink"])
def test_real_recovery_leaves_unowned_directories_untouched(staging, case):
    work, request, _, _, _ = staging
    database = work.parent / "project.sqlite"
    _persist_active_run(database, work, request)
    receipt = work / STEP / OUTPUT_STAGING_RECEIPT
    stage_a = work / "intermediate" / (".python-output-" + "a" * 32)
    stage_b = work / ("inputs" if case == "unapproved-parent" else "outputs") / (".python-output-" + "b" * 32)
    for directory in (stage_a, stage_b):
        directory.mkdir()
        (directory / "retained").write_text("retained")
    records = [{"path": path.relative_to(work).as_posix(), "device": path.stat().st_dev, "inode": path.stat().st_ino}
               for path in (stage_a, stage_b)]
    if case != "unapproved-parent":
        saved = stage_b.with_name("old-stage")
        stage_b.rename(saved)
        if case == "symlink":
            stage_b.symlink_to(saved, target_is_directory=True)
        else:
            stage_b.mkdir()
            (stage_b / "retained").write_text("replacement")
    receipt.write_text(json.dumps(records))
    assert recover_interrupted_runs(database, processes_stopped=lambda run: True) == (request.run_id,)
    assert receipt.is_file() and (stage_b / "retained").is_file()
    assert stage_a.exists() == (case in {"unapproved-parent", "symlink"})
    _assert_sentinels(work)
