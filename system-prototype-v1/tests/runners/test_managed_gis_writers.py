import json
import shutil
import signal
from pathlib import Path

import numpy as np
import pyogrio
import pytest
import rasterio
import yaml
from rasterio.transform import from_origin

from geopilot.domain.step_io import ExecutionStatus, StepRequest
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.output_staging import OUTPUT_STAGING_ENV, owned_output_staging
from geopilot.execution.runner import run_step
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/vector/t13"
CASES = {
    "vector": ("python-select-spatial-relation", "select_spatial_relation.py", ".vector-step-"),
    "raster": ("python-dem-threshold", "classify_dem_threshold.py", ".raster-threshold-"),
    "remote": ("python-remote-sensing-calculate-change", "remote_sensing_workflow.py", ".ndvi-change-"),
}
HOOK = '''
import json
import os
import runpy
import time
from pathlib import Path

original_link = os.link

def observed_link(source, target, *args, **kwargs):
    path = Path(source)
    if path.parent.name.startswith({prefix!r}):
        destination = Path(target)
        evidence = {{"source": str(path.absolute()), "destination": str(destination.absolute()),
                     "bytes": path.stat().st_size,
                     "same_device": path.stat().st_dev == destination.parent.stat().st_dev}}
        Path("ready.tmp").write_text(json.dumps(evidence))
        Path("ready.tmp").replace("ready.json")
        if {interrupt!r}:
            time.sleep(60)
    return original_link(source, target, *args, **kwargs)

os.link = observed_link
runpy.run_path({script!r}, run_name="__main__")
'''


def _write_raster(path, values, *, descriptions, units):
    count, height, width = values.shape
    with rasterio.open(
        path, "w", driver="GTiff", count=count, height=height, width=width, dtype=values.dtype,
        crs="EPSG:32632", transform=from_origin(500000, 5100000, 10, 10), nodata=np.nan,
    ) as dataset:
        dataset.descriptions = descriptions
        dataset.units = units
        dataset.write(values)


def _request(workdir, case, contract):
    if case == "vector":
        source = workdir / "inputs/relations.gpkg"
        shutil.copyfile(FIXTURE / "spatial-relations.gpkg", source)
        inputs = {role: {"path": "inputs/relations.gpkg", "layer": layer}
                  for role, layer in (("targets", "points"), ("area", "area"))}
        parameters = {"spatial_relation": "intersects"}
        outputs = {"targets": {"path": "outputs/targets.gpkg", "layer": "targets"}}
    elif case == "raster":
        source = workdir / "inputs/terrain.tif"
        values = np.asarray([[[-1, 0, 2, np.nan]]], dtype="float32")
        _write_raster(source, values, descriptions=("terrain",), units=("degree",))
        inputs = {"terrain_raster": {"path": "inputs/terrain.tif"}}
        parameters = {"threshold": {"intervals": [
            {"class_id": 7, "lower": 0, "upper": 1, "include_lower": True, "include_upper": True},
            {"class_id": 8, "lower": 1, "upper": None, "include_lower": False, "include_upper": False},
        ]}}
        outputs = {"threshold_regions": {"path": "outputs/threshold.tif"}}
    else:
        source = workdir / "inputs/aligned.tif"
        values = np.asarray([[[0.5, 0.2, 0.25, np.nan]], [[0.25, 0.2, 0.75, np.nan]]])
        _write_raster(source, values, descriptions=("first_ndvi", "second_ndvi"), units=("1", "1"))
        inputs = {"aligned_ndvi": {"path": "inputs/aligned.tif"}}
        parameters = {"change_threshold": 0.1, "area_crs": "EPSG:6933"}
        outputs = {
            "change_raster": {"path": "outputs/change.tif"},
            "change_classes": {"path": "outputs/classes.tif"},
            "area_statistics": {"path": "outputs/statistics.json"},
        }
    request = StepRequest(
        run_id="writers", step_key="write", tool=contract.ref, workdir_ref="writers",
        inputs=inputs, parameters=parameters, output_targets=outputs,
        log_path="steps/write/process.log", cancel_path="cancel.flag",
    )
    return request, source


def _verify_outputs(workdir, case, request, source):
    outputs = {role: workdir / ref.path for role, ref in request.output_targets.items()}
    if case == "vector":
        output = pyogrio.read_dataframe(outputs["targets"], layer="targets")
        original = pyogrio.read_dataframe(source, layer="points")
        truth = json.loads((FIXTURE / "expected.json").read_text())["relations"]["intersects"]["points"]
        expected = original.loc[original.source_feature_id.isin(truth)].reset_index(drop=True)
        assert output.crs == original.crs and sorted(output.source_feature_id) == truth
        assert output.drop(columns="geometry").equals(expected.drop(columns="geometry"))
        assert output.geometry.to_wkb().tolist() == expected.geometry.to_wkb().tolist()
        return {"ids": output.source_feature_id.tolist(), "crs": output.crs.to_string()}
    role = "threshold_regions" if case == "raster" else "change_classes"
    expected = [[0, 7, 8, 255]] if case == "raster" else [[1, 2, 3, 255]]
    with rasterio.open(source) as original, rasterio.open(outputs[role]) as result:
        assert result.crs == original.crs and result.transform == original.transform and result.nodata == 255
        assert result.read(1).tolist() == expected
        assert result.read_masks(1).tolist() == [[255, 255, 255, 0]]
    if case == "remote":
        with rasterio.open(outputs["change_raster"]) as delta:
            np.testing.assert_array_equal(delta.read(1), [[-0.25, 0, 0.5, np.nan]])
        report = json.loads(outputs["area_statistics"].read_text())
        assert [entry["pixel_count"] for entry in report["classes"]] == [1, 1, 1]
    return {"pixels": expected}


