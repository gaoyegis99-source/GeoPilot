import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pyogrio
import pytest
from fastapi.testclient import TestClient
from geopandas import GeoDataFrame
from shapely.geometry import LineString, Point

from geopilot.api.server import create_local_app
from geopilot.domain.step_io import ExecutionStatus
from geopilot.gis.tools.road_network_shortest_path import compute_route
from geopilot.plans.models import PlanStatus
from geopilot.runs.models import CheckStatus


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/methods/method_cli.py"
FIXTURE = ROOT / "tests/method_dev/fixtures/road-network-shortest-path"
MANIFEST = FIXTURE / "manifest.json"
METHOD = {"id": "road-network-shortest-path", "version": 1}
TOOL = {"id": "python-road-network-shortest-path", "version": 1}
CRS = "EPSG:32650"
OPTIONS = {"target_crs": CRS, "snap_tolerance_m": 5}


def file_state():
    return {path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in FIXTURE.rglob("*") if path.is_file()}


def write_evidence(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def invoke(*args):
    command = [sys.executable, str(CLI), *map(str, args)]
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=150, check=False)
    elapsed = time.monotonic() - started
    assert result.returncode == 0, (result.stdout, result.stderr)
    response = json.loads(result.stdout)
    assert response["schema_version"] == 1 and response["status"] == "ok" and response["errors"] == []
    return response, {"argv": command, "exit": result.returncode, "elapsed_seconds": round(elapsed, 6)}


def test_lint_and_isolated_reference_cases(tmp_path):
    before = file_state()
    lint, lint_time = invoke("lint", "--method", ROOT / "config/methods/road-network-shortest-path.v1.yaml")
    assert [(method["id"], method["version"]) for method in lint["data"]["methods"]] == [
        (METHOD["id"], METHOD["version"]),
    ]
    positive, positive_time = invoke("trial", "--package", MANIFEST, "--work-dir", tmp_path / "positive")
    report = positive["data"]["report"]
    assert report["verdict"] == "positive_match" and report["tools"] == [TOOL]
    assert report["actual"]["run"]["execution_status"] == ExecutionStatus.SUCCEEDED.value
    assert report["actual"]["run"]["check_status"] == CheckStatus.PASSED.value
    assert report["actual"]["workers_stopped"] is True and report["source_integrity"]["status"] == "PASS"
    assert report["reference"]["status"] == "PASS"
    outputs = report["reference"]["outputs"]
    assert outputs["route_edges"]["checks"] == {"feature_count": 2, "crs": CRS}
    assert outputs["route_summary"]["status"] == "PASS"
    run = tmp_path / "positive" / report["actual"]["run_dir"]
    edges = report["actual"]["artifacts"]["route_edges"]
    route = pyogrio.read_dataframe(run / edges["path"], layer=edges["layer"]).sort_values("sequence")
    assert list(route["source_feature_id"]) == ["west", "east"]
    assert list(route["distance_m"]) == [30, 30]
    assert list(route.geometry.iloc[1].coords) == [(500030, 3500000), (500060, 3500000)]
    summary = report["actual"]["artifacts"]["route_summary"]
    assert json.loads((run / summary["path"]).read_bytes()) == json.loads(
        (FIXTURE / "expected/route-summary.json").read_bytes(),
    )["value"]

    negative, negative_time = invoke(
        "trial", "--package", MANIFEST, "--work-dir", tmp_path / "negative", "--case", "web-mercator-start",
    )
    report = negative["data"]["report"]
    assert report["verdict"] == "expected_failure_matched"
    assert report["actual"]["phase"] == "draft" and report["actual"]["run_count"] == 0
    assert any(
        (issue["code"], issue["field"]) == ("MEASUREMENT_POLICY_REJECTED", "inputs.start")
        for issue in report["actual"]["issues"]
    )
    assert file_state() == before
    write_evidence(tmp_path / "lint.json", lint)
    write_evidence(tmp_path / "cli-timing.json", {"lint": lint_time, "positive": positive_time, "negative": negative_time})


