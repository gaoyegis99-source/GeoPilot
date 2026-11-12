import json
import time
from pathlib import Path
from threading import Event

import geopandas as gpd
import pyogrio
import pytest
import yaml
from fastapi.testclient import TestClient
from shapely.geometry import box

from geopilot.api.app import create_app
from geopilot.application import plan_runner
from geopilot.application.plan_runner import execute_claimed_recheck, execute_claimed_run
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.checks.tool_checks import TOOL_CHECKS
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
TOOL = {"id": "python-test-tool-check", "version": 1}
METHOD = {"id": "test-tool-checks", "version": 1}
_WORKER = '''
import pyogrio
from geopilot.gis.tools.tool_command import run_tool_command

def run(request):
    source = request["inputs"]["source"]
    frame = pyogrio.read_dataframe(source["path"], layer=source["layer"])
    for name, target in request["output_targets"].items():
        output = frame.to_crs(3857) if name == "original" and request["parameters"]["wrong_crs"] else frame
        pyogrio.write_dataframe(output, target["path"], layer=target["layer"], driver="GPKG")
    return request["output_targets"]

raise SystemExit(run_tool_command(run, description="Write checked point outputs"))
'''


def _registry(worker, rules):
    implementations = {"geopilot.test_tool_check": worker}
    registry = ToolRegistry(python_implementations=implementations)
    registry.register(ToolContract.model_validate({
        **TOOL, "runner": "python", "implementation": "geopilot.test_tool_check",
        "inputs": {"source": "vector_point"}, "outputs": {"selected": "vector_point", "original": "vector_point"},
        "parameters": {"wrong_crs": {"type": "boolean"}},
        "measurement_policy": {"distance": "not_applicable", "area": "not_applicable"},
        "preconditions": [], "check_rules": rules,
    }))
    return registry, implementations


def _workbench(tmp_path, *, tool_rules, method_rules=(), wrong_crs=False):
    worker = tmp_path / "tool-check.py"
    worker.write_text(_WORKER, encoding="utf-8")
    registry, implementations = _registry(worker, tool_rules)
    method = MethodDefinition.model_validate({
        **METHOD, "locked_order": True, "inputs": {"source": "vector_point"}, "critical_options": {},
        "steps": [
            {"key": key, "tool": TOOL, "inputs": {"source": binding}, "parameters": {"wrong_crs": {"literal": wrong}},
             "outputs": {"selected": "vector_point", "original": "vector_point"}}
            for key, binding, wrong in (("first", {"asset": "source"}, wrong_crs), ("last", {"step": "first", "output": "selected"}, False))
        ],
        "expected_artifacts": [{"role": "selected", "step": "last", "output": "selected", "type": "vector_point"}],
        "check_rules": list(method_rules),
    })
    database, workspace = tmp_path / "project.sqlite", tmp_path / "workspace"
    initialize_database(database)
    runtime = {"registry": registry, "python_implementations": implementations}
    app = create_app(
        database, workspace_root=workspace, catalog=MethodCatalog([method], ()), registry=registry,
        registered_rules=RUN_CHECKS,
        execute_run=lambda run: execute_claimed_run(database, run, workspace_root=workspace, **runtime),
        execute_recheck=lambda run, attempt: execute_claimed_recheck(database, run, attempt, **runtime),
    )
    return app, database, workspace, worker, runtime


def _post(client, endpoint, payload, status=200):
    response = client.post(endpoint, json=payload)
    assert response.status_code == status, response.text
    return response.json()


def _draft(client, source=SOURCE, layer="targets"):
    asset = _post(client, "/api/assets", {"path": str(source), "layer": layer})
    task = _post(client, "/api/tasks", {"goal": {"summary": "Check both point outputs"}}, status=201)["task_id"]
    draft = _post(client, f"/api/tasks/{task}/plans", {
        "method": METHOD, "inputs": {"source": {key: asset[key] for key in ("asset_id", "asset_version")}},
        "critical_options": {},
    })
    return draft, f"/api/plans/{draft['id']}", {"plan_version": draft["version"]}


def _wait_for_report(client, run_id, report_id=None):
    deadline = time.monotonic() + 40
    while True:
        state = client.get(f"/api/runs/{run_id}").json()
        if state["current_report_id"] and (report_id is None or state["current_report_id"] == report_id):
            break
        assert time.monotonic() < deadline and state["execution_status"] not in {"FAILED", "CANCELLED"}, state
        time.sleep(0.1)
    report = client.get(f"/api/runs/{run_id}/checks").json()["current_report"]
    assert report["id"] == state["current_report_id"]
    return state, report


