import json
from pathlib import Path

import pyogrio
import yaml

from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepRequest
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/fixtures/vector/road-buffer.gpkg"


def test_prepare_lines_reprojects_actual_input_through_python_worker(tmp_path):
    contract = ToolContract.model_validate(yaml.safe_load(
        (ROOT / "config/tools/python-prepare-measurement-lines.yaml").read_text(encoding="utf-8")
    ))
    registry = ToolRegistry(python_implementations=[contract.implementation])
    registry.register(contract)
    worker = ROOT / "src/geopilot/gis/tools/prepare_measurement_lines.py"
    step = "choose_measurement_method"
    for directory in ("inputs", "intermediate", f"steps/{step}"):
        (tmp_path / directory).mkdir(parents=True)
    source = tmp_path / "inputs/geographic.gpkg"
    reference = pyogrio.read_dataframe(GOLDEN, layer="roads", where="source_feature_id != 'road-far'")
    pyogrio.write_dataframe(reference.to_crs(4326), source, layer="roads")
    fingerprint = fingerprint_file(source)
    request = StepRequest(
        run_id="prepare-roads", step_key=step, tool=contract.ref, workdir_ref="prepare-roads",
        inputs={"source": {"path": "inputs/geographic.gpkg", "layer": "roads"}},
        parameters={"method": "local_projected", "target_crs": "EPSG:32650"},
        output_targets={"roads": {"path": "intermediate/roads.gpkg", "layer": "roads"}},
        log_path=f"steps/{step}/process.log", cancel_path="cancel.flag",
    )

    result = run_step(
        request, registry=registry, workdir=tmp_path,
        python_implementations={contract.implementation: worker},
    )

    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    output = result.outputs["roads"]
    actual = pyogrio.read_dataframe(tmp_path / output.path, layer=output.layer)
    assert actual.crs.to_epsg() == 32650 and actual.source_feature_id.tolist() == reference.source_feature_id.tolist()
    assert actual.geometry.hausdorff_distance(reference.geometry).max() < 1e-6
    assert fingerprint_file(source) == fingerprint
    assert result.environment["pyproj"]
    log = (tmp_path / result.log_path).read_text(encoding="utf-8")
    assert "prepare_measurement_lines.py --request" in log and "Prepared EPSG:32650 (meter)" in log
    persisted = json.loads((tmp_path / f"steps/{step}/request.json").read_text(encoding="utf-8"))
    assert persisted["parameters"] == request.parameters
