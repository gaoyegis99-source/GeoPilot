import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

import pytest

from geopilot.execution.process_manager import capture_process
from geopilot.execution.worker_lease import managed_workers_stopped
from geopilot.observability import process as sampling
from geopilot.observability.process import ProcessProbe


def _python(code, workdir, probe, **options):
    return capture_process([sys.executable, "-c", code], cwd=workdir, probe=probe, **options)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs")
def test_live_process_tree_records_cpu_rss_and_storage_counters(tmp_path):
    child = (
        "import os, time\n"
        "payload = bytearray(b'a') * (24 * 1024 * 1024)\n"
        "with open('worker-data', 'wb') as f:\n"
        "    f.write(payload)\n"
        "    f.flush()\n"
        "    os.fsync(f.fileno())\n"
        "until = time.process_time() + 0.24\n"
        "while time.process_time() < until: pass\n"
        "time.sleep(0.22)\n"
    )
    parent = f"import subprocess, sys\nsubprocess.run([sys.executable, '-c', {child!r}], check=True)\n"
    probe = ProcessProbe("execute")
    result = _python(parent, tmp_path, probe, timeout=8)
    row = probe.outcome

    assert result.exit_code == row["exit_code"] == 0
    assert row["resource_scope"] == "local_process_session"
    assert row["sample_count"] >= 2 and row["peak_sampled_processes"] == 2
    assert 0 <= row["first_sample_ns"] < row["last_sample_ns"] < row["wall_ns"]
    assert row["resources"]["peak_rss_bytes"]["value"] >= 24 * 1024 * 1024
    cpu = row["resources"]["cpu_ns"]
    assert cpu["status"] == "partial" and cpu["value"] >= 100_000_000
    assert row["resources"]["disk_read_bytes"]["value"] >= 0
    assert row["resources"]["disk_write_bytes"]["value"] >= 0
    assert (tmp_path / "worker-data").stat().st_size == 24 * 1024 * 1024
    assert managed_workers_stopped(tmp_path)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs")
def test_open_proc_directory_keeps_identity_through_process_exit():
    with subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE) as process:
        fd = sampling._open_process_directory(process.pid)
        try:
            pinned = Path(f"/proc/self/fd/{fd}")
            assert sampling._read_stat(pinned).parent == os.getpid()
            process.communicate(b"\n", timeout=3)
            with pytest.raises(ProcessLookupError):
                sampling._read_stat(pinned)
        finally:
            os.close(fd)


@pytest.mark.parametrize("mode", ["nonzero", "timeout", "cancel"])
def test_measurement_preserves_exit_timeout_and_cancellation(tmp_path, mode):
    probe = ProcessProbe("execute")
    marker = tmp_path / "launched"
    options = {"timeout": 3}
    if mode == "nonzero":
        code = "raise SystemExit(7)"
    elif mode == "timeout":
        code = "import time\ntime.sleep(30)"
        options["timeout"] = 0.2
    else:
        code = "import time\nfrom pathlib import Path\nPath('launched').touch()\ntime.sleep(30)"
        options["cancel_requested"] = marker.is_file
    result = _python(code, tmp_path, probe, **options)
    row = probe.outcome
    assert result.exit_code == row["exit_code"] and row["wall_ns"] > 0
    assert row["cancelled"] == result.cancelled == (mode == "cancel")
    assert row["timed_out"] == result.timed_out == (mode == "timeout")
    if mode == "nonzero":
        assert result.exit_code == 7
    else:
        assert result.exit_code < 0
    assert managed_workers_stopped(tmp_path)


def test_container_resources_are_explicitly_unavailable(tmp_path):
    probe = ProcessProbe("discovery", external_container=True)
    outcome = _python("pass", tmp_path, probe, timeout=2)
    row = probe.outcome
    assert outcome.exit_code == row["exit_code"] == 0
    assert row["phase"] == "discovery" and row["resource_scope"] == "external_container"
    assert row["sample_count"] == 0
    assert set((reading["status"], reading["value"], reading["reason"]) for reading in row["resources"].values()) == {
        ("unavailable", None, "external_container")
    }


def test_proc_access_denial_keeps_wall_exit_and_unavailable_values(tmp_path, monkeypatch):
    def denied(_):
        raise PermissionError("proc disabled")

    monkeypatch.setattr(sampling, "_open_process_directory", denied)
    probe = ProcessProbe("execute")
    result = _python("print('done')", tmp_path, probe, timeout=2)
    row = probe.outcome
    assert result.stdout.strip() == "done"
    assert row["wall_ns"] > 0 and row["exit_code"] == 0 and row["sample_count"] == 0
    assert all(reading["status"] == "unavailable" and reading["value"] is None for reading in row["resources"].values())


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs")
def test_unresponsive_sampler_does_not_block_runner_or_hold_worker_lease(tmp_path, monkeypatch):
    sampling_started, release, finished = Event(), Event(), Event()

    def stalled(*_):
        sampling_started.set()
        try:
            assert release.wait(6)
            return sampling._Tree((None,) * 4, 0, False, False)
        finally:
            finished.set()

    monkeypatch.setattr(sampling, "_sample_tree", stalled)
    probe = ProcessProbe("execute")
    try:
        start = time.monotonic()
        result = _python("import time; time.sleep(0.2)", tmp_path, probe, timeout=2)
        assert sampling_started.is_set() and not finished.is_set()
        assert time.monotonic() - start < 3
        assert result.exit_code == 0 and probe.outcome["sample_count"] == 0
        assert managed_workers_stopped(tmp_path)
    finally:
        release.set()
        if sampling_started.is_set():
            assert finished.wait(2)
