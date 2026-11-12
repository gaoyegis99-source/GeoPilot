import json
import os
import shutil
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
import rasterio
import yaml
from affine import Affine
from shapely.geometry import LineString, Polygon, box

from geopilot.checks.input_checks import (
    CoverageRequirement,
    InputCheckError,
    InputCheckResult,
    check_step_inputs,
    preflight_step_inputs,
    require_input_checks,
)
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepRequest
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.execution.step_support import PreparedStep
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/fixtures/vector/road-buffer.gpkg"


def _tool(name):
    return ToolContract.model_validate(yaml.safe_load((ROOT / f"config/tools/{name}.yaml").read_text(encoding="utf-8")))


def _prepared(workdir, contract, path, *, layer=None, parameters=None):
    request = {"inputs": {"source": {"path": path, "layer": layer}}, "parameters": parameters or {}}
    return PreparedStep(request, contract, workdir, workdir / "process.log", workdir / "request.json", workdir / "result.json")


def _checks(step, **kwargs):
    return {result.rule: result for result in check_step_inputs(step, **kwargs)}


def _local_contract(kind):
    return ToolContract(
        id="inspect-input", version=1, runner="python", implementation="inspect-input",
        inputs={"source": kind}, outputs={}, parameters={}, preconditions=[], check_rules=[],
        measurement_policy={"distance": "not_applicable", "area": "not_applicable"},
    )


def test_golden_line_metadata_geometry_and_projection_pass(tmp_path):
    shutil.copyfile(GOLDEN, tmp_path / "input.gpkg")
    step = _prepared(tmp_path, _tool("qgis-buffer"), "input.gpkg", layer="roads")

    results = _checks(step)

    assert all(result.passed and result.phase == "input" and result.severity == "blocking" for result in results.values())
    assert results["crs_is_known"].observed == "EPSG:32650"
    assert results["horizontal_unit_is_meter"].observed["metres_per_unit"] == 1.0
    assert results["input_extent_is_valid"].observed == [500150.0, 4401500.0, 501850.0, 4402650.0]
    assert results["geometry_is_valid"].observed["feature_count"] == 3
    assert results["projected_crs_suitable_for_extent"].field == "inputs.source.crs"
    assert results["projected_crs_suitable_for_extent"].expected["method"] == "local_projected"


@pytest.mark.parametrize("layer,field,code", [
    ("missing", "inputs.source.layer", "SOURCE_UNREADABLE"),
    ("analysis_extent", "inputs.source", "METHOD_PRECONDITION_FAILED"),
])
def test_multilayer_input_requires_the_actual_expected_layer_and_type(tmp_path, layer, field, code):
    shutil.copyfile(GOLDEN, tmp_path / "input.gpkg")
    step = _prepared(tmp_path, _tool("qgis-buffer"), "input.gpkg", layer=layer)

    with pytest.raises(InputCheckError) as raised:
        require_input_checks(check_step_inputs(step))

    assert (raised.value.field, raised.value.code) == (field, code)


@pytest.mark.parametrize("epsg,blocked", [
    (4326, "horizontal_unit_is_meter"), (3857, "projected_crs_suitable_for_extent"),
    (2277, "horizontal_unit_is_meter"), (None, "crs_is_known"),
])
def test_actual_unknown_geographic_web_mercator_and_feet_are_blocked(tmp_path, epsg, blocked):
    if epsg == 2277:
        frame = gpd.GeoDataFrame(geometry=[LineString([(-97.74, 30.27), (-97.73, 30.27)])], crs=4326).to_crs(epsg)
    else:
        frame = pyogrio.read_dataframe(GOLDEN, layer="roads")
        frame = frame.to_crs(epsg) if epsg is not None else frame.set_crs(None, allow_override=True)
    path = tmp_path / "input.gpkg"
    if epsg is None:
        with pytest.warns(UserWarning, match="'crs' was not provided"):
            pyogrio.write_dataframe(frame, path, layer="roads")
    else:
        pyogrio.write_dataframe(frame, path, layer="roads")
    step = _prepared(tmp_path, _tool("qgis-buffer"), path.name, layer="roads")

    results = check_step_inputs(step)

    with pytest.raises(InputCheckError) as raised:
        require_input_checks(results)
    failure = next(result for result in raised.value.blocking if result.rule == blocked)
    assert failure.phase == "input" and failure.severity == "blocking" and failure.expected is not None
    assert failure.field == "inputs.source.crs" or failure.field == "inputs.source.crs.unit"
    assert raised.value.results == results
    if epsg == 3857:
        assert next(result for result in results if result.rule == "horizontal_unit_is_meter").passed


