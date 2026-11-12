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
from geopilot.methods.loader import load_method_catalog
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/fixtures/vector/road-buffer.gpkg"


def test_registered_attribute_filter_uses_real_qgis_and_preserves_selected_geometry(tmp_path):
    executable = os.getenv("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if not executable:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS to run the real attribute filter")
    method = load_method_catalog(ROOT / "config/methods").get("attribute-filter-export", 1)
    tool = ToolContract.model_validate(
        yaml.safe_load((ROOT / "config/tools/qgis-extract-by-attribute.yaml").read_text(encoding="utf-8"))
    )
    bindings = yaml.safe_load(
        (ROOT / "config/runner_bindings/qgis-extract-by-attribute.yaml").read_text(encoding="utf-8")
    )
    assert method.steps[0].tool == tool.ref
    assert method.steps[0].outputs == tool.outputs
    registry = ToolRegistry(qgis_process=executable)
    registry.register(tool)

    for directory in ("inputs", "outputs", "steps/filter_features"):
        (tmp_path / directory).mkdir(parents=True)
    source = tmp_path / "inputs/features.gpkg"
    shutil.copyfile(GOLDEN, source)
    before = source.read_bytes()
    request = StepRequest.model_validate({
        "run_id": "attribute-tool",
        "step_key": "filter_features",
        "tool": tool.ref,
        "workdir_ref": "attribute-tool",
        "inputs": {"source": {"path": "inputs/features.gpkg", "layer": "targets"}},
        "parameters": {"field": "source_feature_id", "operator": "equals", "value": "building-west"},
        "output_targets": {"filtered": {"path": "outputs/filtered.gpkg", "layer": "filtered"}},
        "log_path": "steps/filter_features/process.log",
        "cancel_path": "cancel.flag",
    })

    result = run_step(request, registry=registry, workdir=tmp_path, qgis_bindings=bindings, qgis_process=executable)

    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    assert result.environment["qgis_version"]
    actual = pyogrio.read_dataframe(tmp_path / result.outputs["filtered"].path, layer="filtered")
    expected = pyogrio.read_dataframe(source, layer="targets", where="source_feature_id = 'building-west'")
    assert actual.source_feature_id.tolist() == ["building-west"]
    assert actual.crs == expected.crs
    assert actual.geometry.item().equals_exact(expected.geometry.item(), 0)
    log = (tmp_path / request.log_path).read_text(encoding="utf-8")
    assert "--json run native:extractbyattribute -" in log
    assert "measurement: " not in log
    engine_report = json.loads(log.rsplit("\nstdout:\n", 1)[-1].split("\nstderr:\n", 1)[0])
    assert engine_report["inputs"]["OPERATOR"] == 0
    assert engine_report["inputs"]["VALUE"] == "building-west"
    assert source.read_bytes() == before
