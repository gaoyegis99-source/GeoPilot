import json
import os
import shutil
from pathlib import Path

import pyogrio
import pytest
import yaml

from geopilot.domain.step_io import StepRequest
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/fixtures/vector"


def _request(step, tool, inputs, parameters, output):
    return StepRequest(
        run_id="road-selection", step_key=step, tool=tool.ref, workdir_ref="road-selection",
        inputs=inputs, parameters=parameters,
        output_targets={output: {"path": f"intermediate/{output}.gpkg", "layer": output}},
        log_path=f"steps/{step}/process.log", cancel_path="cancel.flag",
    )


def test_real_extent_expansion_and_spatial_selection_match_reference_ids(tmp_path):
    executable = os.getenv("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if not executable:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS to run actual road selection")
    contract_ids = (
        "qgis-expand-analysis-extent", "qgis-select-roads-by-location", "qgis-buffer",
        "qgis-select-points-by-location",
    )
    registry = ToolRegistry(qgis_process=executable)
    contracts = {}
    for tool_id in contract_ids:
        path = ROOT / f"config/tools/{tool_id}.yaml"
        contract = ToolContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        registry.register(contract)
        contracts[tool_id] = contract
    bindings = yaml.safe_load((ROOT / "config/runner_bindings/qgis-buffer.yaml").read_text(encoding="utf-8"))
    bindings.update(
        yaml.safe_load((ROOT / "config/runner_bindings/qgis-road-workflow.yaml").read_text(encoding="utf-8"))
    )
    for directory in (
        "inputs", "intermediate", "steps/expand_analysis_extent", "steps/select_roads",
        "steps/create_buffer", "steps/apply_spatial_relation",
    ):
        (tmp_path / directory).mkdir(parents=True)
    source = tmp_path / "inputs/sample.gpkg"
    shutil.copyfile(GOLDEN / "road-buffer.gpkg", source)
    before = source.read_bytes()
    expected = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))
    configuration = {"registry": registry, "workdir": tmp_path, "qgis_bindings": bindings, "qgis_process": executable}

    expand = _request(
        "expand_analysis_extent", contracts["qgis-expand-analysis-extent"],
        {"source": {"path": "inputs/sample.gpkg", "layer": "analysis_extent"}},
        {"distance": expected["distance_m"], "segments": 16, "dissolve": True,
         "end_cap_style": "round", "join_style": "round", "miter_limit": 2},
        "expanded_extent",
    )
    extent_result = run_step(expand, **configuration)
    assert extent_result.status == "SUCCEEDED", extent_result
    extent = extent_result.outputs["expanded_extent"]
    actual_extent = pyogrio.read_dataframe(tmp_path / extent.path, layer=extent.layer)
    assert actual_extent.crs.to_epsg() == 32650
    assert actual_extent.geometry.item().bounds == pytest.approx((499500, 4399500, 502500, 4402500))

    select = _request(
        "select_roads", contracts["qgis-select-roads-by-location"],
        {"source": {"path": "inputs/sample.gpkg", "layer": "roads"},
         "area": {"path": extent.path, "layer": extent.layer}},
        {"predicate": "intersects"},
        "roads",
    )
    selected_result = run_step(select, **configuration)
    assert selected_result.status == "SUCCEEDED", selected_result
    selected = selected_result.outputs["roads"]
    roads = pyogrio.read_dataframe(tmp_path / selected.path, layer=selected.layer)
    assert roads.crs.to_epsg() == 32650
    assert sorted(roads["source_feature_id"]) == expected["candidate_road_ids"]

    buffer = _request(
        "create_buffer", contracts["qgis-buffer"],
        {"source": {"path": selected.path, "layer": selected.layer}}, expand.parameters, "buffer",
    )
    buffer_result = run_step(buffer, **configuration)
    assert buffer_result.status == "SUCCEEDED", buffer_result
    buffer_output = buffer_result.outputs["buffer"]
    relate = _request(
        "apply_spatial_relation", contracts["qgis-select-points-by-location"],
        {"source": {"path": "inputs/sample.gpkg", "layer": "targets"},
         "area": {"path": buffer_output.path, "layer": buffer_output.layer}},
        {"predicate": "intersects"}, "targets",
    )
    related = run_step(relate, **configuration)
    assert related.status == "SUCCEEDED", related
    points = pyogrio.read_dataframe(tmp_path / related.outputs["targets"].path, layer="targets")
    assert sorted(points["source_feature_id"]) == expected["relations"]["intersects"]["before_clip_target_ids"]
    assert source.read_bytes() == before
    assert "--json run native:buffer -" in (tmp_path / expand.log_path).read_text(encoding="utf-8")
    assert "--json run native:extractbylocation -" in (tmp_path / select.log_path).read_text(encoding="utf-8")
