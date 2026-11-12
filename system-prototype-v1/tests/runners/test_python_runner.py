import csv
import json
import platform
from pathlib import Path

import geopandas
import pyogrio
import pytest
import shapely
import yaml
from shapely.geometry import box

from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.python_runner import run_python
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "src" / "geopilot" / "gis" / "tools" / "planar_area.py"
CONTRACT = ROOT / "config" / "tools" / "python-area.yaml"
IMPLEMENTATION = "geopilot.planar_area"
OUTPUT = "outputs/area.csv"
STEP = "steps/calculate_area"


@pytest.fixture
def area_step(tmp_path):
    workdir = tmp_path / "run-001"
    for directory in ("inputs", "outputs", STEP):
        (workdir / directory).mkdir(parents=True)
    source = workdir / "inputs" / "polygons.gpkg"
    features = geopandas.GeoDataFrame(
        {"source_feature_id": ["square", "rectangle"]},
        geometry=[
            box(1_000_000, 2_000_000, 1_001_000, 2_001_000),
            box(1_002_000, 2_000_000, 1_002_500, 2_000_200),
        ],
        crs="EPSG:6933",
    )
    pyogrio.write_dataframe(features, source, driver="GPKG", layer="parcels")

    contract = ToolContract.model_validate(yaml.safe_load(CONTRACT.read_text(encoding="utf-8")))
    registry = ToolRegistry(python_implementations=[IMPLEMENTATION])
    registry.register(contract, required_modules=("geopandas", "pyogrio", "shapely", "pyproj"))
    request = {
        "run_id": "run-001",
        "step_key": "calculate_area",
        "tool": {"id": "python-area", "version": 1},
        "workdir_ref": "run-001",
        "inputs": {"source": {"path": "inputs/polygons.gpkg", "layer": "parcels"}},
        "parameters": {"area_crs": "EPSG:6933", "area_unit": "square_meter"},
        "output_targets": {"statistics": {"path": OUTPUT}},
        "log_path": f"{STEP}/process.log",
        "cancel_path": "cancel.flag",
    }
    return workdir, registry, request


def _run(area_step, script=SCRIPT, *, timeout=30):
    workdir, registry, request = area_step
    return run_python(request, registry=registry, workdir=workdir,
                      implementations={IMPLEMENTATION: script}, timeout=timeout)


def _read_result(area_step):
    return json.loads((area_step[0] / STEP / "result.json").read_text(encoding="utf-8"))


def _assert_failure(area_step, result, code):
    assert result["status"] == "FAILED"
    assert result["outputs"] == {}
    assert result["error"]["code"] == code
    assert result["error"]["step_key"] == "calculate_area"
    assert result["error"]["message"]
    assert result["error"]["log_path"] == f"{STEP}/process.log"
    assert result == _read_result(area_step)


def test_real_area_process_writes_checked_result_and_reproducible_evidence(area_step):
    workdir, _, request = area_step
    source = workdir / request["inputs"]["source"]["path"]
    original = source.read_bytes()
    result = _run(area_step)

    assert result["status"] == "SUCCEEDED" and result["exit_code"] == 0, result
    assert result["run_id"] == "run-001" and result["step_key"] == "calculate_area"
    assert result["error"] is None
    assert result["outputs"]["statistics"] == {"path": OUTPUT, "layer": None, "type": "table_csv"}
    assert result["started_at"] and result["finished_at"]
    assert result["environment"]["python"] == platform.python_version()
    assert result["environment"]["pyogrio"] == pyogrio.__version__
    assert result["environment"]["shapely"] == shapely.__version__
    assert result == _read_result(area_step)

    request_on_disk = json.loads((workdir / STEP / "request.json").read_text(encoding="utf-8"))
    assert request_on_disk["tool"] == request["tool"]
    assert request_on_disk["inputs"]["source"] == request["inputs"]["source"]
    assert request_on_disk["parameters"] == request["parameters"]
    child_result = json.loads((workdir / STEP / "python-result.json").read_text(encoding="utf-8"))
    assert child_result["outputs"] == {"statistics": {"path": OUTPUT}}
    log = (workdir / result["log_path"]).read_text(encoding="utf-8")
    assert "planar_area.py" in log and f"{STEP}/request.json" in log and f"{STEP}/python-result.json" in log
    assert "Calculated 2 polygon area(s)" in log

    with (workdir / OUTPUT).open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert {row["source_feature_id"]: float(row["area"]) for row in rows} == {"square": 1_000_000, "rectangle": 100_000}
    assert {row["area_unit"] for row in rows} == {"square_meter"}
    assert {row["area_crs"] for row in rows} == {"EPSG:6933"}
    assert source.read_bytes() == original


