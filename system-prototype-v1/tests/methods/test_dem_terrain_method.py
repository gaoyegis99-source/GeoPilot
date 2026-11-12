import time
from pathlib import Path
from threading import Event

import geopandas as gpd
import pyogrio
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.io import MemoryFile
from shapely.geometry import box

from geopilot.api.server import create_local_app
from geopilot.checks.raster_checks import DEM_ARTIFACT_RULES


ROOT = Path(__file__).resolve().parents[2]
DEM = ROOT / "tests/fixtures/raster/dem-plane.tif"
METHOD = {"id": "dem-terrain-analysis", "version": 1}
STEPS = [
    "expand_extent_by_algorithm_neighborhood", "read_dem_for_expanded_extent",
    "check_horizontal_vertical_units", "calculate_slope_or_aspect",
    "clip_back_to_analysis_extent", "extract_by_threshold", "calculate_area_with_equal_area_projection",
]
OPTIONS = {
    "terrain_output": "slope", "slope_unit": "degree", "area_crs": "EPSG:6933",
    "threshold": {"intervals": [
        {"class_id": 7, "lower": 45, "upper": 45, "include_lower": True, "include_upper": True},
    ]},
}


@pytest.fixture
def workbench(tmp_path):
    aoi = tmp_path / "analysis-extent.gpkg"
    frame = gpd.GeoDataFrame(geometry=[box(500020, 4400020, 500050, 4400050)], crs="EPSG:32650")
    pyogrio.write_dataframe(frame, aoi, layer="analysis_extent", driver="GPKG")
    app = create_local_app(tmp_path / "project.sqlite", tmp_path / "workspace")
    with TestClient(app) as client:
        yield client, aoi


def _post(client, path, payload, *, status=200):
    response = client.post(path, json=payload)
    assert response.status_code == status, response.text
    return response.json()


def _asset(client, path, layer=None):
    asset = _post(client, "/api/assets", {"path": str(path), "layer": layer})
    return {key: asset[key] for key in ("asset_id", "asset_version")}


def _draft(workbench, *, source=DEM, options=None):
    client, aoi = workbench
    refs = {"analysis_extent": _asset(client, aoi, "analysis_extent"), "dem": _asset(client, source)}
    goal = {"summary": "分析参考 DEM 的坡度和等积面积"}
    task = _post(client, "/api/tasks", {"goal": goal}, status=201)["task_id"]
    request = {"method": METHOD, "inputs": refs, "critical_options": options or OPTIONS}
    return _post(client, f"/api/tasks/{task}/plans", request), request


