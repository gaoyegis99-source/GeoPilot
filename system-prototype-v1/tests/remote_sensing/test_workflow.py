import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
import rasterio
import yaml
from shapely.geometry import box

from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import ExecutionStatus, StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.remote_sensing.product_metadata import read_product_metadata
from geopilot.tools.registry import ToolRegistry
from product_fixtures import LANDSAT, LANDSAT_CODES, SENTINEL, SENTINEL_CODES


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "src/geopilot/gis/tools/remote_sensing_workflow.py"
IMPLEMENTATION = "geopilot.remote_sensing_workflow"
STEPS = {
    "read-metadata": "read_product_metadata",
    "lookup-configuration": "lookup_sensor_configuration",
    "map-bands": "map_band_roles",
    "scale-reflectance": "apply_scale_and_offset",
    "quality-mask": "build_quality_mask",
    "calculate-ndvi": "calculate_ndvi",
    "align-dates": "align_two_dates",
    "calculate-change": "calculate_change",
}


@pytest.fixture
def run_workflow_step(tmp_path):
    (tmp_path / "inputs").mkdir()
    registry = ToolRegistry(python_implementations=(IMPLEMENTATION,))
    contracts = {}
    for suffix in STEPS:
        tool_id = f"python-remote-sensing-{suffix}"
        contract = ToolContract.model_validate(yaml.safe_load((ROOT / f"config/tools/{tool_id}.yaml").read_text()))
        assert contract.id == tool_id and contract.version == 1 and contract.implementation == IMPLEMENTATION
        registry.register(contract)
        contracts[suffix] = contract

    def launch(suffix, inputs, parameters=None, *, succeeds=True):
        contract = contracts[suffix]
        step = STEPS[suffix]
        directory = f"steps/{step}"
        (tmp_path / directory).mkdir(parents=True)
        targets = {}
        for role, kind in contract.outputs.items():
            extension = "tif" if kind.startswith("raster_") else "json"
            targets[role] = {"path": f"{directory}/{role}.{extension}"}
        request = StepRequest(
            run_id="p8-workflow", step_key=step, tool=contract.ref, workdir_ref="p8-workflow",
            inputs=inputs, parameters=parameters or {}, output_targets=targets,
            log_path=f"{directory}/process.log", cancel_path="cancel.flag",
        )
        result = run_step(
            request, registry=registry, workdir=tmp_path, python_implementations={IMPLEMENTATION: WORKER},
            input_preflight=preflight_step_inputs,
        )
        log = (tmp_path / result.log_path).read_text(encoding="utf-8")
        assert StepResult.model_validate_json((tmp_path / directory / "result.json").read_bytes()) == result
        assert (tmp_path / directory / "request.json").is_file()
        if succeeds:
            assert result.status is ExecutionStatus.SUCCEEDED and result.exit_code == 0, log
            assert (tmp_path / directory / "python-result.json").is_file()
            assert {role: output.type for role, output in result.outputs.items()} == contract.outputs
        else:
            assert result.status is ExecutionStatus.FAILED and result.error.code == "PROCESS_FAILED", log
            assert not result.outputs and all(not (tmp_path / ref["path"]).exists() for ref in targets.values())
        return result, log

    return launch


def _refs(result):
    return {role: {"path": output.path, "layer": output.layer} for role, output in result.outputs.items()}


def _report(workdir, result, role):
    return json.loads((workdir / result.outputs[role].path).read_text(encoding="utf-8"))


def _source(workdir, raw, role):
    path = raw[role]["path"]
    return {"path": path, "fingerprint": fingerprint_file(workdir / path).digest}


def _prepared(launch, raw, configurations):
    metadata, _ = launch("read-metadata", raw)
    configuration, _ = launch("lookup-configuration", _refs(metadata), configurations)
    mapping, _ = launch("map-bands", raw | _refs(configuration))
    return raw | _refs(configuration) | _refs(mapping), metadata, configuration, mapping


def _reflectance_and_quality(launch, inputs):
    scaled, _ = launch("scale-reflectance", inputs)
    masked, _ = launch("quality-mask", inputs)
    reflectance = {role: ref for role, ref in _refs(scaled).items() if role.endswith("_reflectance")}
    return reflectance | _refs(masked), scaled


