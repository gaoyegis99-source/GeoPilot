import csv
import io
import json
import os
import shutil
import time
from pathlib import Path

import pyogrio
import pytest
from fastapi.testclient import TestClient

from geopilot.api.server import create_local_app
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.resolver import resolve_asset
from geopilot.domain.data_asset import DataAssetRef
from geopilot.domain.step_io import StepRequest, StepResult


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "vector"
ROAD = {"id": "road-buffer-statistics", "version": 2}
ATTRIBUTE = {"id": "attribute-filter-export", "version": 1}
ROAD_STEPS = [
    "expand_analysis_extent", "select_roads_from_expanded_extent", "choose_measurement_method", "create_buffer",
    "apply_spatial_relation", "clip_back_to_analysis_extent", "aggregate_by_zone",
]
ATTRIBUTE_STEPS = ["filter_features"]
ROAD_OPTIONS = {
    "distance_m": {"value": 0.5, "unit": "kilometer"}, "distance_method": "local_projected",
    "distance_crs_ref": "EPSG:32650", "spatial_relation": "intersects", "boundary_assignment": "centroid",
    "boundary_tie": "unassigned", "statistic": "count", "output_scope": "clipped_to_analysis_extent",
}


@pytest.fixture
def project(tmp_path):
    qgis = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if qgis is None:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS to run the real workbench reproduction")
    executable = Path(qgis).expanduser().resolve(strict=True)
    assert executable.is_file() and os.access(executable, os.X_OK), executable
    source = tmp_path / "road-buffer.gpkg"
    shutil.copyfile(GOLDEN / source.name, source)
    database = tmp_path / "project.sqlite"
    workspace = tmp_path / "workspace"
    with TestClient(create_local_app(database, workspace, qgis_process=executable)) as client:
        yield client, source, workspace


def _get(client, path, *, params=None):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response


def _post(client, path, document, *, status=200):
    response = client.post(path, json=document)
    assert response.status_code == status, response.text
    return response.json()


def _prepare(client, method, inputs, options, goal, keys):
    task = _post(client, "/api/tasks", {"goal": {"summary": goal}}, status=201)["task_id"]
    task_path = f"/api/tasks/{task}"
    context = _post(client, f"{task_path}/context", {"method": method, "inputs": inputs})
    assert context["task_id"] == task and context["method"] == method
    assert {role: entry["asset"] for role, entry in context["roles"].items()} == inputs
    assert all(entry["status"] == "READY" for entry in context["roles"].values())
    draft = _post(client, f"{task_path}/plans", {"method": method, "inputs": inputs, "critical_options": options})
    assert draft["status"] == "draft" and draft["issues"] == []
    assert draft["inputs"] == inputs and draft["critical_options"] == options
    path = f"/api/plans/{draft['id']}"
    confirmed = _post(client, f"{path}/confirm", {"plan_version": draft["version"]})
    assert confirmed["status"] == "confirmed" and confirmed["method"] == method
    assert [step["key"] for step in confirmed["steps"]] == keys
    assert _get(client, path, params={"version": draft["version"]}).json() == confirmed
    assert _get(client, task_path).json()["runs"] == []
    return confirmed