def test_tool_only_rules_read_each_real_step_and_keep_execution_and_check_separate(tmp_path):
    app, _, workspace, worker, runtime = _workbench(
        tmp_path, tool_rules=["output_exists", "output_crs_expected"], wrong_crs=True,
    )
    with TestClient(app) as client:
        draft, endpoint, version = _draft(client)
        assert draft["check_rules"] == []
        confirmed = _post(client, endpoint + "/confirm", version)
        assert draft["steps"] == confirmed["steps"]
        for step in confirmed["steps"]:
            assert step["tool_check_rules"] == [
                {"name": "output_exists", "version": 1}, {"name": "output_crs_expected", "version": 1},
            ]
        # The worker's live contract can no longer supply the list used by this saved version.
        runtime["registry"], _ = _registry(worker, ["output_exists"])
        run_id = _post(client, endpoint + "/runs", version, status=202)["run_id"]
        state, report = _wait_for_report(client, run_id)
        assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "FAILED")
        body = report["report"]
        assert body["rules"] == {} and body["completed_rules"] == [] and report["rule_versions"] == {}
        assert len(body["tool_rules"]) == 4 and len(body["completed_tool_rules"]) == 4 and body["pending_tool_rules"] == []
        findings = [item for item in body["phase_findings"]["artifact"] if item.get("rule_version") == 1]
        assert len(findings) == 8 and {item["step_key"] for item in findings} == {"first", "last"}
        assert all(item["tool"] == TOOL for item in findings)
        failure, = [item for item in findings if item["passed"] is False]
        assert (failure["step_key"], failure["field"], failure["rule"]) == (
            "first", "steps.first.outputs.original.crs", "output_crs_expected",
        )
        assert failure["observed"] == "EPSG:3857" and failure["expected"] == "EPSG:32650"
        assert (state["error"]["step_key"], state["error"]["field"]) == ("first", "steps.first.outputs.original.crs")
        output, = client.get(f"/api/runs/{run_id}/artifacts").json()["artifacts"]
        assert output["role"] == "selected" and output["readable"]
        assert client.get(f"/api/artifacts/{output['id']}/file").status_code == 200
        directory = workspace / "tasks" / draft["task_id"] / "runs" / run_id
        manifest = json.loads((directory / "outputs/manifest.json").read_text())
        assert any(path.startswith("intermediate/first/") for path in manifest["tool_files"])


@pytest.mark.parametrize(("rules", "code", "position"), [
    (["unknown_tool_rule"], "UNKNOWN_TOOL_CHECK_RULE", 0),
    (["output_exists", "output_exists"], "DUPLICATE_TOOL_CHECK_RULE", 1),
])
def test_tool_rule_errors_are_located_before_any_run(tmp_path, rules, code, position):
    app, _, _, _, _ = _workbench(tmp_path, tool_rules=rules)
    with TestClient(app) as client:
        entry, = client.get("/api/methods").json()["methods"]
        assert entry["available"] is False
        field = f"steps.0.tool.check_rules.{position}"
        assert any(issue["code"] == code and issue["field"] == field for issue in entry["issues"])
        draft, endpoint, version = _draft(client)
        problems = _post(client, endpoint + "/confirm", version, status=422)["detail"]["issues"]
        assert any(issue["code"] == code and issue["field"] == field for issue in problems)
        assert client.get(f"/api/tasks/{draft['task_id']}").json()["runs"] == []


def test_tool_rule_preview_rejects_version_drift_until_draft_is_revised(tmp_path, monkeypatch):
    app, _, _, _, _ = _workbench(tmp_path, tool_rules=["output_exists"])
    with TestClient(app) as client:
        draft, endpoint, version = _draft(client)
        assert draft["steps"][0]["tool_check_rules"] == [{"name": "output_exists", "version": 1}]
        monkeypatch.setitem(TOOL_CHECKS, ("output_exists", 2), TOOL_CHECKS["output_exists", 1])

        issue, = _post(client, endpoint + "/confirm", version, status=422)["detail"]["issues"]
        assert (issue["code"], issue["field"]) == ("TOOL_CHECK_RULE_MISMATCH", "steps.0.tool_check_rules")
        assert client.get(endpoint, params={"version": draft["version"]}).json() == draft

        revised = _post(client, f"/api/tasks/{draft['task_id']}/plans", {
            "base_plan_id": draft["id"], "base_plan_version": draft["version"],
            "method": draft["method"], "inputs": draft["inputs"], "critical_options": draft["critical_options"],
        })
        assert revised["id"] == draft["id"] and revised["version"] == draft["version"] + 1
        confirmed = _post(client, endpoint + "/confirm", {"plan_version": revised["version"]})
        assert revised["steps"] == confirmed["steps"]
        assert all(step["tool_check_rules"] == [{"name": "output_exists", "version": 2}] for step in confirmed["steps"])


