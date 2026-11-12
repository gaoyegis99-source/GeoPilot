import json
import math
import time
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
from fastapi.testclient import TestClient
from pyproj import Geod, Transformer
from rasterio.io import MemoryFile

from geopilot.api.server import create_local_app
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepResult
from geopilot.remote_sensing.indices import NDVI_DOMAIN
from remote_sensing_fixtures import AREA_CRS, CRS, FIRST_GRID, SECOND_GRID, write_mixed_products


ROOT = Path(__file__).resolve().parents[2]
METHOD = {"id": "remote-sensing-index-change", "version": 1}
STEPS = [
    "read_product_metadata", "lookup_sensor_configuration", "map_band_roles", "apply_scale_and_offset",
    "build_quality_mask", "calculate_ndvi", "align_two_dates", "calculate_change",
]
OPTIONS = {"resampling": "nearest", "change_threshold": 0.25, "area_crs": AREA_CRS}
ROLES = {
    "sensor_configuration", "calibration_report", "first_quality_mask", "second_quality_mask", "first_ndvi",
    "second_ndvi", "alignment_report", "change_raster", "change_classes", "area_statistics",
}
CHECK_RULES = {
    "product_configuration_matches", "common_valid_pixels_aligned", "change_direction_is_second_minus_first",
    "ndvi_values_in_range",
}
EXPECTED_CHANGE = np.array([
    [0.5, -5 / 34, -11 / 17, np.nan, np.nan],
    [np.nan, np.nan, np.nan, np.nan, 0.0],
    [np.nan, np.nan, np.nan, np.nan, np.nan],
])
EXPECTED_CLASSES = [[3, 2, 1, 255, 255], [255, 255, 255, 255, 2], [255, 255, 255, 255, 255]]
CONFIG_IDS = {"first_product": "landsat-8-c2-l2sp", "second_product": "sentinel-2-l2a-pb05-11"}