def _response(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def _run_workbench(client, source_paths):
    entry, = [item for item in _response(client.get("/api/methods"))["methods"]
              if item["definition"]["id"] == METHOD["id"]]
    assert entry["available"] is True and entry["definition"]["version"] == METHOD["version"]
    assets = {}
    for role, path in source_paths.items():
        asset = _response(client.post("/api/assets", json={"path": str(path)}))
        assets[role] = {key: asset[key] for key in ("asset_id", "asset_version")}
    request = {"method": METHOD, "inputs": assets, "critical_options": OPTIONS}
    goal = {"summary": "Find the shortest route on the fixture road network"}
    preview = _response(client.post("/api/tasks/context-preview", json={**request, "goal": goal}))
    assert all(value["status"] == "READY" for value in preview["roles"].values())
    task_id = _response(client.post("/api/tasks", json={"goal": goal}), 201)["task_id"]
    draft = _response(client.post(f"/api/tasks/{task_id}/plans", json=request))
    assert draft["status"] == PlanStatus.DRAFT.value and draft["issues"] == []
    assert [step["tool"] for step in draft["steps"]] == [TOOL]
    plan = f"/api/plans/{draft['id']}"
    version = {"plan_version": draft["version"]}
    confirmed = _response(client.post(plan + "/confirm", json=version))
    assert confirmed["status"] == PlanStatus.CONFIRMED.value
    assert _response(client.get(f"/api/tasks/{task_id}"))["runs"] == []
    run_id = _response(client.post(plan + "/runs", json=version), 202)["run_id"]
    endpoint = f"/api/runs/{run_id}"
    deadline = time.monotonic() + 90
    while True:
        state = _response(client.get(endpoint))
        if state["current_report_id"] is not None or state["execution_status"] in {
            ExecutionStatus.FAILED.value, ExecutionStatus.CANCELLED.value,
        }:
            break
        assert time.monotonic() < deadline, state
        time.sleep(0.1)
    return endpoint, state, task_id, confirmed


def test_workbench_confirms_runs_and_checks_the_route(tmp_path):
    app = create_local_app(tmp_path / "project.sqlite", tmp_path / "workspace", qgis_process=tmp_path / "unused-qgis")
    sources = {role: FIXTURE / "inputs" / f"{role}.geojson" for role in ("network", "start", "end")}
    with TestClient(app) as client:
        endpoint, state, task_id, plan = _run_workbench(client, sources)
        assert (state["execution_status"], state["check_status"]) == (
            ExecutionStatus.SUCCEEDED.value, CheckStatus.PASSED.value,
        )
        checks = _response(client.get(endpoint + "/checks"))["current_report"]
        findings = checks["report"]["phase_findings"]
        assert any(item["rule"] == "projected_crs_suitable_for_extent" and item["passed"] for item in findings["input"])
        assert any(item["rule"] == "output_crs_expected" and item["passed"] for item in findings["artifact"])
        artifacts = _response(client.get(endpoint + "/artifacts"))["artifacts"]
        assert {item["role"] for item in artifacts if item["readable"]} == {"route_edges", "route_summary"}
        assert next(item for item in artifacts if item["role"] == "route_edges")["metadata"]["crs"] == CRS
        summary = next(item for item in artifacts if item["role"] == "route_summary")
        assert _response(client.get(f"/api/artifacts/{summary['id']}/file"))["total_distance_m"] == 60
    write_evidence(tmp_path / "workbench.json", {"task_id": task_id, "plan": plan, "run": state,
                                                "checks": checks, "artifacts": artifacts})


def _xy(east, north):
    return 500000 + east, 3500000 + north


def _lines(edges, *, crs=CRS):
    return GeoDataFrame(
        {"source_feature_id": list(edges)},
        geometry=[LineString([_xy(*node) for node in vertices]) for vertices in edges.values()],
        crs=crs,
    )


def _points(*positions, crs=CRS):
    return GeoDataFrame(geometry=[Point(_xy(*position)) for position in positions], crs=crs)


def _compute(edges, *, start=(0, 0), end=(60, 0), tolerance=0):
    return compute_route(edges, _points(start), _points(end), target_crs=CRS, snap_tolerance_m=tolerance)


def test_workbench_preserves_disconnected_failure_without_route_artifacts(tmp_path):
    network = tmp_path / "disconnected.gpkg"
    pyogrio.write_dataframe(
        _lines({"west": [(0, 0), (15, 0)], "east": [(60, 0), (75, 0)]}), network, layer="roads", driver="GPKG",
    )
    workspace = tmp_path / "workspace"
    app = create_local_app(tmp_path / "project.sqlite", workspace, qgis_process=tmp_path / "unused-qgis")
    sources = {"network": network, **{role: FIXTURE / "inputs" / f"{role}.geojson" for role in ("start", "end")}}
    with TestClient(app) as client:
        endpoint, state, task_id, _ = _run_workbench(client, sources)
        assert (state["execution_status"], state["check_status"]) == (
            ExecutionStatus.FAILED.value, CheckStatus.NOT_CHECKED.value,
        )
        assert state["error"]["code"] == "PROCESS_FAILED"
        evidence = _response(client.get(endpoint + "/artifacts"))
        assert evidence["artifacts"] == []
    run_dir = workspace / "tasks" / task_id / "runs" / state["run_id"]
    log = run_dir / "steps/solve_shortest_route/process.log"
    assert "The snapped start and end nodes are disconnected" in log.read_text(encoding="utf-8")
    write_evidence(tmp_path / "disconnected.json", {"run": state, "artifacts": evidence, "process_log": str(log)})


def test_same_cost_routes_use_fixed_tie_order_and_full_line_lengths():
    graph = _lines({
        "north-1": [(0, 0), (30, 40)], "north-2": [(30, 40), (60, 0)],
        "south-1": [(0, 0), (30, -40)], "south-2": [(30, -40), (60, 0)],
    })
    for source in (graph, graph.iloc[::-1]):
        _, summary = _compute(source)
        assert summary["edge_ids"] == ["south-1", "south-2"] and summary["total_distance_m"] == 100
    _, summary = _compute(_lines({"bent": [(0, 0), (0, 80), (60, 80), (60, 0)]}))
    assert summary["total_distance_m"] == 220


@pytest.mark.parametrize("other", [[(30, -10), (30, 10)], [(30, 0), (60, 0)], [(30, 0), (30, 30)]])
def test_refuses_crossings_overlaps_and_unnoded_junctions(other):
    with pytest.raises(ValueError, match="intersect away from shared endpoints"):
        _compute(_lines({"main": [(0, 0), (60, 0)], "other": other}))


@pytest.mark.parametrize(("start", "end", "tolerance", "message"), [
    ((-3, 4), (60, 0), 4.99, "outside the snap tolerance"),
    ((30, 0), (60, 0), 30, "multiple equally near network nodes"),
    ((-3, 4), (0, 0), 5, "same node"),
])
def test_refuses_out_of_range_ambiguous_and_same_node_snaps(start, end, tolerance, message):
    with pytest.raises(ValueError, match=message):
        _compute(_lines({"main": [(0, 0), (60, 0)]}), start=start, end=end, tolerance=tolerance)


@pytest.mark.parametrize(("crs", "message"), [
    ("EPSG:4326", "Geographic CRS"),
    ("EPSG:3857", "Web Mercator"),
    ("EPSG:2263", "requires metre axes"),
])
def test_refuses_angular_web_mercator_and_nonmetre_crs(crs, message):
    with pytest.raises(ValueError, match=message):
        compute_route(_lines({"main": [(0, 0), (60, 0)]}, crs=crs), _points((0, 0), crs=crs),
                      _points((60, 0), crs=crs), target_crs=crs, snap_tolerance_m=0)


def test_requires_one_point_an_explicit_common_crs_and_two_dimensional_lines():
    network = _lines({"main": [(0, 0), (60, 0)]})
    start, end = _points((0, 0)), _points((60, 0))
    with pytest.raises(ValueError, match="exactly one point"):
        compute_route(network, _points((0, 0), (1, 0)), end, **OPTIONS)
    with pytest.raises(ValueError, match="match the confirmed target_crs"):
        compute_route(network, start, end, target_crs="EPSG:32651", snap_tolerance_m=0)
    start.crs = None
    with pytest.raises(ValueError, match="inputs.start.crs must match"):
        compute_route(network, start, end, **OPTIONS)
    start.crs = CRS
    network.geometry = [LineString([(500000, 3500000, 2), (500060, 3500000, 3)])]
    with pytest.raises(ValueError, match="valid 2D LineString"):
        compute_route(network, start, end, **OPTIONS)
