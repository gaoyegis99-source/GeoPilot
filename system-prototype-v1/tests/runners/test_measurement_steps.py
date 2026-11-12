import csv
import json
import math
import os
import shutil
from pathlib import Path

import geopandas
import pyogrio
import pytest
import yaml
from shapely.geometry import Polygon, box

from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "vector"
SCRIPT = ROOT / "src" / "geopilot" / "gis" / "tools" / "planar_area.py"
SOURCE = "inputs/source.gpkg"


def _yaml(relative):
    return yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def qgis_configuration():
    executable = os.getenv("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if executable is None:
        pytest.skip("配置 GEOPILOT_QGIS_PROCESS 以运行真实 QGIS 固定用例")
    contract = ToolContract.model_validate(_yaml("config/tools/qgis-buffer.yaml"))
    registry = ToolRegistry(qgis_process=executable)
    registry.register(contract)
    return {
        "registry": registry,
        "qgis_process": executable,
        "qgis_bindings": _yaml("config/runner_bindings/qgis-buffer.yaml"),
    }


@pytest.fixture(scope="module")
def python_configuration():
    contract = ToolContract.model_validate(_yaml("config/tools/python-area.yaml"))
    registry = ToolRegistry(python_implementations=[contract.implementation])
    registry.register(contract, required_modules=("geopandas", "pyogrio", "pyproj", "shapely"))
    return {"registry": registry, "python_implementations": {contract.implementation: SCRIPT}}


def _setup_step(tmp_path, case, frame, *, tool, step_key, parameters, output_targets):
    workdir = tmp_path / case
    step_dir = workdir / "steps" / step_key
    for directory in (workdir / "inputs", workdir / "outputs", step_dir):
        directory.mkdir(parents=True)
    pyogrio.write_dataframe(frame, workdir / SOURCE, layer="source", driver="GPKG")
    request = StepRequest.model_validate({
        "run_id": case,
        "step_key": step_key,
        "tool": {"id": tool, "version": 1},
        "workdir_ref": case,
        "inputs": {"source": {"path": SOURCE, "layer": "source"}},
        "parameters": parameters,
        "output_targets": output_targets,
        "log_path": f"steps/{step_key}/process.log",
        "cancel_path": "cancel.flag",
    })
    return workdir, request


def _road_input(epsg):
    road = pyogrio.read_dataframe(GOLDEN / "road-buffer.gpkg", layer="roads", where="source_feature_id = 'road-main'")
    return road.to_crs(epsg)


def _area_input(epsg):
    frame = geopandas.GeoDataFrame(
        {"source_feature_id": ["square", "rectangle"]},
        geometry=[
            box(1_000_000, 2_000_000, 1_001_000, 2_001_000),
            box(1_002_000, 2_000_000, 1_002_500, 2_000_200),
        ],
        crs="EPSG:6933",
    )
    return frame.to_crs(epsg)


def _buffer_step(tmp_path, case, epsg, *, distance=500):
    return _setup_step(
        tmp_path, case, _road_input(epsg), tool="qgis-buffer", step_key="create_buffer",
        parameters={
            "distance": distance, "segments": 16, "dissolve": True,
            "end_cap_style": "round", "join_style": "round", "miter_limit": 2,
        },
        output_targets={"buffer": {"path": "outputs/buffer.gpkg", "layer": "buffer"}},
    )


def _area_step(tmp_path, case, epsg):
    return _setup_step(
        tmp_path, case, _area_input(epsg), tool="python-area", step_key="calculate_area",
        parameters={"area_crs": f"EPSG:{epsg}", "area_unit": "square_meter"},
        output_targets={"statistics": {"path": "outputs/area.csv"}},
    )


def _execute_and_reopen_result(workdir, request, configuration):
    source = workdir / SOURCE
    original = fingerprint_file(source)
    result = run_step(request, workdir=workdir, timeout=60, **configuration)
    assert isinstance(result, StepResult)
    assert result.run_id == request.run_id and result.step_key == request.step_key
    assert result.log_path == request.log_path
    log_path = workdir / result.log_path
    assert StepResult.model_validate_json(log_path.with_name("result.json").read_text(encoding="utf-8")) == result
    saved = StepRequest.model_validate_json(log_path.with_name("request.json").read_text(encoding="utf-8"))
    assert saved.tool == request.tool and saved.inputs == request.inputs
    assert saved.output_targets == request.output_targets
    assert fingerprint_file(source) == original
    return result, saved, log_path.read_text(encoding="utf-8")


def _assert_preflight_rejected(workdir, result, log, epsg):
    assert result.status == "FAILED" and result.exit_code is None
    assert not result.outputs
    assert result.error.code == "MEASUREMENT_POLICY_REJECTED"
    assert result.error.phase == "preflight"
    assert result.error.step_key == result.step_key
    assert result.error.field == "inputs.source.crs"
    assert result.error.message.strip()
    assert f"EPSG:{epsg}" in str(result.error.observed)
    assert not any(line.startswith("$ ") for line in log.splitlines())
    assert not list((workdir / "outputs").iterdir())


def _assert_measurement_logged(log, *, method, crs, unit):
    entries = [json.loads(line.removeprefix("measurement: ")) for line in log.splitlines()
               if line.startswith("measurement: ")]
    assert len(entries) == 1
    assert {key: entries[0][key] for key in ("role", "method", "crs", "unit")} == {
        "role": "source", "method": method, "crs": crs, "unit": unit,
    }


@pytest.mark.parametrize(("case", "epsg"), [("T01", 4326), ("T02", 3857)], ids=["T01-geographic", "T02-web-mercator"])
def test_fixed_distance_rejects_geographic_and_web_mercator_before_dispatch(tmp_path, qgis_configuration, case, epsg):
    workdir, request = _buffer_step(tmp_path, case, epsg)
    assert pyogrio.read_info(workdir / SOURCE, layer="source")["crs"] == f"EPSG:{epsg}"
    result, saved, log = _execute_and_reopen_result(workdir, request, qgis_configuration)
    assert saved.parameters["distance"] == 500
    _assert_preflight_rejected(workdir, result, log, epsg)


def test_fixed_T03_local_projected_buffer_matches_independent_reference(tmp_path, qgis_configuration):
    workdir, request = _buffer_step(tmp_path, "T03", 32650, distance={"value": 0.5, "unit": "kilometer"})
    result, saved, log = _execute_and_reopen_result(workdir, request, qgis_configuration)
    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    assert result.error is None and result.environment["qgis_version"]
    assert saved.parameters["distance"] == 500 and saved.parameters["segments"] == 16
    _assert_measurement_logged(log, method="local_projected", crs="EPSG:32650", unit="meter")
    assert any(line.startswith("$ ") and "--json run native:buffer -" in line for line in log.splitlines())
    engine_report = json.loads(log.rsplit("\nstdout:\n", 1)[-1].split("\nstderr:\n", 1)[0])
    assert engine_report["inputs"]["DISTANCE"] == 500
    assert engine_report["inputs"]["SEGMENTS"] == 16
    assert engine_report["inputs"]["END_CAP_STYLE"] == 0

    output = result.model_dump(mode="json")["outputs"]["buffer"]
    assert output == {"path": "outputs/buffer.gpkg", "layer": "buffer", "type": "vector_polygon"}
    buffer = pyogrio.read_dataframe(workdir / output["path"], layer=output["layer"])
    assert buffer.crs.to_epsg() == 32650 and buffer["source_feature_id"].tolist() == ["road-main"]
    actual = buffer.geometry.item()
    assert actual.is_valid and not actual.is_empty

    expected = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))
    reference = expected["main_road_buffer"]
    start, end = reference["endpoints_xy_m"]
    radius = expected["distance_m"]
    segments = expected["buffer_options"]["segments_per_quarter"]
    vertices = []
    for (x, y), initial_angle in ((end, -math.pi / 2), (start, math.pi / 2)):
        for index in range(2 * segments + 1):
            angle = initial_angle + index * math.pi / (2 * segments)
            vertices.append((x + radius * math.cos(angle), y + radius * math.sin(angle)))
    capsule = Polygon(vertices)
    limits = reference["absolute_tolerances"]
    assert actual.area == pytest.approx(reference["discrete_area_m2"], abs=limits["area_m2"], rel=0)
    assert actual.bounds == pytest.approx(reference["bounds_xy_m"], abs=limits["bounds_m"], rel=0)
    assert actual.boundary.hausdorff_distance(capsule.boundary) <= limits["boundary_hausdorff_m"]


