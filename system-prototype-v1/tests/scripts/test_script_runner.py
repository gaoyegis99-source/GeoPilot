import json
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from shutil import copyfile
from textwrap import dedent
from threading import Barrier
from time import monotonic, sleep

import pyogrio
import pytest

from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.scripts import runner
from geopilot.scripts.runner import load_script_result, run_script_snapshot
from geopilot.scripts.snapshot import create_script_snapshot, save_script_draft


SOURCE = Path(__file__).resolve().parents[1] / "fixtures/vector/road-buffer.gpkg"
RUN_ID = "script-run"


@pytest.fixture
def project(tmp_path):
    database = tmp_path / "project.sqlite"
    root = tmp_path / "workspace"
    source = tmp_path / "buildings.gpkg"
    copyfile(SOURCE, source)
    initialize_database(database)
    asset = register_asset(database, source, layer="targets")
    return database, root, asset


def _prepare(project, script, *, with_input=False):
    database, root, asset = project
    draft = save_script_draft(
        root, "filter-buildings", script_text=dedent(script),
        inputs={"source": asset.ref} if with_input else {}, parameters={"feature_id": "building-west"},
    )
    snapshot = create_script_snapshot(database, root, draft.id, draft.version, script_run_id=RUN_ID)
    return root, root / "scripts" / snapshot.script_run_id, snapshot