def test_legacy_confirmation_without_tool_checks_remains_runnable(tmp_path):
    app, database, _, _, _ = _workbench(tmp_path, tool_rules=["output_exists"], method_rules=["output_exists"])
    with TestClient(app) as client:
        draft, endpoint, version = _draft(client)
        confirmed = _post(client, endpoint + "/confirm", version)
        for step in confirmed["steps"]:
            step.pop("tool_check_rules", None)
        # Represent a persisted confirmation written by the earlier JSON schema.
        with open_database(database) as connection, connection:
            connection.execute("UPDATE analysis_plan SET plan_json = ? WHERE id = ? AND version = ?",
                               (json.dumps(confirmed), draft["id"], draft["version"]))
        assert client.get(endpoint, params={"version": draft["version"]}).json() == confirmed
        run_id = _post(client, endpoint + "/runs", version, status=202)["run_id"]
        state, report = _wait_for_report(client, run_id)
        assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "PASSED")
        assert report["rule_versions"] == {"output_exists": 1}
        assert report["report"]["completed_rules"] == ["output_exists"] and "tool_rules" not in report["report"]


def test_recheck_uses_frozen_tool_version_and_original_intermediate_files(tmp_path, monkeypatch):
    app, _, workspace, _, _ = _workbench(tmp_path, tool_rules=["output_exists", "output_crs_expected"])
    entered, resume = Event(), Event()
    original_check = TOOL_CHECKS["output_exists", 1]

    def pause(context):
        result = original_check(context)
        if not entered.is_set():
            entered.set()
            assert resume.wait(30)
        return result

    monkeypatch.setitem(TOOL_CHECKS, ("output_exists", 1), pause)
    with TestClient(app) as client:
        draft, endpoint, version = _draft(client)
        _post(client, endpoint + "/confirm", version)
        run_id = _post(client, endpoint + "/runs", version, status=202)["run_id"]
        run_endpoint = f"/api/runs/{run_id}"
        try:
            assert entered.wait(30), client.get(run_endpoint).text
            state = client.get(run_endpoint).json()
            assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "CHECKING")
            registered, = client.get(run_endpoint + "/artifacts").json()["artifacts"]
            assert registered["role"] == "selected" and registered["readable"]
            _post(client, run_endpoint + "/cancel", {}, status=202)
        finally:
            resume.set()
        _, initial = _wait_for_report(client, run_id)
        assert initial["status"] == "NOT_CHECKED" and initial["report"]["pending_tool_rules"]

        monkeypatch.delitem(TOOL_CHECKS, ("output_exists", 1))
        monkeypatch.setitem(TOOL_CHECKS, ("output_exists", 2), original_check)
        failure = _post(client, run_endpoint + "/checks", {}, status=409)["detail"]
        assert (failure["code"], failure["field"]) == ("CHECK_RULE_VERSION_MISMATCH", "steps.first.check_rules.output_exists")
        monkeypatch.setitem(TOOL_CHECKS, ("output_exists", 1), original_check)

        directory = workspace / "tasks" / draft["task_id"] / "runs" / run_id
        intermediate = directory / "intermediate/first/original.gpkg"
        bytes_before = intermediate.read_bytes()
        frame = pyogrio.read_dataframe(intermediate, layer="original")
        pyogrio.write_dataframe(frame.iloc[:1], intermediate, layer="original", driver="GPKG")
        failure = _post(client, run_endpoint + "/checks", {}, status=409)["detail"]
        assert (failure["code"], failure["field"]) == (
            "ARTIFACT_CHANGED", "tool_files.intermediate/first/original.gpkg.fingerprint",
        )
        intermediate.write_bytes(bytes_before)
        evidence = [*directory.glob("steps/*/process.log"), *directory.glob("steps/*/result.json"), intermediate]
        before = {path: fingerprint_file(path) for path in evidence}

        report_id = _post(client, run_endpoint + "/checks", {}, status=202)["report_id"]
        state, rechecked = _wait_for_report(client, run_id, report_id)
        assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "PASSED")
        assert len(rechecked["report"]["completed_tool_rules"]) == 4 and not rechecked["report"]["pending_tool_rules"]
        assert all(rule["rule_version"] == 1 for rule in rechecked["report"]["tool_rules"].values())
        historical = client.get(run_endpoint + "/checks", params={"report_id": initial["id"]}).json()
        assert historical["selected_report"] == initial and rechecked["id"] != initial["id"]
        assert {path: fingerprint_file(path) for path in evidence} == before