def test_extent_outside_utm_area_of_use_is_blocked(tmp_path):
    points = LineString([(122.0, 40.0), (122.01, 40.01)])
    frame = gpd.GeoDataFrame(geometry=[points], crs=4326).to_crs(32650)
    pyogrio.write_dataframe(frame, tmp_path / "outside.gpkg", layer="roads")

    result = _checks(_prepared(tmp_path, _tool("qgis-buffer"), "outside.gpkg", layer="roads"))[
        "projected_crs_suitable_for_extent"
    ]

    assert not result.passed and "outside the area of use" in result.message
    assert result.observed["crs"] == "EPSG:32650" and result.observed["detail"][0] > 120


@pytest.mark.parametrize("epsg,passed", [(6933, True), (32650, False)])
def test_area_policy_uses_real_equal_area_projection(tmp_path, epsg, passed):
    frame = pyogrio.read_dataframe(GOLDEN, layer="analysis_extent").to_crs(epsg)
    pyogrio.write_dataframe(frame, tmp_path / "area.gpkg", layer="extent")
    step = _prepared(tmp_path, _tool("python-area"), "area.gpkg", layer="extent")

    results = _checks(step)

    assert results["horizontal_unit_is_meter"].passed
    assert results["equal_area_crs_suitable_for_extent"].passed == passed
    assert results["equal_area_crs_suitable_for_extent"].observed["crs"] == f"EPSG:{epsg}"


def test_geometry_checks_actual_self_intersections_and_null_features(tmp_path):
    bowtie = Polygon([(500000, 4400000), (500100, 4400100), (500100, 4400000), (500000, 4400100)])
    frame = gpd.GeoDataFrame(geometry=[bowtie, None], crs=32650)
    path = tmp_path / "invalid.gpkg"
    pyogrio.write_dataframe(frame, path, layer="extent", geometry_type="Polygon")
    before = fingerprint_file(path)

    result = _checks(_prepared(tmp_path, _tool("qgis-expand-analysis-extent"), path.name, layer="extent"))["geometry_is_valid"]

    assert not result.passed and result.field == "inputs.source.geometry"
    assert result.observed["feature_count"] == result.observed["invalid_count"] == 2
    assert fingerprint_file(path) == before


def test_rotated_tiff_coverage_uses_affine_footprint_instead_of_envelope(tmp_path):
    path = tmp_path / "rotated.tif"
    transform = Affine(1, 1, 500000, 1, -1, 4400000)
    with rasterio.open(path, "w", driver="GTiff", width=4, height=4, count=1, dtype="uint8",
                       crs="EPSG:32650", transform=transform) as raster:
        raster.write(np.arange(16, dtype="uint8").reshape(1, 4, 4))
    step = _prepared(tmp_path, _local_contract("raster"), path.name)
    inside = CoverageRequirement((500003, 4399999, 500005, 4400001), "EPSG:32650")
    outside_grid = CoverageRequirement((500000.25, 4400002.25, 500001, 4400003), "EPSG:32650")

    result = _checks(step, required_coverage={"source": inside})["data_covers_required_extent"]
    failed = _checks(step, required_coverage={"source": outside_grid})["data_covers_required_extent"]

    assert result.passed and not failed.passed
    assert failed.expected == {"bounds": list(outside_grid.bounds), "crs": "EPSG:32650"}
    assert failed.observed["bounds"] == [500000.0, 4399996.0, 500008.0, 4400004.0]
    assert failed.field == "inputs.source.coverage"


def test_polygon_coverage_does_not_treat_holes_or_mismatched_crs_as_covered(tmp_path):
    outer = box(500000, 4400000, 500100, 4400100)
    hole = box(500040, 4400040, 500060, 4400060)
    frame = gpd.GeoDataFrame(geometry=[Polygon(outer.exterior.coords, [hole.exterior.coords])], crs=32650)
    pyogrio.write_dataframe(frame, tmp_path / "coverage.gpkg", layer="extent")
    step = _prepared(tmp_path, _local_contract("vector_polygon"), "coverage.gpkg", layer="extent")
    inside = CoverageRequirement((500001, 4400001, 500010, 4400010), "EPSG:32650")
    in_hole = CoverageRequirement((500045, 4400045, 500055, 4400055), "EPSG:32650")
    wrong_crs = CoverageRequirement(inside.bounds, "EPSG:32651")

    for requested, passed in ((inside, True), (in_hole, False), (wrong_crs, False)):
        result = _checks(step, required_coverage={"source": requested})["data_covers_required_extent"]
        assert result.passed == passed