def test_real_script_reads_snapshot_parameters_and_writes_reopenable_geopackage(project):
    database, root, asset = project
    formal = create_run_workspace(root, "formal-task", RUN_ID)
    formal_result = formal.path / "outputs/maps/buildings.gpkg"
    formal_result.parent.mkdir()
    copyfile(SOURCE, formal_result)
    formal_digest = fingerprint_file(formal_result).digest
    root, workdir, snapshot = _prepare(project, """
        import argparse
        import json
        import sys
        from pathlib import Path

        import pyogrio

        parser = argparse.ArgumentParser()
        parser.add_argument('--request', required=True)
        request_path = parser.parse_args().request
        assert request_path == 'snapshot/request.json'
        assert sys.flags.ignore_environment and sys.dont_write_bytecode and sys.flags.utf8_mode
        request = json.loads(Path(request_path).read_text(encoding='utf-8'))
        assert Path.cwd().name == request['script_run_id']
        source = request['asset_files']['source']
        frame = pyogrio.read_dataframe(source['path'], layer=source['layer'])
        chosen = frame[frame.source_feature_id == request['parameters']['feature_id']]
        Path('outputs/maps').mkdir()
        pyogrio.write_dataframe(chosen, 'outputs/maps/buildings.gpkg', layer='selected')
        Path('outputs/summary.txt').write_text(str(len(chosen)), encoding='utf-8')
        print(f'已选择 {len(chosen)} 条')
        print('实际暂存输入已读取', file=sys.stderr)
    """, with_input=True)
    request = workdir / "snapshot/request.json"
    original_request = request.read_bytes()
    asset.source.path.unlink()

    result = run_script_snapshot(root, RUN_ID)

    assert (result.script_run_id, result.script_id, result.version) == (RUN_ID, snapshot.script_id, snapshot.version)
    assert (result.status, result.exit_code, result.timed_out, result.error) == ("SUCCEEDED", 0, False, None)
    assert result.started_at <= result.finished_at
    assert load_script_result(root, RUN_ID) == result
    assert json.loads((workdir / "result.json").read_text(encoding="utf-8")) == result.model_dump(mode="json")
    assert [entry.path for entry in result.outputs] == ["outputs/maps/buildings.gpkg", "outputs/summary.txt"]
    assert all(entry.size_bytes == (workdir / entry.path).stat().st_size for entry in result.outputs)
    output = pyogrio.read_dataframe(workdir / result.outputs[0].path, layer="selected")
    assert output.crs.to_epsg() == 32650 and output.source_feature_id.tolist() == ["building-west"]
    assert (workdir / result.outputs[1].path).read_text(encoding="utf-8") == "1"
    log = (workdir / result.log_path).read_text(encoding="utf-8")
    assert "-E -B -u -X utf8 snapshot/script.py --request snapshot/request.json" in log
    assert "stdout:\n已选择 1 条" in log and "stderr:\n实际暂存输入已读取" in log and "exit_code: 0" in log
    assert request.read_bytes() == original_request
    assert fingerprint_file(workdir / snapshot.asset_files["source"].path).digest == asset.fingerprint
    assert fingerprint_file(formal_result).digest == formal_digest
    with open_database(database, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifact").fetchone()[0] == 0


def test_real_nonzero_exit_keeps_logs_and_unpublished_partial_files(project):
    root, workdir, _ = _prepare(project, """
        import sys
        from pathlib import Path

        Path('outputs/partial.txt').write_text('partial', encoding='utf-8')
        print('before failure')
        print('diagnostic stderr', file=sys.stderr)
        raise SystemExit(7)
    """)
    result = run_script_snapshot(root, RUN_ID)
    assert (result.status, result.exit_code, result.timed_out, result.error.code, result.outputs) == (
        "FAILED", 7, False, "PROCESS_FAILED", (),
    )
    assert result.error.log_path == result.log_path == "process.log"
    assert (workdir / "outputs/partial.txt").read_text(encoding="utf-8") == "partial"
    log_path = workdir / result.log_path
    original = log_path.read_bytes()
    assert b"stdout:\nbefore failure" in original and b"stderr:\ndiagnostic stderr" in original and b"exit_code: 7" in original
    with pytest.raises(FileExistsError):
        run_script_snapshot(root, RUN_ID)
    assert load_script_result(root, RUN_ID) == result
    assert log_path.read_bytes() == original


def _still_executing(pid):
    try:
        process = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return process.rsplit(")", 1)[-1].split()[0] not in {"Z", "X"}


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="Linux process group inspection")
def test_real_timeout_stops_leader_and_descendant_and_retains_streams(project):
    child = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('outputs/descendant.pid').write_text(str(os.getpid())); time.sleep(30)"
    )
    root, workdir, _ = _prepare(project, f"""
        import os
        import subprocess
        import sys
        import time
        from pathlib import Path

        Path('outputs/leader.pid').write_text(str(os.getpid()))
        subprocess.Popen([sys.executable, '-c', {child!r}])
        print('script waiting')
        print('diagnostic before timeout', file=sys.stderr)
        time.sleep(30)
    """)
    try:
        result = run_script_snapshot(root, RUN_ID, timeout=2)
        assert (result.status, result.timed_out, result.error.code, result.outputs) == ("FAILED", True, "PROCESS_TIMEOUT", ())
        assert result.exit_code == -int(signal.SIGTERM)
        assert load_script_result(root, RUN_ID) == result
        log = (workdir / result.log_path).read_text(encoding="utf-8")
        assert "stdout:\nscript waiting" in log and "stderr:\ndiagnostic before timeout" in log
        pids = [int((workdir / "outputs" / name).read_text()) for name in ("leader.pid", "descendant.pid")]
        deadline = monotonic() + 0.5
        while any(_still_executing(pid) for pid in pids) and monotonic() < deadline:
            sleep(0.01)
        assert not any(_still_executing(pid) for pid in pids)
    finally:
        for file in (workdir / "outputs").glob("*.pid"):
            pid = int(file.read_text())
            if _still_executing(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_real_process_start_failure_publishes_error_and_log(project, monkeypatch):
    root, workdir, _ = _prepare(project, "print('should not start')\n")
    missing_python = workdir / "missing-python"
    monkeypatch.setattr(runner.sys, "executable", str(missing_python))
    result = run_script_snapshot(root, RUN_ID)
    assert (result.status, result.exit_code, result.timed_out, result.error.code) == (
        "FAILED", None, False, "PROCESS_START_FAILED",
    )
    assert "missing-python" in (workdir / result.log_path).read_text(encoding="utf-8")
    assert load_script_result(root, RUN_ID) == result


def test_missing_staged_input_is_reported_before_the_child_starts(project):
    root, workdir, snapshot = _prepare(project, "print('should not start')\n", with_input=True)
    (workdir / snapshot.asset_files["source"].path).unlink()
    result = run_script_snapshot(root, RUN_ID)
    assert (result.status, result.exit_code, result.error.code, result.error.field) == (
        "FAILED", None, "SOURCE_UNREADABLE", "asset_files.source.path",
    )
    log = (workdir / result.log_path).read_text(encoding="utf-8")
    assert "SOURCE_UNREADABLE" in log and "should not start" not in log
    assert load_script_result(root, RUN_ID) == result


@pytest.mark.parametrize("output", [
    "from pathlib import Path; Path('outputs/link').symlink_to('../snapshot/request.json')",
    "import os; os.mkfifo('outputs/pipe')",
])
def test_invalid_output_path_or_file_type_never_enters_manifest(project, output):
    root, workdir, _ = _prepare(project, output)
    result = run_script_snapshot(root, RUN_ID)
    assert (result.status, result.exit_code, result.error.code, result.outputs) == ("FAILED", 0, "OUTPUT_INVALID", ())
    assert (workdir / "snapshot/request.json").is_file()
    assert len(list((workdir / "outputs").iterdir())) == 1
    assert load_script_result(root, RUN_ID) == result


def test_concurrent_same_snapshot_runs_only_once(project):
    root, workdir, _ = _prepare(project, """
        from pathlib import Path
        with Path('outputs/once.txt').open('x', encoding='utf-8') as output:
            output.write('executed')
    """)
    start = Barrier(2, timeout=10)

    def execute(_):
        start.wait()
        try:
            return run_script_snapshot(root, RUN_ID)
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, (1, 2)))
    result, = [item for item in outcomes if item is not None]
    assert result.status == "SUCCEEDED" and load_script_result(root, RUN_ID) == result
    assert (workdir / "outputs/once.txt").read_text(encoding="utf-8") == "executed"


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan"), "2"])
def test_invalid_timeouts_are_rejected_before_claiming_snapshot(project, timeout):
    root, workdir, _ = _prepare(project, "print('no run')\n")
    with pytest.raises(ValueError, match="timeout"):
        run_script_snapshot(root, RUN_ID, timeout=timeout)
    assert not (workdir / "process.log").exists()
    with pytest.raises(FileNotFoundError):
        load_script_result(root, RUN_ID)


def test_tampered_snapshot_never_launches_child(project):
    root, workdir, snapshot = _prepare(project, "print('original')\n")
    script = workdir / snapshot.script_path
    script.chmod(0o600)
    script.write_text("print('changed')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        run_script_snapshot(root, RUN_ID)
    assert not (workdir / "process.log").exists()