@pytest.fixture
def products(tmp_path, sensor_registry, landsat_product_xml, sentinel_product_xml, write_product_stack):
    def make(*, mixed=True):
        first = tmp_path / "inputs/first.tif"
        second = tmp_path / "inputs/second.tif"
        write_product_stack(
            first, profile=LANDSAT, xml=landsat_product_xml(), codes=LANDSAT_CODES, pixel_m=30,
            values=[[[0, 8, 0], [0, 0, 0]], [[20000] * 3] * 2, [[0] * 3] * 2,
                    [[10000] * 3, [10000, 10000, 0]]],
        )
        if mixed:
            scl = np.full((6, 9), 4, dtype="uint16")
            scl[4:6, :2] = 9
            nir, red = np.full(scl.shape, 7000), np.full(scl.shape, 3000)
            red[1, 7] = 1000
            red[4, 4], nir[4, 4] = 2500, 8000
            write_product_stack(
                second, profile=SENTINEL, xml=sentinel_product_xml(), codes=SENTINEL_CODES,
                pixel_m=10, scl_resampling="nearest", values=[scl, nir, red],
            )
        else:
            write_product_stack(
                second, profile=LANDSAT, xml=landsat_product_xml(), codes=LANDSAT_CODES, pixel_m=30,
                values=[[[0] * 3] * 2, [[15000] * 3] * 2, [[0] * 3] * 2, [[10000] * 3] * 2],
            )
        raw = {f"{period}_product": {"path": path.relative_to(tmp_path).as_posix()}
               for period, path in (("first", first), ("second", second))}
        configurations = {f"{period}_configuration": sensor_registry.lookup(read_product_metadata(path).key).model_dump(mode="json")
                          for period, path in (("first", first), ("second", second))}
        return raw, configurations

    return make


def test_real_eight_child_steps_preserve_frozen_config_and_original_pixels(tmp_path, run_workflow_step, products):
    launch = run_workflow_step
    raw, configurations = products()
    sources = {role: _source(tmp_path, raw, role) for role in raw}
    extent = tmp_path / "inputs/extent.gpkg"
    pyogrio.write_dataframe(gpd.GeoDataFrame(geometry=[box(500000, 5099940, 500090, 5100000)], crs="EPSG:32632"),
                            extent, layer="study_area", driver="GPKG")
    inputs, metadata, configuration, mapping = _prepared(launch, raw, configurations)
    ndvi_inputs, scaled = _reflectance_and_quality(launch, inputs)
    ndvi, _ = launch("calculate-ndvi", ndvi_inputs)
    aligned, _ = launch(
        "align-dates", _refs(ndvi) | {"analysis_extent": {"path": "inputs/extent.gpkg", "layer": "study_area"}},
        {"resampling": "nearest"},
    )
    change, _ = launch("calculate-change", {"aligned_ndvi": _refs(aligned)["aligned_ndvi"]},
                       {"change_threshold": 0.1, "area_crs": "EPSG:6933"})

    metadata_report = _report(tmp_path, metadata, "product_metadata")
    configuration_report = _report(tmp_path, configuration, "sensor_configuration")
    mapping_report = _report(tmp_path, mapping, "band_mapping")
    calibration_report = _report(tmp_path, scaled, "calibration_report")
    for period in ("first", "second"):
        role = f"{period}_product"
        assert metadata_report[role]["metadata_xml"] == read_product_metadata(tmp_path / raw[role]["path"]).xml
        assert configuration_report[role]["configuration"] == configurations[f"{period}_configuration"]
        assert all(document[role]["source"] == sources[role]
                   for document in (metadata_report, configuration_report, mapping_report, calibration_report))
        with rasterio.open(tmp_path / ndvi.outputs[f"{period}_ndvi"].path) as raster:
            lineage = json.loads(raster.tags(ns="GEOPILOT_REMOTE_SENSING")["SOURCE"])
            assert lineage["period"] == period and lineage["source"] == sources[role]
            assert lineage["configuration"] == mapping_report[role]["configuration"]
            ndvi_summary = json.loads(raster.tags(ns="GEOPILOT_NDVI")["SUMMARY"])
            assert ndvi_summary["total_pixels"] == raster.width * raster.height
    assert mapping_report["first_product"]["bands"]["red"]["index"] == 4
    assert mapping_report["second_product"]["bands"]["red"]["index"] == 3
    assert calibration_report["first_product"]["bands"]["red"]["offset"] == -0.2
    assert calibration_report["second_product"]["bands"]["nir"]["dn_offset"] == -1000
    alignment = _report(tmp_path, aligned, "alignment_report")
    assert alignment["common_valid_pixels"] == 3 and alignment["warped"]
    with rasterio.open(tmp_path / change.outputs["change_raster"].path) as delta:
        np.testing.assert_allclose(delta.read(1), [[-5 / 34, np.nan, 6 / 17], [np.nan, 0, np.nan]], rtol=0, atol=1e-15)
        assert delta.crs.to_string() == "EPSG:32632" and delta.res == (30, 30)
    with rasterio.open(tmp_path / change.outputs["change_classes"].path) as classes:
        assert classes.read(1).tolist() == [[1, 255, 3], [255, 2, 255]]
    statistics = _report(tmp_path, change, "area_statistics")
    assert statistics["direction"] == "second_minus_first" and statistics["area_crs"] == "EPSG:6933"
    assert [entry["pixel_count"] for entry in statistics["classes"]] == [1, 1, 1]
    assert all(900 < entry["area_m2"] < 902 for entry in statistics["classes"])
    assert {role: _source(tmp_path, raw, role) for role in raw} == sources


