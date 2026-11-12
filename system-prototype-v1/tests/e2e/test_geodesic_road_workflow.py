import csv
import json
import os
import shutil
import time
from math import radians
from pathlib import Path

import pyogrio
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box

from geopilot.api.server import create_local_app
from geopilot.application.run_checks import RUN_CHECKS, RunCheckContext
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepFileRef, StepRequest, StepResult
from geopilot.plans.models import AnalysisPlan
from tests.fixtures.geodesic.road_workflow import (
    DISTANCE_M,
    LOCAL_CRS,
    ORIGIN_LONGITUDE,
    ROLES,
    UTM_CRS,
    WGS84_SEMI_MAJOR_M,
    write_road_workflow_fixture,
)


METHOD = {"id": "road-buffer-statistics", "version": 3}
STEPS = [
    "expand_analysis_extent", "select_roads_from_expanded_extent", "choose_measurement_method", "create_buffer",
    "apply_spatial_relation", "clip_back_to_analysis_extent", "aggregate_by_zone",
]
GEODESIC_TOOLS = [
    "python-expand-wgs84-analysis-extent", "python-select-wgs84-roads", "python-prepare-wgs84-measurement-lines",
    "python-wgs84-geodesic-buffer",
]
ARTIFACTS = {"buffer_extent", "selected_targets", "zone_statistics"}
CANDIDATE_IDS = ["road-main", "road-near"]


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    return write_road_workflow_fixture(tmp_path_factory.mktemp("road-equatorial") / "roads.gpkg")


@pytest.fixture
def workbench(tmp_path):
    configured = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if configured is None:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS to exercise the real seven-step road workflow")
    executable = Path(configured).resolve(strict=True)
    workspace = tmp_path / "workspace"
    with TestClient(create_local_app(tmp_path / "project.sqlite", workspace, qgis_process=executable)) as client:
        yield client, workspace


def _get(client, endpoint):
    response = client.get(endpoint)
    assert response.status_code == 200, response.text
    return response


def _post(client, endpoint, data, *, status=200):
    response = client.post(endpoint, json=data)
    assert response.status_code == status, response.text
    return response.json()


def _confirm(client, source, distance_method, predicate, mixed):
    geodesic = distance_method == "geodesic"
    layers = {role: f"{role}_mercator" for role in ROLES}
    if geodesic:
        layers = {role: f"{role}_utm" if mixed and role != "roads" else role for role in ROLES}
    assets = {}
    for role, layer in layers.items():
        asset = _post(client, "/api/assets", {"path": str(source), "layer": layer})
        assets[role] = {key: asset[key] for key in ("asset_id", "asset_version")}
    task = _post(client, "/api/tasks", {"goal": {"summary": "Verify the 500 metre road influence"}}, status=201)
    options = {
        "distance_method": distance_method, "distance_m": {"value": 0.5, "unit": "kilometer"},
        "spatial_relation": predicate, "boundary_assignment": "centroid", "boundary_tie": "unassigned",
        "statistic": "count", "output_scope": "clipped_to_analysis_extent",
    }
    if not geodesic:
        options["distance_crs_ref"] = LOCAL_CRS
    draft = _post(client, f"/api/tasks/{task['task_id']}/plans", {
        "method": METHOD, "inputs": assets, "critical_options": options,
    })
    assert draft["status"] == "draft" and draft["issues"] == [], draft["issues"]
    plan = _post(client, f"/api/plans/{draft['id']}/confirm", {"plan_version": draft["version"]})
    assert plan["status"] == "confirmed" and plan["method"] == METHOD and plan["inputs"] == assets
    assert [step["key"] for step in plan["steps"]] == STEPS
    assert ("distance_crs_ref" in plan["critical_options"]) is not geodesic
    assert [step["tool"] for step in plan["steps"][:4]] == [
        {"id": tool, "version": 1} for tool in (
            GEODESIC_TOOLS if geodesic else
            ["qgis-expand-analysis-extent", "qgis-select-roads-by-location", "python-prepare-measurement-lines", "qgis-buffer"]
        )
    ]
    return plan, layers