def _get(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response


def _post(client, path, data, *, status=200):
    response = client.post(path, json=data)
    assert response.status_code == status, response.text
    return response.json()


def _draft(client, paths, *, critical_options=OPTIONS):
    refs = {}
    for role, (path, layer) in paths.items():
        asset = _post(client, "/api/assets", {"path": str(path), "layer": layer})
        refs[role] = {key: asset[key] for key in ("asset_id", "asset_version")}
    goal = {"summary": "检查两期植被指数变化及等积面积"}
    task = _post(client, "/api/tasks", {"goal": goal}, status=201)["task_id"]
    plan = _post(client, f"/api/tasks/{task}/plans", {
        "method": METHOD, "inputs": refs, "critical_options": critical_options,
    })
    assert plan["task_id"] == task and plan["inputs"] == refs
    return plan, refs


def _run(client, tmp_path, plan, *, check_status):
    path = f"/api/plans/{plan['id']}"
    version = {"plan_version": plan["version"]}
    confirmed = _post(client, path + "/confirm", version)
    assert confirmed["status"] == "confirmed" and confirmed["issues"] == []
    assert confirmed["critical_options"] == plan["critical_options"]
    assert _get(client, f"/api/tasks/{plan['task_id']}").json()["runs"] == []
    run_id = _post(client, path + "/runs", version, status=202)["run_id"]
    endpoint = f"/api/runs/{run_id}"
    deadline = time.monotonic() + 90
    while True:
        state = _get(client, endpoint).json()
        if state["execution_status"] in {"FAILED", "CANCELLED"} or state["current_report_id"] or time.monotonic() >= deadline:
            break
        time.sleep(0.1)

    workdir = tmp_path / "workspace" / "tasks" / plan["task_id"] / "runs" / run_id
    assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", check_status), (state, workdir)
    assert _get(client, endpoint + "/origin").json() == {
        "run_id": run_id, "task_id": plan["task_id"], "plan_id": plan["id"], "plan_version": plan["version"],
    }
    steps = _get(client, endpoint + "/steps").json()["steps"]
    assert [step["step_key"] for step in steps] == STEPS
    assert all(step["status"] == "SUCCEEDED" and step["log_available"] for step in steps)
    for key in STEPS:
        result = StepResult.model_validate_json((workdir / "steps" / key / "result.json").read_text())
        assert result.step_key == key and result.exit_code == 0 and result.environment["python"]
        log = _get(client, endpoint + f"/steps/{key}/log").text
        assert any(line.startswith("$ ") and " --request " in line for line in log.splitlines())

    report = _get(client, endpoint + "/checks").json()["current_report"]
    assert report["id"] == state["current_report_id"] and report["status"] == check_status
    assert CHECK_RULES <= set(report["report"]["completed_rules"])
    assert all(report["report"]["rules"][rule]["passed"] for rule in CHECK_RULES - {"ndvi_values_in_range"})
    artifacts = {item["role"]: item for item in _get(client, endpoint + "/artifacts").json()["artifacts"]}
    assert set(artifacts) == ROLES
    manifest = json.loads((workdir / "outputs" / "manifest.json").read_text())["artifacts"]
    assert set(manifest) == ROLES
    confirmed_steps = {item["key"]: item for item in confirmed["steps"]}
    for role, artifact in artifacts.items():
        source = artifact["source"]
        assert artifact["readable"] and artifact["fingerprint"] == manifest[role]["fingerprint"]
        assert fingerprint_file(workdir / artifact["uri"]).digest == artifact["fingerprint"]
        assert (source["plan_id"], source["plan_version"]) == (plan["id"], plan["version"])
        assert source["tool"] == confirmed_steps[source["step_key"]]["tool"]
        assert all(plan["inputs"][key] == ref for key, ref in source["inputs"].items())
    return artifacts, report, workdir


def _download(client, artifact):
    return _get(client, f"/api/artifacts/{artifact['id']}/file")


def _ellipsoid_area(row, column):
    corners = [(column, row), (column + 1, row), (column + 1, row + 1), (column, row + 1)]
    vertices = [FIRST_GRID * point for point in corners]
    edges = [np.linspace(a, b, 256, endpoint=False) for a, b in zip(vertices, vertices[1:] + vertices[:1])]
    x, y = np.concatenate(edges).T
    lon, lat = Transformer.from_crs(CRS, 4326, always_xy=True).transform(x, y)
    area, _ = Geod(ellps="WGS84").polygon_area_perimeter(lon, lat)
    return abs(area)


def _open_result(workdir, step, role):
    result = StepResult.model_validate_json((workdir / "steps" / step / "result.json").read_text())
    return workdir / result.outputs[role].path


def _assert_registered_change(client, artifacts):
    change = artifacts["change_raster"]
    with MemoryFile(_download(client, change).content) as memory, memory.open() as dataset:
        assert dataset.crs.to_string() == CRS and dataset.transform == FIRST_GRID and dataset.shape == (3, 5)
        assert dataset.descriptions == ("ndvi_change",) and dataset.dtypes == ("float64",)
        assert dataset.units == ("1",) and math.isnan(dataset.nodata)
        values = dataset.read(1, masked=True)
        np.testing.assert_array_equal(values.mask, np.isnan(EXPECTED_CHANGE))
        np.testing.assert_allclose(values.filled(np.nan), EXPECTED_CHANGE, rtol=0, atol=1e-15)
    with MemoryFile(_download(client, artifacts["change_classes"]).content) as memory, memory.open() as dataset:
        assert dataset.crs.to_string() == CRS and dataset.transform == FIRST_GRID
        assert dataset.dtypes == ("uint8",) and dataset.nodata == 255
        assert dataset.read(1).tolist() == EXPECTED_CLASSES

    alignment = _download(client, artifacts["alignment_report"]).json()
    assert alignment["warped"] and alignment["resampling"] == "nearest"
    assert alignment["first_grid"]["transform"] == alignment["target_grid"]["transform"] == list(FIRST_GRID)[:6]
    assert alignment["second_grid"]["transform"] == list(SECOND_GRID)[:6]
    assert alignment["first_grid"]["resolution"] == [30, 30] and alignment["second_grid"]["resolution"] == [10, 10]
    assert (alignment["analysis_pixels"], alignment["common_valid_pixels"]) == (14, 4)
    return alignment


@pytest.fixture
def workbench(tmp_path):
    app = create_local_app(tmp_path / "project.sqlite", tmp_path / "workspace")
    with TestClient(app) as client:
        yield client


def test_remote_sensing_method_is_registered_with_locked_eight_step_order(workbench):
    response = workbench.get("/api/methods")
    assert response.status_code == 200, response.text
    methods = {entry["definition"]["id"]: entry for entry in response.json()["methods"]}
    assert METHOD["id"] in methods
    entry = methods[METHOD["id"]]
    definition = entry["definition"]
    assert definition["version"] == METHOD["version"] and definition["locked_order"] is True
    assert entry["available"] is True, entry["issues"]
    assert set(definition["inputs"]) == {"first_product", "second_product", "analysis_extent"}
    assert set(OPTIONS) <= definition["critical_options"].keys()
    assert set(entry["derived_options"]) == {"first_configuration", "second_configuration"}
    assert [step["key"] for step in definition["steps"]] == STEPS


def test_t10_t11_mixed_native_products_execute_and_publish_original_pixels(workbench, tmp_path):
    paths = write_mixed_products(tmp_path / "sources")
    fingerprints = {role: fingerprint_file(path) for role, (path, _) in paths.items()}
    plan, refs = _draft(workbench, paths, critical_options={
        **OPTIONS, "first_configuration": {"id": "untrusted-configuration", "version": 999},
    })
    assert plan["issues"] == [] and [step["key"] for step in plan["steps"]] == STEPS
    assert all(plan["critical_options"][key] == value for key, value in OPTIONS.items())
    configs = {role: yaml.safe_load((ROOT / "config" / "sensors" / f"{config}.v1.yaml").read_text())
               for role, config in CONFIG_IDS.items()}
    for role, period in (("first_product", "first"), ("second_product", "second")):
        assert plan["critical_options"][f"{period}_configuration"] == configs[role]

    artifacts, report, workdir = _run(workbench, tmp_path, plan, check_status="PASSED")

    assert report["report"]["rules"]["ndvi_values_in_range"]["passed"] is True
    assert artifacts["change_raster"]["source"]["inputs"] == refs
    assert artifacts["area_statistics"]["source"]["inputs"] == refs
    products = _download(workbench, artifacts["sensor_configuration"]).json()
    calibration = _download(workbench, artifacts["calibration_report"]).json()
    mapping = json.loads(_open_result(workdir, "map_band_roles", "band_mapping").read_text())
    for role, config in configs.items():
        assert products[role]["configuration"] == config
        expected_key = {key: config[key] for key in ("sensor", "product_level", "processing_version")}
        for record in (products[role], calibration[role], mapping[role]):
            assert record["product_key"] == expected_key
            assert record["product_id"] == products[role]["product_id"]
            assert record["source"]["fingerprint"] == fingerprints[role].digest
            assert fingerprint_file(workdir / record["source"]["path"]).digest == fingerprints[role].digest
    assert products["first_product"]["product_id"].startswith("LC08_L2SP_043035_20240701_")
    assert products["second_product"]["product_id"].startswith("S2A_MSIL2A_20240831T103021_N0511_")
    landsat = calibration["first_product"]["bands"]
    sentinel = calibration["second_product"]["bands"]
    for role, product_code, index, number in (("red", "SR_B4", 4, 4), ("nir", "SR_B5", 2, 5)):
        band = landsat[role]
        assert (band["source_code"], band["source_index"]) == (product_code, index)
        assert (band["scale"], band["offset"]) == (0.0000275, -0.2)
        assert band["scale_field"]["path"] == ["LEVEL2_SURFACE_REFLECTANCE_PARAMETERS", f"REFLECTANCE_MULT_BAND_{number}"]
        assert band["offset_field"]["path"] == ["LEVEL2_SURFACE_REFLECTANCE_PARAMETERS", f"REFLECTANCE_ADD_BAND_{number}"]
    for role, product_code, index, band_id in (("red", "B04", 3, 3), ("nir", "B08", 2, 7)):
        band = sentinel[role]
        assert (band["source_code"], band["source_index"]) == (product_code, index)
        assert (band["quantification"], band["dn_offset"]) == (10000.0, -1000.0)
        assert band["dn_offset_field"]["band_id"] == band_id
        assert mapping["second_product"]["bands"][role]["code"] == product_code

    with rasterio.open(_open_result(workdir, "apply_scale_and_offset", "first_reflectance")) as dataset:
        np.testing.assert_allclose(dataset.read()[:, 0, 1], [0.075, 0.35], rtol=0, atol=1e-15)
    with rasterio.open(_open_result(workdir, "apply_scale_and_offset", "second_reflectance")) as dataset:
        np.testing.assert_array_equal(dataset.read()[:, 2, 2], [0.125, 0.375])
        np.testing.assert_array_equal(dataset.read()[:, 5, 2], [-0.02, 0.02])
    with MemoryFile(_download(workbench, artifacts["first_quality_mask"]).content) as memory, memory.open() as dataset:
        assert dataset.read(1).tolist() == [[1, 1, 1, 0, 1], [1, 1, 1, 1, 1], [0, 0, 0, 0, 0]]
    with MemoryFile(_download(workbench, artifacts["second_quality_mask"]).content) as memory, memory.open() as dataset:
        values = dataset.read(1)
        assert (int(values.sum()), values[2, 14], values[5, 2], values[5, 5], values[5, 8]) == (154, 0, 1, 0, 0)
    with MemoryFile(_download(workbench, artifacts["first_ndvi"]).content) as memory, memory.open() as dataset:
        assert dataset.transform == FIRST_GRID
        values = dataset.read(1, masked=True)
        assert values[0, 0] == 0 and values[0, 1] == pytest.approx(11 / 17, rel=0, abs=1e-15)
        assert values.mask[0, 3] and values[1, 3] == 0
    with MemoryFile(_download(workbench, artifacts["second_ndvi"]).content) as memory, memory.open() as dataset:
        assert dataset.transform == SECOND_GRID
        values = dataset.read(1, masked=True)
        assert values[2, 2] == 0.5 and values[2, 8] == 0 and values[5, 11] == 0.5
        assert all(values.mask[index] for index in ((2, 14), (5, 2), (5, 5), (5, 8)))
        diagnostics = json.loads(dataset.tags(ns=NDVI_DOMAIN)["SUMMARY"])
        assert diagnostics["denominator_zero_pixels"] == 1 and diagnostics["out_of_range_pixels"] == 0

    _assert_registered_change(workbench, artifacts)
    area = _download(workbench, artifacts["area_statistics"]).json()
    assert (area["source_crs"], area["area_crs"], area["area_unit"]) == (CRS, AREA_CRS, "square_meter")
    assert (area["direction"], area["threshold"], area["cell_area_method"]) == ("second_minus_first", 0.25, "projected_cell_boundary")
    assert (area["valid_pixel_count"], area["nodata_pixel_count"], area["unselected_pixel_count"]) == (4, 11, 0)
    expected_areas = [_ellipsoid_area(0, 2), _ellipsoid_area(0, 1) + _ellipsoid_area(1, 4), _ellipsoid_area(0, 0)]
    assert [(item["class_name"], item["pixel_count"]) for item in area["classes"]] == [
        ("decrease", 1), ("stable", 2), ("increase", 1),
    ]
    assert [item["area_m2"] for item in area["classes"]] == pytest.approx(expected_areas, rel=0, abs=1e-5)
    assert area["total_area_m2"] == pytest.approx(math.fsum(expected_areas), rel=0, abs=2e-5)
    assert all(fingerprint_file(path) == fingerprints[role] for role, (path, _) in paths.items())


def test_ndvi_outlier_is_reported_without_alignment_pollution(workbench, tmp_path):
    plan, _ = _draft(workbench, write_mixed_products(tmp_path / "sources", anomaly=True))
    assert plan["issues"] == []
    artifacts, report, _ = _run(workbench, tmp_path, plan, check_status="WARNING")
    warning = report["report"]["rules"]["ndvi_values_in_range"]
    assert warning["passed"] is False and warning["severity"] == "warning"
    assert warning["observed"] == {"first_ndvi": 0, "second_ndvi": 1}
    with MemoryFile(_download(workbench, artifacts["second_ndvi"]).content) as memory, memory.open() as dataset:
        values = dataset.read(1, masked=True)
        assert values[5, 5] == pytest.approx(5, abs=1e-14)
        diagnostics = json.loads(dataset.tags(ns=NDVI_DOMAIN)["SUMMARY"])
        assert diagnostics["out_of_range_pixels"] == 1
        assert diagnostics["first_out_of_range"] == {"row": 5, "column": 5, "value": pytest.approx(5, abs=1e-14)}
    _assert_registered_change(workbench, artifacts)


@pytest.mark.parametrize(("role", "product_key"), [
    ("first_product", "landsat-8-oli-tirs/L2SP/03"),
    ("second_product", "sentinel-2-msi/L2A/05.12"),
])
def test_t12_unconfigured_product_version_is_blocked_before_any_step(workbench, tmp_path, role, product_key):
    plan, _ = _draft(workbench, write_mixed_products(tmp_path / "sources", unknown_role=role))
    issue, = [issue for issue in plan["issues"] if issue["code"] == "SENSOR_CONFIG_NOT_FOUND"]
    assert issue["field"] == f"inputs.{role}" and product_key in issue["message"]
    endpoint = f"/api/plans/{plan['id']}"
    version = {"plan_version": plan["version"]}
    problem = _post(workbench, endpoint + "/confirm", version, status=422)["detail"]
    assert issue in problem["issues"]
    blocked = _post(workbench, endpoint + "/runs", version, status=409)
    assert blocked["detail"]["code"] == "PLAN_NOT_CONFIRMED" and "run_id" not in blocked
    assert _get(workbench, f"/api/tasks/{plan['task_id']}").json()["runs"] == []
    assert not list((tmp_path / "workspace").rglob("request.json"))