def test_wrong_frozen_configuration_does_not_publish(tmp_path, run_workflow_step, products):
    raw, configurations = products()
    metadata, _ = run_workflow_step("read-metadata", raw)
    configurations["first_configuration"] = configurations["second_configuration"]
    _, log = run_workflow_step("lookup-configuration", _refs(metadata), configurations, succeeds=False)
    assert "SENSOR_CONFIG_NOT_FOUND" in log and "first_configuration" in log


@pytest.mark.parametrize("tamper", ["source", "mapping"])
def test_real_products_and_mapping_are_rechecked_before_processing(tmp_path, run_workflow_step, products, tamper):
    raw, configurations = products()
    inputs, _, _, mapping = _prepared(run_workflow_step, raw, configurations)
    if tamper == "source":
        with rasterio.open(tmp_path / raw["first_product"]["path"], "r+") as raster:
            band = raster.read(4)
            band[0, 0] = 9000
            raster.write(band, 4)
        _, log = run_workflow_step("scale-reflectance", inputs, succeeds=False)
        assert "first_product" in log and "fingerprint" in log
    else:
        path = tmp_path / mapping.outputs["band_mapping"].path
        report = json.loads(path.read_text(encoding="utf-8"))
        report["first_product"]["bands"]["red"]["index"] = 2
        path.write_text(json.dumps(report), encoding="utf-8")
        _, log = run_workflow_step("quality-mask", inputs, succeeds=False)
        assert "band_mapping" in log and "first_product" in log


def test_same_grid_but_different_raw_source_mask_is_rejected(tmp_path, run_workflow_step, products):
    raw, configurations = products(mixed=False)
    prepared, _, _, _ = _prepared(run_workflow_step, raw, configurations)
    inputs, _ = _reflectance_and_quality(run_workflow_step, prepared)
    first_quality = tmp_path / inputs["first_quality_mask"]["path"]
    with rasterio.open(tmp_path / inputs["second_quality_mask"]["path"]) as second:
        other = json.loads(second.tags(ns="GEOPILOT_REMOTE_SENSING")["SOURCE"])
    with rasterio.open(first_quality, "r+") as first:
        lineage = json.loads(first.tags(ns="GEOPILOT_REMOTE_SENSING")["SOURCE"])
        assert lineage["product_id"] == other["product_id"] and lineage["configuration"] == other["configuration"]
        assert lineage["source"]["fingerprint"] != other["source"]["fingerprint"]
        lineage["source"]["fingerprint"] = other["source"]["fingerprint"]
        first.update_tags(ns="GEOPILOT_REMOTE_SENSING", SOURCE=json.dumps(lineage))
    _, log = run_workflow_step("calculate-ndvi", inputs, succeeds=False)
    assert "first_quality_mask" in log and "source" in log


def test_second_period_calibration_failure_leaves_the_output_group_unpublished(tmp_path, run_workflow_step, products):
    raw, configurations = products(mixed=False)
    with rasterio.open(tmp_path / raw["second_product"]["path"], "r+") as raster:
        xml = raster.tags(ns="GEOPILOT_PRODUCT")["METADATA_XML"]
        xml = xml.replace("2.75E-05</REFLECTANCE_MULT_BAND_4>", "0</REFLECTANCE_MULT_BAND_4>")
        raster.update_tags(ns="GEOPILOT_PRODUCT", METADATA_XML=xml)
    inputs, _, _, _ = _prepared(run_workflow_step, raw, configurations)

    _, log = run_workflow_step("scale-reflectance", inputs, succeeds=False)

    assert "REFLECTANCE_MULT_BAND_4" in log and "positive" in log
    directory = tmp_path / "steps/apply_scale_and_offset"
    assert {path.name for path in directory.iterdir()} == {"request.json", "result.json", "process.log"}