@pytest.mark.parametrize(("output", "angle", "interrupt"), [
    ("slope", 45, False), ("aspect", 270, False), ("slope", 45, True),
])
def test_real_seven_step_terrain_method_derives_ratio_and_registers_results(
    workbench, output, angle, interrupt, monkeypatch,
):
    client, _ = workbench
    entered, resume = Event(), Event()
    rule_name = "dem_covers_expanded_extent"
    if interrupt:
        check = DEM_ARTIFACT_RULES[rule_name]

        def pause_first_check(context):
            findings = check(context)
            entered.set()
            assert resume.wait(20)
            return findings

        monkeypatch.setitem(DEM_ARTIFACT_RULES, rule_name, pause_first_check)
    entry, = [item for item in client.get("/api/methods").json()["methods"] if item["definition"]["id"] == METHOD["id"]]
    assert entry["available"] is True and entry["derived_options"] == ["z_factor"]
    interval = {**OPTIONS["threshold"]["intervals"][0], "lower": angle, "upper": angle}
    draft, request = _draft(workbench, options={
        **OPTIONS, "terrain_output": output, "z_factor": 999, "threshold": {"intervals": [interval]},
    })
    assert draft["issues"] == [] and draft["critical_options"]["z_factor"] == 1
    assert [step["key"] for step in draft["steps"]] == STEPS
    assert draft["inputs"] == request["inputs"]
    plan_endpoint = f"/api/plans/{draft['id']}"
    _post(client, plan_endpoint + "/confirm", {"plan_version": 1})
    assert client.get(f"/api/tasks/{draft['task_id']}").json()["runs"] == []
    run_id = _post(client, plan_endpoint + "/runs", {"plan_version": 1}, status=202)["run_id"]
    run_endpoint = f"/api/runs/{run_id}"
    if interrupt:
        try:
            assert entered.wait(90), client.get(run_endpoint).text
            _post(client, run_endpoint + "/cancel", {}, status=202)
        finally:
            resume.set()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        state = client.get(run_endpoint).json()
        if state["execution_status"] in {"FAILED", "CANCELLED"} or state["current_report_id"]:
            break
        time.sleep(0.1)
    expected_status = "NOT_CHECKED" if interrupt else "PASSED"
    assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", expected_status), state
    steps = client.get(run_endpoint + "/steps").json()["steps"]
    assert [step["step_key"] for step in steps] == STEPS
    assert all(step["status"] == "SUCCEEDED" for step in steps)
    results = {item["role"]: item for item in client.get(run_endpoint + "/artifacts").json()["artifacts"]}
    assert set(results) == {"terrain_raster", "threshold_regions", "area_statistics"}
    metadata = results["terrain_raster"]["metadata"]
    assert (metadata["width"], metadata["height"]) == (3, 3)
    assert metadata["transform"] == [10, 0, 500020, 0, -10, 4400050]
    assert metadata["crs"] == "EPSG:32650" and metadata["nodata"] == [-9999]
    band, = metadata["band_statistics"]
    assert band["valid_pixel_count"] == 9 and band["masked_pixel_count"] == 0
    assert (band["minimum"], band["maximum"], band["mean"]) == (angle, angle, angle)
    assert all(item["source"]["plan_version"] == 1 and item["source"]["inputs"] for item in results.values())
    raster_file = client.get(f"/api/artifacts/{results['terrain_raster']['id']}/file")
    assert raster_file.status_code == 200
    with MemoryFile(raster_file.content) as memory, memory.open() as raster:
        assert raster.read(1, masked=True).compressed().tolist() == [angle] * 9
    stats = client.get(f"/api/artifacts/{results['area_statistics']['id']}/file").json()
    assert stats["selected_pixel_count"] == 9 and stats["classes"][0]["class_id"] == 7
    assert stats["area_crs"] == "EPSG:6933" and stats["total_area_m2"] == pytest.approx(900.7204322, abs=1e-5)
    checks_endpoint = run_endpoint + "/checks"
    first = client.get(checks_endpoint).json()["current_report"]
    rules = {rule_name, "units_resolved", "border_pixels_match_reference"}
    if not interrupt:
        assert rules <= set(first["report"]["completed_rules"])
        assert all(first["report"]["rules"][rule]["passed"] for rule in rules)
        return
    assert first["report"]["interruption"]["code"] == "CHECK_CANCELLED"
    queued = _post(client, checks_endpoint, {}, status=202)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = client.get(checks_endpoint).json()["current_report"]
        if current["id"] == queued["report_id"]:
            break
        time.sleep(0.1)
    assert current["id"] == queued["report_id"] != first["id"]
    assert current["status"] == "PASSED"
    assert client.get(run_endpoint + "/steps").json()["steps"] == steps
    assert all(current["report"]["rules"][rule]["passed"] for rule in rules)
    assert client.get(run_endpoint + "/artifacts").json()["artifacts"] == list(results.values())


def test_invalid_equal_area_target_is_rejected_before_run(workbench):
    client, _ = workbench
    draft, _ = _draft(workbench, options={**OPTIONS, "area_crs": "EPSG:3857"})
    response = _post(client, f"/api/plans/{draft['id']}/confirm", {"plan_version": 1}, status=422)
    assert any(issue["field"] == "critical_options.area_crs" for issue in response["detail"]["issues"])
    assert client.get(f"/api/tasks/{draft['task_id']}").json()["runs"] == []


def test_missing_vertical_unit_can_be_resolved_by_an_explicit_choice(workbench, tmp_path):
    client, _ = workbench
    unlabelled = tmp_path / "unlabelled-dem.tif"
    with rasterio.open(DEM) as original:
        with rasterio.open(unlabelled, "w", **original.profile) as target:
            target.write(original.read())
    draft, request = _draft(workbench, source=unlabelled)
    assert any(issue["code"] == "DEM_VERTICAL_UNIT_REQUIRED" for issue in draft["issues"])
    revised = _post(client, f"/api/tasks/{draft['task_id']}/plans", {
        **request, "base_plan_id": draft["id"], "base_plan_version": draft["version"],
        "critical_options": {**OPTIONS, "vertical_unit": "foot"},
    })
    assert revised["version"] == 2 and revised["issues"] == []
    assert revised["critical_options"]["z_factor"] == pytest.approx(0.3048)