def test_fixed_T05_web_mercator_area_rejected_before_dispatch(tmp_path, python_configuration):
    workdir, request = _area_step(tmp_path, "T05", 3857)
    assert pyogrio.read_info(workdir / SOURCE, layer="source")["crs"] == "EPSG:3857"
    result, saved, log = _execute_and_reopen_result(workdir, request, python_configuration)
    assert saved.parameters == {"area_crs": "EPSG:3857", "area_unit": "square_meter"}
    _assert_preflight_rejected(workdir, result, log, 3857)


def test_fixed_T06_equal_area_polygons_match_manual_areas(tmp_path, python_configuration):
    workdir, request = _area_step(tmp_path, "T06", 6933)
    result, saved, log = _execute_and_reopen_result(workdir, request, python_configuration)
    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    assert result.error is None and result.environment["python"]
    assert saved.parameters == {"area_crs": "EPSG:6933", "area_unit": "square_meter"}
    _assert_measurement_logged(log, method="equal_area", crs="EPSG:6933", unit="square_meter")
    assert any(line.startswith("$ ") and "planar_area.py" in line for line in log.splitlines())
    assert "Calculated 2 polygon area(s)" in log

    output = result.model_dump(mode="json")["outputs"]["statistics"]
    assert output == {"path": "outputs/area.csv", "layer": None, "type": "table_csv"}
    with (workdir / output["path"]).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert {row["source_feature_id"]: float(row["area"]) for row in rows} == {"square": 1_000_000, "rectangle": 100_000}
    assert {(row["area_crs"], row["area_unit"]) for row in rows} == {("EPSG:6933", "square_meter")}
