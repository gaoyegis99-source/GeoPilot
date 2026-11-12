from pathlib import Path

import pyogrio
import pytest

from geopilot.application.plan_runner import execute_claimed_run
from geopilot.domain.step_io import StepResult
from geopilot.persistence.run_repository import get_run, list_run_steps
from geopilot.persistence.schema import open_database
from geopilot.scheduler.failure_handler import list_run_diagnostics
from geopilot.scheduler.loop import run_once


WORKER = Path(__file__).resolve().parents[2] / "src/geopilot/gis/tools/prepare_measurement_lines.py"


@pytest.mark.parametrize(("fault", "error_code", "partial_exists"), [
    ("start", "PROCESS_START_FAILED", False),
    ("nonzero", "PROCESS_FAILED", True),
    ("timeout", "PROCESS_TIMEOUT", True),
    ("invalid", "OUTPUT_INVALID", True),
    ("missing", "OUTPUT_MISSING", False),
])
def test_failed_middle_step_retains_real_data_for_diagnosis(queued_run, tmp_path, fault, error_code, partial_exists):
    database, workspace, registry, implementation, run_id, workdir = queued_run
    middle = tmp_path / "middle.py"
    if fault != "start":
        middle.write_text(
            "import json, runpy, sys, time\nfrom pathlib import Path\n"
            f"try:\n    runpy.run_path({str(WORKER)!r}, run_name='__main__')\n"
            "except SystemExit as error:\n    assert error.code == 0\n"
            "manifest = Path(sys.argv[sys.argv.index('--result') + 1])\n"
            "output = Path(json.loads(manifest.read_text())['outputs']['roads']['path'])\n"
            "print('Intermediate measurement completed', flush=True)\n"
            + {
                "nonzero": "raise SystemExit(7)\n",
                "timeout": "time.sleep(60)\n",
                "invalid": "manifest.write_text('{bad JSON')\n",
                "missing": "output.unlink()\n",
            }[fault],
            encoding="utf-8",
        )
    assert run_once(database, lambda run: execute_claimed_run(
        database, run, workspace_root=workspace, registry=registry,
        python_implementations={implementation: WORKER, "test.measurement_middle": middle}, timeout=3,
    )) == run_id
    with open_database(database, read_only=True) as connection:
        run = get_run(connection, run_id)
        first, failed, pending = list_run_steps(connection, run_id)
    assert (run.execution_status, run.check_status, run.current_step) == ("FAILED", "NOT_CHECKED", "consume")
    assert run.error == failed.result.error
    assert run.error.step_key == "consume" and run.error.code == error_code
    assert first.status == "SUCCEEDED" and pending.status == "NOT_STARTED" and pending.request is None
    assert not (workdir / "steps/finalize").exists() and not (workdir / "outputs/manifest.json").exists()
    assert not list((workdir / "checks").iterdir())
    assert failed.result.outputs == {}
    assert failed.result == StepResult.model_validate_json(
        (workdir / "steps/consume/result.json").read_text(encoding="utf-8")
    )

    files = list_run_diagnostics(database, run_id)
    vectors = [item for item in files if item.type == "vector_line"]
    assert [item.step_key for item in vectors] == (["prepare", "consume"] if partial_exists else ["prepare"])
    assert all(item.role == "roads" and item.readable for item in vectors)
    for item in vectors:
        features = pyogrio.read_dataframe(workdir / item.path, layer=item.layer)
        assert sorted(features["source_feature_id"]) == ["road-edge", "road-far", "road-main"]
    logs = {item.step_key: item for item in files if item.type == "log"}
    assert set(logs) == {"prepare", "consume"}
    assert error_code in (workdir / logs["consume"].path).read_text(encoding="utf-8")


def test_corrupt_file_remains_visible_as_unreadable_diagnostics(queued_run, tmp_path):
    database, workspace, registry, implementation, run_id, workdir = queued_run
    middle = tmp_path / "corrupt.py"
    middle.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "request = Path(sys.argv[sys.argv.index('--request') + 1])\n"
        "target = json.loads(request.read_text())['output_targets']['roads']\n"
        "Path(target['path']).write_bytes(b'corrupt geopackage')\n"
        "Path(sys.argv[sys.argv.index('--result') + 1]).write_text(json.dumps({'outputs': {'roads': target}}))\n",
        encoding="utf-8",
    )
    assert run_once(database, lambda run: execute_claimed_run(
        database, run, workspace_root=workspace, registry=registry,
        python_implementations={implementation: WORKER, "test.measurement_middle": middle},
    )) == run_id
    item, = [
        item for item in list_run_diagnostics(database, run_id)
        if item.step_key == "consume" and item.type == "vector_line"
    ]
    assert not item.readable and (workdir / item.path).read_bytes() == b"corrupt geopackage"