def _start_and_verify(client, workspace, plan, asset_fingerprint, roles):
    run_id = _post(client, f"/api/plans/{plan['id']}/runs", {"plan_version": plan["version"]}, status=202)["run_id"]
    endpoint = f"/api/runs/{run_id}"
    directory = workspace / "tasks" / plan["task_id"] / "runs" / run_id
    deadline = time.monotonic() + 90
    while True:
        status = _get(client, endpoint).json()
        if status["execution_status"] in {"FAILED", "CANCELLED"} or status["current_report_id"] or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    assert (status["execution_status"], status["check_status"]) == ("SUCCEEDED", "PASSED"), (status, directory)
    assert _get(client, endpoint + "/origin").json() == {
        "run_id": run_id, "task_id": plan["task_id"], "plan_id": plan["id"], "plan_version": plan["version"],
    }
    snapshot = json.loads((directory / "request.json").read_text())
    assert snapshot["run_id"] == run_id and snapshot["plan"] == plan
    assert set(snapshot["asset_files"]) == set(plan["inputs"])
    for role, reference in snapshot["asset_files"].items():
        assert reference["layer"] == ("targets" if role == "features" else role)
        assert fingerprint_file(directory / reference["path"]).digest == asset_fingerprint

    steps = _get(client, endpoint + "/steps").json()["steps"]
    assert [item["step_key"] for item in steps] == [step["key"] for step in plan["steps"]]
    assert all(item["status"] == "SUCCEEDED" and item["log_available"] for item in steps)
    results = {}
    for step in plan["steps"]:
        key = step["key"]
        root = directory / "steps" / key
        request = StepRequest.model_validate_json((root / "request.json").read_text())
        result = StepResult.model_validate_json((root / "result.json").read_text())
        assert request.run_id == result.run_id == run_id and request.step_key == result.step_key == key
        assert request.tool.model_dump(mode="json") == step["tool"]
        assert request.parameters == step["parameters"] and result.exit_code == 0
        log = (root / "process.log").read_text()
        assert log == _get(client, endpoint + f"/steps/{key}/log").text
        commands = [line for line in log.splitlines() if line.startswith("$ ")]
        if request.tool.id.startswith("qgis-"):
            assert sum(" --json run " in line for line in commands) == 1 and result.environment["qgis_version"]
        else:
            assert sum(" --request " in line for line in commands) == 1 and result.environment["python"]
        results[key] = result

    report = _get(client, endpoint + "/checks").json()["current_report"]
    assert report["id"] == status["current_report_id"] and report["status"] == "PASSED"
    assert report["report"]["completed_rules"] == plan["check_rules"]
    assert set(report["report"]["phase_findings"]) == {"input", "process", "artifact"}
    assert json.loads((directory / "checks" / "report.json").read_text()) == report["report"]

    artifacts = {item["role"]: item for item in _get(client, endpoint + "/artifacts").json()["artifacts"]}
    manifest = json.loads((directory / "outputs" / "manifest.json").read_text())["artifacts"]
    assert set(artifacts) == set(manifest) == roles
    for role, item in artifacts.items():
        origin = item["source"]
        assert item["readable"] and item["run_id"] == run_id
        assert (origin["plan_id"], origin["plan_version"]) == (plan["id"], plan["version"])
        assert origin["inputs"] and all(plan["inputs"][key] == ref for key, ref in origin["inputs"].items())
        assert results[origin["step_key"]].outputs[origin["output_name"]].path == item["uri"]
        output = directory / item["uri"]
        assert manifest[role]["fingerprint"] == item["fingerprint"] == fingerprint_file(output).digest
        assert _get(client, f"/api/artifacts/{item['id']}/file").content == output.read_bytes()
    return directory, artifacts, results, report


def _read_vector(directory, artifact):
    return pyogrio.read_dataframe(directory / artifact["uri"], layer=artifact["layer"])


