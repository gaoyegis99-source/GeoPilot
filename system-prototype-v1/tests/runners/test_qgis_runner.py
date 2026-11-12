import json
import os
import shutil
import sys
from pathlib import Path

import pyogrio
import pytest
import yaml

from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.qgis_runner import run_qgis
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "vector"
ALGORITHM = "native:buffer"
STEP = "steps/create_buffer"
SOURCE = "inputs/road's source.gpkg"
SOURCE_LAYER = "selected roads"
OUTPUT = "outputs/road's buffer.gpkg"
OUTPUT_LAYER = "buffer reference"


def load_buffer_configuration():
    contract_file = ROOT / "config" / "tools" / "qgis-buffer.yaml"
    binding_file = ROOT / "config" / "runner_bindings" / "qgis-buffer.yaml"
    contract = ToolContract.model_validate(yaml.safe_load(contract_file.read_text(encoding="utf-8")))
    return contract, yaml.safe_load(binding_file.read_text(encoding="utf-8"))


def prepare_buffer_request(workdir):
    for directory in ("inputs", "outputs", STEP):
        (workdir / directory).mkdir(parents=True)
    fixture = GOLDEN / "road-buffer.gpkg"
    source = workdir / SOURCE
    pyogrio.write_dataframe(pyogrio.read_dataframe(fixture, layer="targets"), source,
                            layer="other", driver="GPKG")
    roads = pyogrio.read_dataframe(fixture, layer="roads", where="source_feature_id = 'road-main'")
    pyogrio.write_dataframe(roads, source, layer=SOURCE_LAYER, driver="GPKG")
    return {
        "run_id": "run-001",
        "step_key": "create_buffer",
        "tool": {"id": "qgis-buffer", "version": 1},
        "workdir_ref": "run-001",
        "inputs": {"source": {"path": SOURCE, "layer": SOURCE_LAYER}},
        "parameters": {
            "distance": {"value": 0.5, "unit": "kilometer"},
            "segments": 16,
            "dissolve": True,
            "end_cap_style": "round",
            "join_style": "round",
            "miter_limit": 2,
        },
        "output_targets": {"buffer": {"path": OUTPUT, "layer": OUTPUT_LAYER}},
        "log_path": f"{STEP}/process.log",
        "cancel_path": "cancel.flag",
    }


def test_real_qgis_runs_named_geopackage_layers_and_persists_evidence(tmp_path):
    executable = os.getenv("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if not executable:
        pytest.skip("需要真实 qgis_process；请配置 GEOPILOT_QGIS_PROCESS")
    workdir = tmp_path / "working dir"
    request = prepare_buffer_request(workdir)
    original_input = (workdir / SOURCE).read_bytes()
    contract, bindings = load_buffer_configuration()
    registry = ToolRegistry(qgis_process=executable)
    registry.register(contract)
    result = run_qgis(request, registry=registry, workdir=workdir, bindings=bindings,
                      qgis_process=executable, timeout=30)

    assert result["status"] == "SUCCEEDED" and result["exit_code"] == 0, result
    assert result["error"] is None
    assert result["environment"]["qgis_version"] == registry.qgis_version
    assert result["environment"]["gdal_version"]
    assert result["outputs"] == {"buffer": {"path": OUTPUT, "layer": OUTPUT_LAYER, "type": "vector_polygon"}}
    assert json.loads((workdir / STEP / "result.json").read_text(encoding="utf-8")) == result
    saved = json.loads((workdir / STEP / "request.json").read_text(encoding="utf-8"))
    assert saved["tool"] == request["tool"] and saved["inputs"] == request["inputs"]
    assert saved["parameters"]["distance"] == 500
    log = (workdir / result["log_path"]).read_text(encoding="utf-8")
    assert "native:buffer" in log and f"|layername={SOURCE_LAYER}" in log
    assert f"|layername={OUTPUT_LAYER}" in log and "DISTANCE" in log
    assert "END_CAP_STYLE" in log and "JOIN_STYLE" in log

    assert pyogrio.list_layers(workdir / SOURCE)[:, 0].tolist() == ["other", SOURCE_LAYER]
    assert pyogrio.list_layers(workdir / OUTPUT)[:, 0].tolist() == [OUTPUT_LAYER]
    buffered = pyogrio.read_dataframe(workdir / OUTPUT, layer=OUTPUT_LAYER)
    assert buffered.crs.to_epsg() == 32650 and buffered["source_feature_id"].tolist() == ["road-main"]
    geometry = buffered.geometry.iloc[0]
    assert geometry.is_valid and not geometry.is_empty
    expected = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))["main_road_buffer"]
    tolerances = expected["absolute_tolerances"]
    assert geometry.area == pytest.approx(expected["discrete_area_m2"], abs=tolerances["area_m2"], rel=0)
    assert geometry.bounds == pytest.approx(expected["bounds_xy_m"], abs=tolerances["bounds_m"], rel=0)
    assert (workdir / SOURCE).read_bytes() == original_input