def test_attribute_filter_checks_fields_without_requiring_measurement_crs(tmp_path):
    points = pyogrio.read_dataframe(GOLDEN, layer="targets").set_crs(None, allow_override=True)
    with pytest.warns(UserWarning, match="'crs' was not provided"):
        pyogrio.write_dataframe(points, tmp_path / "targets.gpkg", layer="targets")
    tool = _tool("qgis-extract-by-attribute")

    present = _checks(_prepared(tmp_path, tool, "targets.gpkg", layer="targets", parameters={"field": "source_feature_id"}))
    missing = _checks(_prepared(tmp_path, tool, "targets.gpkg", layer="targets", parameters={"field": "absent"}))

    assert all(result.passed for result in present.values())
    assert "crs_is_known" not in present
    assert not missing["selected_field_exists"].passed
    assert missing["selected_field_exists"].observed == ["source_feature_id"]


def test_clip_requires_matching_actual_crs_across_roles(tmp_path):
    shutil.copyfile(GOLDEN, tmp_path / "input.gpkg")
    extent = pyogrio.read_dataframe(GOLDEN, layer="analysis_extent").to_crs(32651)
    pyogrio.write_dataframe(extent, tmp_path / "other-crs.gpkg", layer="extent")
    step = _prepared(tmp_path, _tool("python-clip-points-to-extent"), "input.gpkg", layer="targets")
    step.request["inputs"]["extent"] = {"path": "other-crs.gpkg", "layer": "extent"}

    result = _checks(step)["input_crs_match"]

    assert not result.passed and result.observed == {"source": "EPSG:32650", "extent": "EPSG:32651"}


def test_warning_can_be_reported_without_blocking():
    warning = InputCheckResult(
        rule="coverage_warning", field="inputs.source.coverage", severity="warning", passed=False,
        observed="partial", expected="full", message="Coverage is partial",
    )

    assert warning.phase == "input"
    require_input_checks((warning,))


@pytest.fixture(scope="module")
def qgis_configuration():
    executable = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if not executable:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS for the actual preflight and process test")
    tool = _tool("qgis-expand-analysis-extent")
    registry = ToolRegistry(qgis_process=executable)
    registry.register(tool)
    bindings = yaml.safe_load((ROOT / "config/runner_bindings/qgis-road-workflow.yaml").read_text(encoding="utf-8"))
    return tool, registry, bindings, executable


@pytest.mark.parametrize("invalid", [False, True])
def test_actual_run_step_uses_opt_in_input_gate(tmp_path, qgis_configuration, invalid):
    tool, registry, bindings, executable = qgis_configuration
    for directory in ("inputs", "outputs", "steps/expand"):
        (tmp_path / directory).mkdir(parents=True)
    source = tmp_path / "inputs/extent.gpkg"
    frame = pyogrio.read_dataframe(GOLDEN, layer="analysis_extent")
    if invalid:
        frame = frame.set_geometry([Polygon([(500000, 4400000), (501000, 4401000), (501000, 4400000), (500000, 4401000)])])
    pyogrio.write_dataframe(frame, source, layer="extent")
    original = fingerprint_file(source)
    request = StepRequest(
        run_id="input-gate", step_key="expand", tool=tool.ref, workdir_ref="input-gate",
        inputs={"source": {"path": "inputs/extent.gpkg", "layer": "extent"}},
        parameters={"distance": 500, "segments": 16, "dissolve": True, "end_cap_style": "round",
                    "join_style": "round", "miter_limit": 2},
        output_targets={"expanded_extent": {"path": "outputs/extent.gpkg", "layer": "expanded_extent"}},
        log_path="steps/expand/process.log", cancel_path="cancel.flag",
    )

    result = run_step(
        request, registry=registry, workdir=tmp_path, qgis_bindings=bindings, qgis_process=executable,
        input_preflight=preflight_step_inputs,
    )

    log = (tmp_path / request.log_path).read_text(encoding="utf-8")
    records = [json.loads(line.removeprefix("input_check: ")) for line in log.splitlines() if line.startswith("input_check: ")]
    geometry = next(row for row in records if row["rule"] == "geometry_is_valid")
    assert geometry["phase"] == "input" and geometry["severity"] == "blocking"
    assert geometry["passed"] is not invalid and geometry["observed"]["invalid_count"] == int(invalid)
    assert geometry["expected"] is not None
    assert fingerprint_file(source) == original
    if invalid:
        assert result.status == "FAILED" and result.exit_code is None
        assert result.error.field == "inputs.source.geometry"
        assert not any(line.startswith("$ ") for line in log.splitlines())
        assert not list((tmp_path / "outputs").iterdir())
    else:
        assert result.status == "SUCCEEDED" and result.exit_code == 0, result
        assert "--json run native:buffer -" in log and "measurement: " in log
        output = pyogrio.read_dataframe(tmp_path / "outputs/extent.gpkg", layer="expanded_extent")
        assert output.crs.to_epsg() == 32650
        assert output.total_bounds == pytest.approx([499500, 4399500, 502500, 4402500])
