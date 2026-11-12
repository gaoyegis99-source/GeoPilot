import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/install-check/run_matrix.py"


@pytest.fixture
def matrix():
    spec = importlib.util.spec_from_file_location("install_matrix", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_matrix(output, qgis, *args):
    return subprocess.run([
        sys.executable, str(SCRIPT), "--label", "test-row", "--output", str(output),
        "--qgis-process", str(qgis), *args,
    ], capture_output=True, text=True, check=False)


def test_missing_qgis_records_failed_row_without_install(tmp_path):
    output = tmp_path / "evidence"
    result = run_matrix(output, tmp_path / "missing-qgis")

    assert result.returncode == 1
    report = json.loads((output / "report.json").read_bytes())
    assert report["status"] == "FAIL" and report["exit_code"] == 1
    assert report["label"] == "test-row" and len(report["commit"]) == 40
    assert report["steps"] == []
    assert "QGIS Processing" in report["error"]


def test_existing_output_is_preserved(tmp_path):
    report = tmp_path / "report.json"
    report.write_text("previous evidence", encoding="utf-8")

    result = run_matrix(tmp_path, sys.executable)

    assert result.returncode == 2
    assert report.read_text(encoding="utf-8") == "previous evidence"
    assert list(tmp_path.iterdir()) == [report]


def test_git_snapshot_contains_clean_source_and_excludes_local_state(tmp_path, matrix):
    root = matrix.extract_source(matrix.git("rev-parse", "HEAD"), tmp_path)

    assert (root / "src/geopilot/api/server.py").is_file()
    assert (root / "frontend/package-lock.json").is_file()
    assert not (root / "frontend/node_modules").exists()
    assert not (root / "local-data").exists()
    assert not (root / ".venv").exists()


def test_start_failure_records_exit_and_log(tmp_path, matrix):
    report = {"steps": []}
    with matrix.server(report, tmp_path, "backend", [
        sys.executable, "-c", "print('startup failed'); raise SystemExit(7)",
    ], tmp_path, None) as process:
        with pytest.raises(RuntimeError, match="exited with code 7"):
            matrix.wait_http(process, "http://127.0.0.1:0/", timeout=2)

    assert report["steps"][0]["exit_code"] == 7
    assert (tmp_path / "backend.log").read_text(encoding="utf-8") == "startup failed\n"