def test_real_workbench_replays_road_and_executes_independent_attribute_method(project, tmp_path):
    client, source, workspace = project
    before = fingerprint_file(source)
    expected = json.loads((GOLDEN / "expected.json").read_text())
    target_reference = pyogrio.read_dataframe(source, layer="targets").set_index("source_feature_id")
    methods = {(item["definition"]["id"], item["definition"]["version"]): item["available"]
               for item in _get(client, "/api/methods").json()["methods"]}
    assert methods[(ROAD["id"], ROAD["version"])] and methods[(ATTRIBUTE["id"], ATTRIBUTE["version"])]
    assets = {}
    for layer in ("analysis_extent", "roads", "targets", "zones"):
        row = _post(client, "/api/assets", {"path": str(source), "layer": layer})
        assert row["source"]["layer"] == layer
        assets[layer] = {key: row[key] for key in ("asset_id", "asset_version")}
        asset = resolve_asset(tmp_path / "project.sqlite", DataAssetRef.model_validate(assets[layer]))
        assert asset.fingerprint == before.digest and asset.source.layer == layer
    assert len({reference["asset_id"] for reference in assets.values()}) == 4

    road_plan = _prepare(client, ROAD, assets, ROAD_OPTIONS, "道路 500 米缓冲后的建筑分区计数", ROAD_STEPS)
    road_roles = {"buffer_extent", "selected_targets", "zone_statistics"}
    road_runs = [_start_and_verify(client, workspace, road_plan, before.digest, road_roles) for _ in range(2)]
    attribute_plan = _prepare(
        client, ATTRIBUTE, {"features": assets["targets"]}, {"field_name": "source_feature_id", "match_value": "building-west"},
        "筛选西区参考建筑", ATTRIBUTE_STEPS,
    )
    attribute_dir, attribute_files, _, attribute_report = _start_and_verify(
        client, workspace, attribute_plan, before.digest, {"filtered_features"},
    )
    assert len({directory for directory, _, _, _ in road_runs} | {attribute_dir}) == 3

    buffers = []
    csvs = []
    expected_relation = expected["relations"]["intersects"]
    for directory, files, results, _ in road_runs:
        reference = results["select_roads_from_expanded_extent"].outputs["roads"]
        candidates = pyogrio.read_dataframe(directory / reference.path, layer=reference.layer)
        assert sorted(candidates["source_feature_id"]) == expected["candidate_road_ids"]
        selected = _read_vector(directory, files["selected_targets"]).set_index("source_feature_id").sort_index()
        assert selected.crs.to_epsg() == 32650 and selected.index.tolist() == expected_relation["clipped_target_ids"]
        assert selected.geometry.geom_equals_exact(target_reference.loc[selected.index].geometry, tolerance=0).all()
        buffer = _read_vector(directory, files["buffer_extent"])
        assert buffer.crs.to_epsg() == 32650 and buffer.geometry.is_valid.all()
        buffers.append(buffer.geometry.union_all())
        csv_bytes = (directory / files["zone_statistics"]["uri"]).read_bytes()
        rows = list(csv.DictReader(io.StringIO(csv_bytes.decode())))
        counts = {row["zone_id"] or "unassigned": int(row["count"]) for row in rows}
        assert {**counts, "total": sum(counts.values())} == expected_relation["counts"]
        assert {row["zone_id"]: json.loads(row["source_feature_ids"]) for row in rows if row["zone_id"]} == expected_relation["zone_target_ids"]
        assert [json.loads(row["source_feature_ids"]) for row in rows if not row["zone_id"]] == [expected_relation["unassigned_target_ids"]]
        csvs.append(csv_bytes)
    tolerance = expected["main_road_buffer"]["absolute_tolerances"]
    assert csvs[0] == csvs[1]
    assert buffers[0].hausdorff_distance(buffers[1]) <= tolerance["boundary_hausdorff_m"]
    assert abs(buffers[0].area - buffers[1].area) <= tolerance["area_m2"]
    filtered = _read_vector(attribute_dir, attribute_files["filtered_features"])
    assert filtered.crs.to_epsg() == 32650 and filtered["source_feature_id"].tolist() == ["building-west"]
    assert (filtered.geometry.item().x, filtered.geometry.item().y) == (500750, 4401500)
    assert fingerprint_file(source) == before

    evidence = {
        "fixture": source.name, "fixture_fingerprint": before.digest,
        "road_runs": [directory.relative_to(tmp_path).as_posix() for directory, _, _, _ in road_runs],
        "road_plan": {key: road_plan[key] for key in ("id", "version", "method")},
        "road_report_ids": [report["id"] for _, _, _, report in road_runs],
        "road_candidate_ids": expected["candidate_road_ids"], "road_selected_ids": expected_relation["clipped_target_ids"],
        "counts": expected_relation["counts"], "road_csv_equal": True,
        "buffer_hausdorff_m": buffers[0].hausdorff_distance(buffers[1]),
        "buffer_area_difference_m2": abs(buffers[0].area - buffers[1].area),
        "attribute_run": attribute_dir.relative_to(tmp_path).as_posix(), "attribute_report_id": attribute_report["id"],
        "attribute_plan": {key: attribute_plan[key] for key in ("id", "version", "method")},
        "attribute_ids": filtered["source_feature_id"].tolist(), "attribute_xy": [500750, 4401500],
    }
    (tmp_path / "comparison.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
