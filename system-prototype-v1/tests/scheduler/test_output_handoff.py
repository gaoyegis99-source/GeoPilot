from pathlib import Path
from shutil import copyfile

import pytest

from geopilot.domain.step_io import StepFileRef, StepOutput
from geopilot.execution.step_support import AdapterError
from geopilot.scheduler.output_resolver import resolve_named_output


SOURCE = Path(__file__).resolve().parents[1] / "fixtures/vector/road-buffer.gpkg"


def test_reopens_reported_named_layer_and_preserves_relative_reference(tmp_path):
    target = tmp_path / "output.gpkg"
    copyfile(SOURCE, target)
    ref = resolve_named_output(
        tmp_path, StepOutput(path=target.name, layer="roads", type="vector_line"), field="inputs.source",
    )
    assert ref == StepFileRef(path=target.name, layer="roads")


@pytest.mark.parametrize(("kind", "layer", "state", "code"), [
    ("vector_line", "roads", "missing", "OUTPUT_MISSING"),
    ("vector_line", "roads", "corrupt", "OUTPUT_INVALID"),
    ("vector_line", "wrong-layer", "valid", "OUTPUT_INVALID"),
    ("vector_polygon", "roads", "valid", "OUTPUT_INVALID"),
])
def test_named_output_reports_missing_or_unreadable_evidence(tmp_path, kind, layer, state, code):
    target = tmp_path / "output.gpkg"
    if state == "valid":
        copyfile(SOURCE, target)
    elif state == "corrupt":
        target.write_bytes(b"not a geopackage")
    with pytest.raises(AdapterError) as error:
        resolve_named_output(tmp_path, StepOutput(path=target.name, layer=layer, type=kind), field="inputs.source")
    assert (error.value.code, error.value.phase, error.value.field) == (code, "preflight", "inputs.source")