def test_area_script_checks_actual_input_crs_against_parameter(area_step):
    workdir, _, request = area_step
    request["parameters"]["area_crs"] = "EPSG:32650"
    result = _run(area_step)
    _assert_failure(area_step, result, "PROCESS_FAILED")
    assert result["exit_code"] != 0
    log = (workdir / result["log_path"]).read_text(encoding="utf-8")
    assert "area_crs" in log and "EPSG:6933" in log and "EPSG:32650" in log
    assert not (workdir / OUTPUT).exists()


@pytest.mark.parametrize("configuration", [{}, {IMPLEMENTATION: Path("planar_area.py")}], ids=["unconfigured", "relative"])
def test_script_can_only_come_from_explicit_absolute_configuration(area_step, configuration):
    workdir, registry, request = area_step
    result = run_python(request, registry=registry, workdir=workdir, implementations=configuration)
    _assert_failure(area_step, result, "PROCESS_START_FAILED")
    assert result["exit_code"] is None
    assert not (workdir / OUTPUT).exists()


def test_nonzero_child_exit_preserves_process_output(area_step, tmp_path):
    script = tmp_path / "fail.py"
    script.write_text("import sys\nprint('actual stdout')\nprint('actual stderr', file=sys.stderr)\nraise SystemExit(7)\n", encoding="utf-8")
    result = _run(area_step, script)
    _assert_failure(area_step, result, "PROCESS_FAILED")
    assert result["exit_code"] == 7
    log = (area_step[0] / result["log_path"]).read_text(encoding="utf-8")
    assert "actual stdout" in log and "actual stderr" in log


def test_long_child_process_is_reported_as_timeout(area_step, tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    result = _run(area_step, script, timeout=0.25)
    _assert_failure(area_step, result, "PROCESS_TIMEOUT")
    assert not (area_step[0] / OUTPUT).exists()


@pytest.mark.parametrize(
    ("manifest", "error_code"),
    [
        (None, "OUTPUT_MISSING"),
        ('{"outputs": {}}', "OUTPUT_MISSING"),
        ('{"outputs": {"statistics": {"path": "outputs/other.csv"}}}', "OUTPUT_INVALID"),
        ('{"outputs": {"statistics": {"path": "outputs/area.csv", "layer": "other"}}}', "OUTPUT_INVALID"),
        ('{"outputs": {"unexpected": "outputs/area.csv"}}', "OUTPUT_INVALID"),
        ("{malformed", "OUTPUT_INVALID"),
    ],
    ids=["absent-file", "absent-role", "wrong-path", "wrong-layer", "extra-role", "malformed"],
)
def test_zero_exit_requires_a_valid_child_manifest(area_step, tmp_path, manifest, error_code):
    script = tmp_path / "report.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        f"Path({OUTPUT!r}).write_text('key,value\\narea,1\\n', encoding='utf-8')\n"
        f"manifest = {manifest!r}\n"
        "if manifest is not None:\n"
        "    Path(sys.argv[sys.argv.index('--result') + 1]).write_text(manifest, encoding='utf-8')\n",
        encoding="utf-8",
    )
    result = _run(area_step, script)
    _assert_failure(area_step, result, error_code)
    assert result["exit_code"] == 0


@pytest.mark.parametrize(("csv_text", "error_code"), [(None, "OUTPUT_MISSING"), ("", "OUTPUT_INVALID")])
def test_valid_manifest_cannot_substitute_for_a_readable_csv(area_step, tmp_path, csv_text, error_code):
    script = tmp_path / "report.py"
    manifest = json.dumps({"outputs": {"statistics": OUTPUT}})
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        f"csv_text = {csv_text!r}\n"
        "if csv_text is not None:\n"
        f"    Path({OUTPUT!r}).write_text(csv_text, encoding='utf-8')\n"
        f"Path(sys.argv[sys.argv.index('--result') + 1]).write_text({manifest!r}, encoding='utf-8')\n",
        encoding="utf-8",
    )
    result = _run(area_step, script)
    _assert_failure(area_step, result, error_code)
    assert result["exit_code"] == 0
    assert result["error"]["field"] == "outputs.statistics"


def test_existing_child_manifest_is_not_reused_or_overwritten(area_step, tmp_path):
    workdir, _, _ = area_step
    stale_manifest = workdir / STEP / "python-result.json"
    stale_manifest.write_text("previous manifest", encoding="utf-8")
    script = tmp_path / "would-run.py"
    script.write_text(f"from pathlib import Path\nPath({OUTPUT!r}).touch()\n", encoding="utf-8")
    result = _run(area_step, script)
    assert result["status"] == "FAILED"
    assert stale_manifest.read_text(encoding="utf-8") == "previous manifest"
    assert not (workdir / OUTPUT).exists()
