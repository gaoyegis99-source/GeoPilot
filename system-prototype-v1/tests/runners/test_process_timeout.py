import csv
import json
import os
import signal
import time
from pathlib import Path

import geopandas
import pyogrio
import pytest
import yaml
from shapely.geometry import box

from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.python_runner import run_python
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GIS_WORKER = ROOT / "src/geopilot/gis/tools/planar_area.py"


def _still_executing(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return False
    return stat.rsplit(")", 1)[-1].split()[0] not in {"Z", "X"}


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="Linux process group inspection")
@pytest.mark.parametrize("quiet_child", [False, True], ids=["stubborn-leader", "quiet-descendant"])
def test_timeout_stops_real_worker_group_and_keeps_evidence(tmp_path, quiet_child):
    pyogrio.write_dataframe(
        geopandas.GeoDataFrame({"source_feature_id": ["square"]}, geometry=[box(0, 0, 10, 10)], crs=6933),
        tmp_path / "input.gpkg", layer="areas",
    )
    tool = ToolContract.model_validate(yaml.safe_load((ROOT / "config/tools/python-area.yaml").read_text()))
    registry = ToolRegistry(python_implementations={tool.implementation})
    registry.register(tool)
    worker = tmp_path / "worker.py"
    quiet_program = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('descendant.pid').write_text(str(os.getpid())); time.sleep(60)"
    )
    worker.write_text(
        "import os, runpy, signal, subprocess, sys, time\nfrom pathlib import Path\n"
        "Path('leader.pid').write_text(str(os.getpid()))\n"
        f"if {quiet_child!r}:\n"
        f"    subprocess.Popen([sys.executable, '-c', {quiet_program!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "else:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"try:\n    runpy.run_path({str(GIS_WORKER)!r}, run_name='__main__')\n"
        "except SystemExit as error:\n    assert error.code == 0\n"
        "print('GIS computation flushed', flush=True)\n"
        "print('worker remains active', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    request = {
        "run_id": "run-timeout", "step_key": "area", "tool": {"id": tool.id, "version": tool.version},
        "workdir_ref": "run-timeout", "inputs": {"source": {"path": "input.gpkg", "layer": "areas"}},
        "parameters": {"area_crs": "EPSG:6933", "area_unit": "square_meter"},
        "output_targets": {"statistics": {"path": "area.csv"}}, "log_path": "process.log",
        "cancel_path": "cancel.flag",
    }
    try:
        result = run_python(
            request, registry=registry, workdir=tmp_path, implementations={tool.implementation: worker}, timeout=3,
        )
        assert result["status"] == "FAILED" and result["error"]["code"] == "PROCESS_TIMEOUT"
        assert result["exit_code"] == -int(signal.SIGTERM if quiet_child else signal.SIGKILL)
        assert result["outputs"] == {}
        assert result == json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
        assert not (tmp_path / "cancel.flag").exists()
        with (tmp_path / "area.csv").open(encoding="utf-8") as stream:
            area, = csv.DictReader(stream)
        assert area["source_feature_id"] == "square" and float(area["area"]) == 100
        log = (tmp_path / "process.log").read_text(encoding="utf-8")
        assert "stdout:\n" in log and "GIS computation flushed" in log
        assert "stderr:\nworker remains active" in log and f"exit_code: {result['exit_code']}" in log
        pids = [int((tmp_path / "leader.pid").read_text())]
        if quiet_child:
            pids.append(int((tmp_path / "descendant.pid").read_text()))
        deadline = time.monotonic() + 0.5
        while any(_still_executing(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(_still_executing(pid) for pid in pids)
    finally:
        for path in tmp_path.glob("*.pid"):
            pid = int(path.read_text())
            if _still_executing(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