def _run(client, workspace, plan, layers, source_fingerprint):
    run_id = _post(client, f"/api/plans/{plan['id']}/runs", {"plan_version": plan["version"]}, status=202)["run_id"]
    endpoint = f"/api/runs/{run_id}"
    workdir = workspace / "tasks" / plan["task_id"] / "runs" / run_id
    deadline = time.monotonic() + 90
    while True:
        state = _get(client, endpoint).json()
        if state["current_report_id"] or state["execution_status"] in {"FAILED", "CANCELLED"} or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "PASSED"), (state, workdir)
    snapshot = json.loads((workdir / "request.json").read_text())
    assert snapshot["plan"] == plan and snapshot["run_id"] == run_id
    assert {role: ref["layer"] for role, ref in snapshot["asset_files"].items()} == layers
    assert all(fingerprint_file(workdir / ref["path"]).digest == source_fingerprint
               for ref in snapshot["asset_files"].values())

    results = {}
    for step in plan["steps"]:
        key = step["key"]
        folder = workdir / "steps" / key
        request = StepRequest.model_validate_json((folder / "request.json").read_text())
        result = StepResult.model_validate_json((folder / "result.json").read_text())
        assert request.run_id == result.run_id == run_id and request.step_key == result.step_key == key
        assert request.tool.model_dump(mode="json") == step["tool"]
        assert request.parameters == step["parameters"] and result.exit_code == 0 and result.status == "SUCCEEDED"
        log = _get(client, endpoint + f"/steps/{key}/log").text
        assert log == (folder / "process.log").read_text()
        commands = [line for line in log.splitlines() if line.startswith("$ ")]
        marker = " --json run " if request.tool.id.startswith("qgis-") else " --request "
        assert sum(marker in command for command in commands) == 1
        results[key] = result

    report = _get(client, endpoint + "/checks").json()["current_report"]
    assert report["id"] == state["current_report_id"] and report["status"] == "PASSED"
    body = report["report"]
    assert body["completed_rules"] == plan["check_rules"]
    assert body == json.loads((workdir / "checks/report.json").read_text())
    assert all(item["passed"] for item in body["rules"].values())
    items = _get(client, endpoint + "/artifacts").json()["artifacts"]
    assert {item["role"] for item in items} == ARTIFACTS
    manifest = json.loads((workdir / "outputs/manifest.json").read_text())["artifacts"]
    assert set(manifest) == ARTIFACTS
    artifacts = {}
    for item in items:
        role, origin = item["role"], item["source"]
        reference = results[origin["step_key"]].outputs[origin["output_name"]]
        assert (origin["plan_id"], origin["plan_version"]) == (plan["id"], plan["version"])
        assert item["readable"] and item["uri"] == reference.path
        assert item["fingerprint"] == manifest[role]["fingerprint"] == fingerprint_file(workdir / reference.path).digest
        artifacts[role] = reference
    return workdir, artifacts, results, body


def _vector(workdir, output):
    return pyogrio.read_dataframe(workdir / output.path, layer=output.layer).set_index("source_feature_id")