def test_tool_rules_do_not_pass_using_a_reconstructed_request_when_stored_request_differs(tmp_path, monkeypatch):
    app, _, _, _, _ = _workbench(tmp_path, tool_rules=["output_exists"])
    original = plan_runner.collect_check_findings

    def alter_after_process_check(database, run_id, context, **kwargs):
        findings = original(database, run_id, context, **kwargs)
        request = context.workdir / "steps/first/request.json"
        document = json.loads(request.read_text())
        document["parameters"]["wrong_crs"] = True
        request.write_text(json.dumps(document))
        return findings

    monkeypatch.setattr(plan_runner, "collect_check_findings", alter_after_process_check)
    with TestClient(app) as client:
        _, endpoint, version = _draft(client)
        _post(client, endpoint + "/confirm", version)
        run_id = _post(client, endpoint + "/runs", version, status=202)["run_id"]
        state, report = _wait_for_report(client, run_id)
        assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "FAILED")
        failure, = [finding for finding in report["report"]["phase_findings"]["artifact"]
                    if finding.get("rule_version") == 1 and finding["passed"] is False]
        assert failure["field"] == "steps.first.request"
        assert report["report"]["tool_rules"]["steps.last.check_rules.output_exists"]["passed"] is True


def test_area_tool_contract_checks_actual_csv_against_confirmed_crs_and_units(tmp_path):
    source = tmp_path / "polygons.gpkg"
    frame = gpd.GeoDataFrame({"source_feature_id": ["square"]}, geometry=[box(100, 100, 200, 200)], crs=6933)
    pyogrio.write_dataframe(frame, source, layer="polygons", driver="GPKG")
    worker = ROOT / "src/geopilot/gis/tools/planar_area.py"
    contract = ToolContract.model_validate(yaml.safe_load((ROOT / "config/tools/python-area.yaml").read_text()))
    implementations = {contract.implementation: worker}
    registry = ToolRegistry(python_implementations=implementations)
    registry.register(contract)
    crs = "urn:ogc:def:crs:EPSG::6933"
    method = MethodDefinition.model_validate({
        **METHOD, "locked_order": True, "inputs": {"source": "vector_polygon"}, "critical_options": {},
        "steps": [{"key": "measure", "tool": contract.ref, "inputs": {"source": {"asset": "source"}},
                   "parameters": {"area_crs": {"literal": crs}, "area_unit": {"literal": "square_meter"}},
                   "outputs": {"statistics": "table_csv"}}],
        "expected_artifacts": [{"role": "area", "step": "measure", "output": "statistics", "type": "table_csv"}],
        "check_rules": [],
    })
    database, workspace = tmp_path / "project.sqlite", tmp_path / "workspace"
    initialize_database(database)
    app = create_app(
        database, workspace_root=workspace, catalog=MethodCatalog([method], ()), registry=registry,
        execute_run=lambda run: execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry, python_implementations=implementations,
        ),
    )
    with TestClient(app) as client:
        _, endpoint, version = _draft(client, source, "polygons")
        _post(client, endpoint + "/confirm", version)
        run_id = _post(client, endpoint + "/runs", version, status=202)["run_id"]
        state, report = _wait_for_report(client, run_id)
        assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "PASSED")
        findings = {item["rule"]: item for item in report["report"]["phase_findings"]["artifact"] if "rule_version" in item}
        assert set(findings) == set(contract.check_rules) and all(item["passed"] for item in findings.values())
        assert findings["output_area_crs_expected"]["observed"] == {"row_count": 1, "values": [crs]}
        assert findings["output_area_unit_expected"]["observed"] == {"row_count": 1, "values": ["square_meter"]}
        artifact, = client.get(f"/api/runs/{run_id}/artifacts").json()["artifacts"]
        assert f"square,10000.0,square_meter,{crs}" in client.get(f"/api/artifacts/{artifact['id']}/file").text
