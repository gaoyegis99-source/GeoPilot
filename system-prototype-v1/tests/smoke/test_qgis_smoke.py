import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pyogrio
import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke" / "qgis_smoke.py"


def test_qgis_processing_buffers_a_real_geopackage(tmp_path):
    executable = os.getenv("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if not executable:
        pytest.skip("需要真实 qgis_process；请配置 GEOPILOT_QGIS_PROCESS")

    run_dir = tmp_path / "qgis run"
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--qgis-process", executable, "--output-dir", str(run_dir)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PASS"
    assert set(summary["commands"]) == {"version", "list", "help", "buffer"}
    for entry in summary["commands"].values():
        assert entry["exit_code"] == 0
        assert (run_dir / entry["log"]).stat().st_size > 0
    assert "native:buffer" in (run_dir / "list.log").read_text(encoding="utf-8")
    assert "DISTANCE" in (run_dir / "help.log").read_text(encoding="utf-8")

    roads = pyogrio.read_dataframe(run_dir / "roads.gpkg", layer="roads")
    assert roads.crs.to_epsg() == 32650
    assert roads["road_id"].tolist() == ["ROAD-1"]
    assert roads.geometry.iloc[0].length == 1000

    buffered = pyogrio.read_dataframe(run_dir / "buffer.gpkg", layer=summary["output"]["layer"])
    assert buffered.crs.to_epsg() == 32650
    assert buffered["road_id"].tolist() == ["ROAD-1"]
    geometry = buffered.geometry.iloc[0]
    assert geometry.geom_type in {"Polygon", "MultiPolygon"}
    assert geometry.is_valid and not geometry.is_empty
    assert geometry.bounds == pytest.approx((499975, 4499975, 501025, 4500025), abs=1e-6, rel=0)
    assert geometry.area == pytest.approx(51950.90322016128, abs=1e-5, rel=0)


def test_missing_qgis_process_records_start_failure(tmp_path):
    run_dir = tmp_path / "missing-qgis"
    missing_executable = tmp_path / "qgis_process"
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--qgis-process", str(missing_executable), "--output-dir", str(run_dir)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 1
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "FAIL"
    assert summary["commands"]["version"]["exit_code"] is None
    assert "无法启动 QGIS" in summary["error"]
    assert str(missing_executable) in (run_dir / "version.log").read_text(encoding="utf-8")
    assert not (run_dir / "buffer.gpkg").exists()


def test_existing_output_directory_is_preserved(tmp_path):
    previous_output = tmp_path / "buffer.gpkg"
    previous_output.write_bytes(b"previous output")
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--output-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert process.returncode != 0
    assert "已存在" in process.stderr
    assert previous_output.read_bytes() == b"previous output"