@pytest.mark.parametrize(("defect", "field", "message"), [
    ("unknown_alias", "parameters.distance", "DISTANCE_TYPO"),
    ("unavailable_enum_index", "enum_values.end_cap_style", "index 9"),
    ("wrong_destination_role", "outputs.buffer", "SEPARATE_DISJOINT"),
])
def test_actual_qgis_schema_rejects_bad_binding_without_calculation(tmp_path, defect, field, message):
    executable = os.getenv("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if not executable:
        pytest.skip("需要真实 qgis_process；请配置 GEOPILOT_QGIS_PROCESS")
    request = prepare_buffer_request(tmp_path)
    contract, bindings = load_buffer_configuration()
    binding = bindings[ALGORITHM]
    if defect == "unknown_alias":
        binding["parameters"]["distance"] = "DISTANCE_TYPO"
    elif defect == "unavailable_enum_index":
        binding["enum_values"]["end_cap_style"]["round"] = 9
    else:
        binding["outputs"]["buffer"] = "SEPARATE_DISJOINT"
    registry = ToolRegistry(qgis_process=executable)
    registry.register(contract)
    result = run_qgis(request, registry=registry, workdir=tmp_path, bindings=bindings,
                      qgis_process=executable, timeout=30)

    assert result["status"] == "FAILED" and result["exit_code"] is None, result
    assert result["error"]["code"] == "INVALID_PARAMETER" and result["error"]["phase"] == "preflight"
    assert result["error"]["field"].endswith(field)
    assert message.casefold() in result["error"]["message"].casefold()
    log = (tmp_path / STEP / "process.log").read_text(encoding="utf-8")
    assert "--json help native:buffer" in log and "--json run native:buffer" not in log
    assert not (tmp_path / OUTPUT).exists()


def _discovery_response():
    parameters = {
        name: {"is_destination": name == "OUTPUT", "is_advanced": False, "optional": False}
        for name in ("INPUT", "DISTANCE", "SEGMENTS", "DISSOLVE", "END_CAP_STYLE", "JOIN_STYLE", "MITER_LIMIT", "OUTPUT")
    }
    parameters["END_CAP_STYLE"]["available_options"] = {"0": "Round", "1": "Flat", "2": "Square"}
    parameters["JOIN_STYLE"]["available_options"] = {"0": "Round", "1": "Miter", "2": "Bevel"}
    return json.dumps({"algorithm_details": {"id": ALGORITHM}, "parameters": parameters, "outputs": {"OUTPUT": {}}})


def _write_test_cli(tmp_path, run_body):
    """A local test executable for the error paths only."""
    script = tmp_path / "test-qgis-process"
    script.write_text(
        f"#!{sys.executable}\nimport json\nimport sys\n"
        "if sys.argv[1:] == ['--json', 'list']:\n"
        "    print(json.dumps({'qgis_version': 'test-only', 'providers': {\n"
        "        'native': {'algorithms': {'native:buffer': {}}}}}))\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1:] == ['--json', 'help', 'native:buffer']:\n"
        f"    print({_discovery_response()!r})\n"
        "    raise SystemExit(0)\n"
        "assert sys.argv[1:] == ['--json', 'run', 'native:buffer', '-']\n"
        "request = json.load(sys.stdin)\n"
        f"{run_body}\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script


def run_failure(tmp_path, run_body, code, *, missing_executable=False, timeout=30, change_bindings=None,
                change_request=None):
    executable = _write_test_cli(tmp_path, run_body)
    contract, bindings = load_buffer_configuration()
    registry = ToolRegistry(qgis_process=executable)
    registry.register(contract)
    assert registry.get(contract.id).available
    workdir = tmp_path / "run-001"
    request = prepare_buffer_request(workdir)
    if change_bindings is not None:
        change_bindings(bindings[ALGORITHM])
    if change_request is not None:
        change_request(request)
    if missing_executable:
        executable = tmp_path / "missing-executable"

    result = run_qgis(request, registry=registry, workdir=workdir, bindings=bindings,
                      qgis_process=executable, timeout=timeout)
    assert result["status"] == "FAILED" and result["outputs"] == {}, result
    assert result["error"]["code"] == code and result["error"]["step_key"] == "create_buffer", result
    assert result["error"]["log_path"] == f"{STEP}/process.log"
    assert json.loads((workdir / STEP / "result.json").read_text(encoding="utf-8")) == result
    return result, workdir


def test_start_failure_preserves_actual_process_diagnostic(tmp_path):
    result, workdir = run_failure(tmp_path, "raise SystemExit(0)", "PROCESS_START_FAILED",
                                  missing_executable=True)
    assert result["exit_code"] is None
    assert "missing-executable" in (workdir / STEP / "process.log").read_text(encoding="utf-8")


def test_nonzero_exit_preserves_code_and_logs(tmp_path):
    body = "print('synthetic stdout')\nprint('synthetic stderr', file=sys.stderr)\nraise SystemExit(7)"
    result, workdir = run_failure(tmp_path, body, "PROCESS_FAILED")
    assert result["exit_code"] == 7
    log = (workdir / STEP / "process.log").read_text(encoding="utf-8")
    assert "synthetic stdout" in log and "synthetic stderr" in log


def test_timeout_is_reported_separately(tmp_path):
    result, workdir = run_failure(tmp_path, "import time\ntime.sleep(5)", "PROCESS_TIMEOUT", timeout=0.25)
    assert isinstance(result["exit_code"], int) and result["exit_code"] != 0
    assert not (workdir / OUTPUT).exists()


@pytest.mark.parametrize(("stdout", "code"), [
    (json.dumps({"qgis_version": "test-only", "results": {"OUTPUT": f"./{OUTPUT}|layername={OUTPUT_LAYER}"}}),
     "OUTPUT_MISSING"),
    ('{"results": {}}', "OUTPUT_MISSING"),
    ('{"results": {"OUTPUT": "./outputs/wrong.gpkg"}}', "OUTPUT_INVALID"),
    (json.dumps({"results": {"OUTPUT": f"./{OUTPUT}|layername=wrong"}}), "OUTPUT_INVALID"),
    ('{"results": {"UNDECLARED": "other.gpkg"}}', "OUTPUT_INVALID"),
    ('not valid JSON', "OUTPUT_INVALID"),
], ids=["missing-file", "missing-output", "wrong-path", "wrong-layer", "extra-output", "malformed"])
def test_zero_exit_requires_matching_manifest_and_real_output(tmp_path, stdout, code):
    result, _ = run_failure(tmp_path, f"print({stdout!r})", code)
    assert result["exit_code"] == 0


@pytest.mark.parametrize(("invalid", "field"), [
    ("missing_input", "inputs"), ("duplicate_alias", "parameters"), ("missing_enum", "enum_values.end_cap_style"),
])
def test_invalid_runner_bindings_are_rejected_before_execution(tmp_path, invalid, field):
    def change(binding):
        if invalid == "missing_input":
            binding["inputs"].clear()
        elif invalid == "duplicate_alias":
            binding["parameters"]["segments"] = binding["inputs"]["source"]
        else:
            binding["enum_values"]["end_cap_style"].pop("square")

    result, _ = run_failure(tmp_path, "raise SystemExit(77)", "INVALID_PARAMETER", change_bindings=change)
    assert result["exit_code"] is None
    assert result["error"]["phase"] == "preflight"
    assert result["error"]["field"].endswith(field)


def test_reserved_uri_delimiter_in_output_layer_is_rejected_before_execution(tmp_path):
    def change(request):
        request["output_targets"]["buffer"]["layer"] = "not|a layer"

    result, _ = run_failure(tmp_path, "raise SystemExit(77)", "INVALID_PARAMETER", change_request=change)
    assert result["exit_code"] is None and result["error"]["field"] == "output_targets.buffer.layer"