def _assert_geometry(source, layers, distance_method, predicate, outcome):
    workdir, artifacts, results, report = outcome
    geodesic = distance_method == "geodesic"
    candidates = _vector(workdir, results[STEPS[1]].outputs["roads"]).sort_index()
    original_roads = pyogrio.read_dataframe(source, layer=layers["roads"]).set_index("source_feature_id")
    assert candidates.index.tolist() == CANDIDATE_IDS
    assert candidates.crs == original_roads.crs
    assert candidates.geometry.geom_equals_exact(original_roads.loc[CANDIDATE_IDS].geometry, 0).all()

    buffer = artifacts["buffer_extent"]
    layer = pyogrio.read_dataframe(workdir / buffer.path, layer=buffer.layer)
    geometry = layer.geometry.union_all()
    assert layer.crs.to_string() == ("EPSG:4326" if geodesic else LOCAL_CRS) and geometry.is_valid
    if geodesic:
        scope = results[STEPS[0]].outputs["analysis_extent"]
        prepared_extent = pyogrio.read_dataframe(workdir / scope.path, layer=scope.layer)
        assert prepared_extent.crs.to_epsg() == 4326
    else:
        prepared_extent = pyogrio.read_dataframe(source, layer=layers["analysis_extent"])
    extent = prepared_extent.geometry.union_all()
    assert geometry.difference(extent).area > 0
    original_targets = pyogrio.read_dataframe(source, layer=layers["targets"]).set_index("source_feature_id")
    cap = original_targets.loc["point-cap"].geometry
    if geodesic:
        original_targets = original_targets.to_crs(4326)
        cap = original_targets.loc["point-cap"].geometry
        cap_radius = radians(ORIGIN_LONGITUDE - geometry.bounds[0]) * WGS84_SEMI_MAJOR_M
        assert cap_radius == pytest.approx(DISTANCE_M, abs=0.05, rel=0)
        assert geometry.touches(cap)
        cap_selected = predicate == "intersects"
    else:
        main = original_roads.loc["road-main"].geometry
        assert main.bounds[0] - geometry.bounds[0] == pytest.approx(DISTANCE_M, abs=1e-6, rel=0)
        # A QGIS round cap vertex can have a tiny nonzero y from trigonometric rounding.
        assert geometry.boundary.distance(cap) < 1e-8
        cap_selected = geometry.covers(cap)
    assert geometry.contains(original_targets.loc["point-cap-inside"].geometry)
    assert geometry.disjoint(original_targets.loc["point-cap-outside"].geometry)

    west = ["point-cap-inside", "point-west"]
    if cap_selected:
        west.insert(0, "point-cap")
    members = {"zone-west": west, "zone-east": ["point-east", "point-near-road"], "": ["point-shared-zone"]}
    selected_ids = sorted(member for group in members.values() for member in group)
    related = _vector(workdir, results[STEPS[4]].outputs["targets"])
    assert sorted(related.index) == sorted([*selected_ids, "point-outside-aoi"])
    selected = _vector(workdir, artifacts["selected_targets"]).sort_index()
    assert selected.index.tolist() == selected_ids and selected.crs == original_targets.crs
    assert selected.geometry.geom_equals_exact(original_targets.loc[selected_ids].geometry, 0).all()
    assert all(extent.covers(point) for point in selected.geometry)
    preserved = _vector(workdir, results[STEPS[5]].outputs["original_targets"]).sort_index()
    assert preserved.equals(selected)

    csv_path = workdir / artifacts["zone_statistics"].path
    with csv_path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 3
    for row in rows:
        zone = row["zone_id"]
        assert row["assignment_status"] == ("assigned" if zone else "unassigned")
        assert json.loads(row["source_feature_ids"]) == members[zone] and int(row["count"]) == len(members[zone])

    observations = report["rules"]["measurement_policy_satisfied"]["observed"]
    assert observations["problems"] == {} and set(observations["steps"]) == {STEPS[0], STEPS[3]}
    for evidence in observations["steps"].values():
        assert evidence["crs"] == ("EPSG:4326" if geodesic else LOCAL_CRS)
        assert evidence["unit"] == "meter" and evidence["request_distance_m"] == DISTANCE_M
        if geodesic:
            assert evidence["ellipsoid"] == "WGS84" and evidence["engine_distance_m"] == DISTANCE_M
        else:
            assert evidence["qgis_distance_m"] == DISTANCE_M
    candidate_check = report["rules"]["outside_roads_within_influence_included"]["observed"]
    assert candidate_check["expected_ids"] == candidate_check["actual_ids"] == CANDIDATE_IDS
    assert candidate_check["missing_ids"] == candidate_check["unexpected_ids"] == []
    count_check = report["rules"]["detail_count_matches_summary"]["observed"]
    assert count_check["unassigned_count"] == 1 and count_check["summary_total_count"] == len(selected_ids)
    if geodesic:
        assert observations["steps"][STEPS[0]]["source_crs"] == (
            UTM_CRS if layers["analysis_extent"].endswith("_utm") else "EPSG:4326"
        )
        assert observations["steps"][STEPS[3]]["centerline_samples"] == 6
        assert candidate_check["expanded_extent_matches_reference"] is True
        assert candidate_check["outside_included_ids"] == ["road-near"]
        assert count_check["expected_zone_members"] == count_check["actual_zone_members"] == members
    return geometry, csv_path.read_bytes()


def _assert_measurement_and_scope_drift_are_detected(tmp_path, outcome, plan):
    original_workdir, artifacts, results, _ = outcome
    workdir = tmp_path / "altered-check-evidence"
    shutil.copytree(original_workdir, workdir)
    saved = json.loads((workdir / "request.json").read_text())
    context = RunCheckContext(
        plan=AnalysisPlan.model_validate(plan), workdir=workdir,
        asset_files={key: StepFileRef.model_validate(ref) for key, ref in saved["asset_files"].items()},
        steps=results, artifacts=artifacts,
    )
    log = workdir / results[STEPS[3]].log_path
    prefix = "geodesic_measurement: "
    lines = log.read_text().splitlines()
    index, = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    changed = json.loads(lines[index].removeprefix(prefix))
    changed["distance_m"] = 499
    lines[index] = prefix + json.dumps(changed)
    log.write_text("\n".join(lines) + "\n")
    measurement = RUN_CHECKS["measurement_policy_satisfied"](context)
    assert not measurement.passed and "reported distance 499" in measurement.observed["problems"][STEPS[3]]
    log.write_bytes((original_workdir / results[STEPS[3]].log_path).read_bytes())

    buffer = artifacts["buffer_extent"]
    buffer_layer = pyogrio.read_dataframe(workdir / buffer.path, layer=buffer.layer)
    shifted = buffer_layer.set_geometry([box(4, 0, 4.001, 0.001)])
    pyogrio.write_dataframe(shifted, workdir / buffer.path, layer=buffer.layer, driver="GPKG")
    measurement = RUN_CHECKS["measurement_policy_satisfied"](context)
    assert not measurement.passed and "centreline samples lie outside" in measurement.observed["problems"][STEPS[3]]

    expand_outputs = results[STEPS[0]].outputs
    extent, expanded = expand_outputs["analysis_extent"], expand_outputs["expanded_extent"]
    scope = pyogrio.read_dataframe(workdir / extent.path, layer=extent.layer)
    pyogrio.write_dataframe(scope, workdir / expanded.path, layer=expanded.layer, driver="GPKG")
    coverage = RUN_CHECKS["outside_roads_within_influence_included"](context)
    assert not coverage.passed and coverage.observed["expanded_extent_matches_reference"] is False
    assert coverage.observed["expected_ids"] == CANDIDATE_IDS
    assert all(record.status == "SUCCEEDED" for record in results.values())