def exercise_writer(workdir, case, *, interrupt, managed, source_root=ROOT):
    for folder in ("inputs", "outputs", "steps/write", "outputs/.unrelated"):
        (workdir / folder).mkdir(parents=True)
    sentinel = workdir / "outputs/.unrelated/retained"
    sentinel.write_bytes(b"retain")
    tool_id, worker_name, prefix = CASES[case]
    tool = source_root / f"config/tools/{tool_id}.yaml"
    contract = ToolContract.model_validate(yaml.safe_load(tool.read_text()))
    registry = ToolRegistry(python_implementations=(contract.implementation,))
    registry.register(contract)
    request, source = _request(workdir, case, contract)
    before = source.read_bytes()
    probe = workdir / "probe.py"
    worker = source_root / "src/geopilot/gis/tools" / worker_name
    probe.write_text(HOOK.format(prefix=prefix, interrupt=interrupt, script=str(worker)))
    marker = workdir / "ready.json"

    result = run_step(
        request, registry=registry, workdir=workdir, python_implementations={contract.implementation: probe},
        timeout=8, cancel_requested=marker.is_file if interrupt else None,
    )
    assert marker.is_file(), (result, (workdir / request.log_path).read_text())
    observation = json.loads(marker.read_text())
    staged = Path(observation["source"])
    destination = Path(observation["destination"])
    assert observation["bytes"] > 0 and observation["same_device"]
    assert source.read_bytes() == before and sentinel.read_bytes() == b"retain"
    assert not list(workdir.rglob(".python-output-*"))
    if case == "remote":
        assert staged.parent.parent == destination.parent
        assert destination.parent.name.startswith(".land-use-")
        assert destination.parent.parent.name.startswith(".python-output-")
    elif managed:
        assert staged.parent.parent.name.startswith(".python-output-")
        assert staged.parent.parent.parent == destination.parent
    else:
        assert staged.parent.parent == destination.parent

    if interrupt:
        assert (result.status, result.exit_code, result.error.code) == (
            ExecutionStatus.CANCELLED, -signal.SIGTERM, "PROCESS_CANCELLED",
        )
        assert not result.outputs and all(not (workdir / ref.path).exists() for ref in request.output_targets.values())
        assert staged.exists() == (not managed and case != "remote")
        if staged.exists():
            assert staged.stat().st_size == observation["bytes"]
        details = {}
    else:
        assert (result.status, result.exit_code) == (ExecutionStatus.SUCCEEDED, 0), result
        assert set(result.outputs) == set(request.output_targets) and not staged.exists()
        details = _verify_outputs(workdir, case, request, source)

    return {
        "case": case, "managed": managed, "interrupted": interrupt, "status": result.status,
        "exit": result.exit_code, "stage": staged.relative_to(workdir).as_posix(),
        "staged_bytes_before_stop": observation["bytes"], "stage_remains": staged.exists(),
        "destination": destination.relative_to(workdir).as_posix(), "outputs": sorted(result.outputs),
        "independent_reference": details,
    }


@pytest.mark.parametrize("case", list(CASES))
@pytest.mark.parametrize("interrupt", [False, True], ids=["success", "interrupt"])
def test_real_managed_writer_preserves_results_and_reclaims_stage(tmp_path, case, interrupt):
    print(json.dumps(exercise_writer(tmp_path, case, interrupt=interrupt, managed=True)))


def test_only_exact_declared_targets_use_owned_stage(tmp_path, monkeypatch):
    from geopilot.execution.output_staging import child_output_staging_parent, temporary_output_directory

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(OUTPUT_STAGING_ENV, raising=False)
    destination = Path("outputs/result.gpkg")
    destination.parent.mkdir()
    request = {
        "log_path": "process.log", "cancel_path": "cancel.flag", "inputs": {},
        "output_targets": {"result": {"path": str(destination)}},
    }
    assert child_output_staging_parent(destination) == destination.parent
    with temporary_output_directory(destination, prefix=".test-") as direct:
        assert Path(direct).absolute().parent == destination.parent.absolute()
    with owned_output_staging(tmp_path, request, {}) as env:
        monkeypatch.setenv(OUTPUT_STAGING_ENV, env[OUTPUT_STAGING_ENV])
        stage = child_output_staging_parent(destination)
        assert stage.parent == destination.parent.absolute()
        assert stage.name.startswith(".python-output-")
        assert child_output_staging_parent(destination.absolute()) == stage
        for undeclared in (destination.with_name("scratch.gpkg"), stage / "nested.gpkg", Path("outside.gpkg")):
            assert child_output_staging_parent(undeclared) == undeclared.parent