@pytest.mark.parametrize(("distance_method", "predicate", "mixed"), [
    pytest.param("geodesic", "intersects", False, id="geodesic-intersects"),
    pytest.param("geodesic", "within", False, id="geodesic-within"),
    pytest.param("geodesic", "intersects", True, id="geodesic-mixed-input-crs"),
    pytest.param("local_projected", "intersects", False, id="local-projected-intersects"),
])
def test_confirmed_seven_step_road_workflow(workbench, source, tmp_path, distance_method, predicate, mixed):
    client, workspace = workbench
    original = fingerprint_file(source)
    entry, = [item for item in _get(client, "/api/methods").json()["methods"] if item["definition"]["id"] == METHOD["id"]
              and item["definition"]["version"] == METHOD["version"]]
    assert entry["available"], entry["issues"]
    plan, layers = _confirm(client, source, distance_method, predicate, mixed)
    first = _run(client, workspace, plan, layers, original.digest)
    geometry, summary = _assert_geometry(source, layers, distance_method, predicate, first)
    if (distance_method, predicate, mixed) == ("geodesic", "intersects", False):
        repeat = _run(client, workspace, plan, layers, original.digest)
        repeated_geometry, repeated_summary = _assert_geometry(source, layers, distance_method, predicate, repeat)
        assert repeat[0] != first[0] and geometry.equals_exact(repeated_geometry, 0) and summary == repeated_summary
        _assert_measurement_and_scope_drift_are_detected(tmp_path, first, plan)
    assert fingerprint_file(source) == original


def test_confirmed_geodesic_workflow_publishes_empty_outputs_when_only_far_roads_exist(workbench, source, tmp_path):
    client, workspace = workbench
    package = tmp_path / "far-road.gpkg"
    shutil.copyfile(source, package)
    roads = pyogrio.read_dataframe(package, layer="roads")
    pyogrio.write_dataframe(roads.loc[roads["source_feature_id"] == "road-far"], package, layer="roads", driver="GPKG")
    original = fingerprint_file(package)
    plan, layers = _confirm(client, package, "geodesic", "intersects", False)
    workdir, artifacts, results, report = _run(client, workspace, plan, layers, original.digest)

    outputs = [results[STEPS[1]].outputs["roads"], results[STEPS[2]].outputs["roads"], artifacts["buffer_extent"],
               results[STEPS[4]].outputs["targets"], artifacts["selected_targets"], results[STEPS[5]].outputs["original_targets"]]
    for output in outputs:
        reopened = pyogrio.read_dataframe(workdir / output.path, layer=output.layer)
        assert reopened.empty and reopened.crs.to_epsg() == 4326
    with (workdir / artifacts["zone_statistics"].path).open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert {row["zone_id"]: (int(row["count"]), json.loads(row["source_feature_ids"])) for row in rows} == {
        "zone-east": (0, []), "zone-west": (0, []), "": (0, []),
    }
    rules = report["rules"]
    coverage = rules["measurement_policy_satisfied"]["observed"]["steps"][STEPS[3]]
    assert coverage["road_count"] == coverage["centerline_samples"] == 0
    candidate = rules["outside_roads_within_influence_included"]["observed"]
    assert candidate["expected_ids"] == candidate["actual_ids"] == []
    assert rules["detail_count_matches_summary"]["observed"]["summary_total_count"] == 0
    assert fingerprint_file(package) == original
