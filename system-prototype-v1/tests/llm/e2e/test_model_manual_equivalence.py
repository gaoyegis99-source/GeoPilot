import json
import os
from pathlib import Path
from shutil import which
from time import monotonic, sleep

import pyogrio
import pytest
from fastapi.testclient import TestClient
from shapely import from_wkt

from geopilot.api.server import create_local_app
from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.llm.base import ModelErrorCode
from geopilot.llm.providers.mock import MockModelProvider
from geopilot.persistence.schema import open_database


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
METHOD = {"id": "attribute-filter-export", "version": 1}
TOOL = {"id": "qgis-extract-by-attribute", "version": 1}
STEP = "filter_features"
GOAL = {"summary": "按标识筛选西侧建筑点并导出。"}
OPTIONS = {"field_name": "source_feature_id", "match_value": "building-west"}
PARAMETERS = {"field": "source_feature_id", "operator": "equals", "value": "building-west"}
ROLE = "filtered_features"


@pytest.fixture(scope="module")
def qgis_process():
    executable = os.environ.get("GEOPILOT_QGIS_PROCESS") or which("qgis_process")
    if executable:
        return Path(executable)
    managed = ROOT / ".environments/qgis-deb/qgis_process"
    if managed.is_file():
        return managed
    pytest.skip("Set GEOPILOT_QGIS_PROCESS to execute the real QGIS workflow")


def _project(tmp_path, qgis_process, provider):
    database = tmp_path / "project.sqlite"
    workspace = tmp_path / "workspace"
    app = create_local_app(database, workspace, qgis_process=qgis_process, model_provider=provider)
    asset = register_asset(database, SOURCE, layer="targets")
    return app, database, workspace, asset


def _counts(database):
    with open_database(database, read_only=True) as connection:
        tables = ("task", "analysis_plan", "run", "artifact", "check_report")
        return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables)


def _get(client, path, **options):
    response = client.get(path, **options)
    assert response.status_code == 200, response.text
    return response.json()


def _post(client, path, body, *, status=200):
    response = client.post(path, json=body)
    assert response.status_code == status, response.text
    return response.json()


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_evidence(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _save_draft(client, inputs, *, method, options):
    task_id = _post(client, "/api/tasks", {"goal": GOAL}, status=201)["task_id"]
    draft = _post(client, f"/api/tasks/{task_id}/plans", {
        "goal": GOAL, "method": method, "inputs": inputs, "critical_options": options,
    })
    assert draft["status"] == "draft" and draft["issues"] == []
    assert draft["task_id"] == task_id and draft["version"] == 1
    return draft


def _confirm_and_run(client, draft):
    endpoint = f"/api/plans/{draft['id']}"
    version = {"plan_version": draft["version"]}
    confirmed = _post(client, f"{endpoint}/confirm", version)
    assert confirmed == {**draft, "status": "confirmed"}
    assert _get(client, endpoint, params={"version": draft["version"]}) == confirmed
    assert _get(client, f"/api/tasks/{draft['task_id']}")["runs"] == []

    run_id = _post(client, f"{endpoint}/runs", version, status=202)["run_id"]
    deadline = monotonic() + 30
    while True:
        state = _get(client, f"/api/runs/{run_id}")
        terminal = state["execution_status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}
        if (terminal and state["check_status"] != "CHECKING") or monotonic() >= deadline:
            break
        sleep(0.05)
    assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "PASSED"), state
    assert state["error"] is None
    return confirmed, state


def _run_evidence(client, workspace, plan, state, asset):
    run_id = state["run_id"]
    endpoint = f"/api/runs/{run_id}"
    origin = _get(client, f"{endpoint}/origin")
    assert origin == {"run_id": run_id, "task_id": plan["task_id"],
                      "plan_id": plan["id"], "plan_version": plan["version"]}
    workdir = workspace / "tasks" / origin["task_id"] / "runs" / run_id
    snapshot = _read_json(workdir / "request.json")
    assert (snapshot["run_id"], snapshot["plan"]) == (run_id, plan)
    staged = snapshot["asset_files"]["features"]
    assert staged["layer"] == "targets"
    assert fingerprint_file(workdir / staged["path"]).digest == asset.fingerprint

    step, = _get(client, f"{endpoint}/steps")["steps"]
    assert (step["step_key"], step["position"], step["status"], step["log_available"]) == (STEP, 0, "SUCCEEDED", True)
    request = _read_json(workdir / "steps" / STEP / "request.json")
    assert request["tool"] == TOOL and request["parameters"] == PARAMETERS
    assert request["inputs"] == {"source": staged}
    result = _read_json(workdir / "steps" / STEP / "result.json")
    assert result["exit_code"] == 0 and result["status"] == "SUCCEEDED"
    qgis_version = result["environment"]["qgis_version"]
    log = client.get(f"{endpoint}/steps/{STEP}/log")
    assert log.status_code == 200 and log.text == (workdir / result["log_path"]).read_text(encoding="utf-8")
    _, command, process = log.text.partition(" --json run native:extractbyattribute -\nstdout:\n")
    stdout, stderr, tail = process.partition("\nstderr:\n")
    assert command and stderr and "exit_code: 0" in tail.splitlines()
    engine = json.loads(stdout)
    assert qgis_version and engine["qgis_version"] == qgis_version
    assert {key: engine["inputs"][key] for key in ("FIELD", "OPERATOR", "VALUE")} == {
        "FIELD": "source_feature_id", "OPERATOR": 0, "VALUE": "building-west",
    }

    artifact, = _get(client, f"{endpoint}/artifacts")["artifacts"]
    assert artifact["run_id"] == run_id and artifact["role"] == ROLE and artifact["readable"] is True
    assert artifact["source"] == {"plan_id": plan["id"], "plan_version": plan["version"], "step_key": STEP,
                                  "output_name": "filtered", "tool": TOOL, "inputs": plan["inputs"]}
    output = workdir / artifact["uri"]
    download = client.get(f"/api/artifacts/{artifact['id']}/file")
    assert download.status_code == 200 and download.content == output.read_bytes()
    assert fingerprint_file(output).digest == artifact["fingerprint"]
    frame = pyogrio.read_dataframe(output, layer=artifact["layer"])
    reference = _read_json(SOURCE.with_name("source.json"))
    expected, = (item for item in reference["layers"]["targets"] if item["source_feature_id"] == "building-west")
    assert frame.source_feature_id.tolist() == [expected["source_feature_id"]]
    assert frame.crs.to_string() == reference["crs"]
    geometry = frame.geometry.item()
    assert geometry.equals_exact(from_wkt(expected["wkt"]), tolerance=0)

    checks = _get(client, f"{endpoint}/checks")
    report = checks["current_report"]
    assert checks["report_ids"] == [state["current_report_id"]] == [report["id"]]
    assert checks["check_status"] == report["status"] == "PASSED"
    assert report["run_id"] == run_id and report["rule_versions"] == {"output_exists": 1}
    detail = report["report"]
    assert detail == _read_json(workdir / "checks" / "report.json")
    assert detail["completed_rules"] == ["output_exists"] and detail["pending_rules"] == []
    assert detail["rules"]["output_exists"]["passed"] is True and detail["interruption"] is None
    phases = detail["phase_findings"]
    assert set(phases) == {"input", "process", "artifact"}
    assert all(findings and all(item["passed"] is True for item in findings) for findings in phases.values())
    assert {"step_order_matches", "step_process_matches"} <= {item["rule"] for item in phases["process"]}
    return {"origin": origin, "workdir": workdir.relative_to(workspace.parent).as_posix(),
            "input_fingerprint": asset.fingerprint, "artifact_id": artifact["id"], "artifact_uri": artifact["uri"],
            "result": {"feature_ids": frame.source_feature_id.tolist(), "crs": frame.crs.to_string(),
                       "wkt": geometry.wkt}, "qgis_version": qgis_version, "report_id": report["id"],
            "execution_status": state["execution_status"], "check_status": checks["check_status"],
            "rule_versions": report["rule_versions"], "rules": detail["rules"]}


def test_model_and_manual_confirmed_runs_match(tmp_path, qgis_process):
    suggestion = {"method": METHOD, "goal": {"summary": "筛选建筑点。"},
                  "options": {**OPTIONS, "match_value": "building-east"}, "missing": [],
                  "explanation": "使用已登记的属性筛选方法。"}
    provider = MockModelProvider(response_text=json.dumps(suggestion, ensure_ascii=False))
    app, database, workspace, asset = _project(tmp_path, qgis_process, provider)
    inputs = {"features": asset.ref.model_dump(mode="json")}

    with TestClient(app) as client:
        manual = _save_draft(client, inputs, method=METHOD, options=OPTIONS)
        assert _counts(database) == (1, 1, 0, 0, 0)
        preview = _post(client, "/api/model-assistance", {"goal": GOAL, "method": METHOD, "inputs": inputs})
        assert preview == {**suggestion, "explanation_issue": None, "issues": []}
        assert _counts(database) == (1, 1, 0, 0, 0)
        assert _get(client, f"/api/plans/{manual['id']}", params={"version": manual["version"]}) == manual

        user_changes = {"critical_options.match_value": {"suggested": preview["options"]["match_value"],
                                                          "applied": "building-west"}}
        edited_options = {**preview["options"], "match_value": user_changes["critical_options.match_value"]["applied"]}
        assisted = _save_draft(client, inputs, method=preview["method"], options=edited_options)
        assert _counts(database) == (2, 2, 0, 0, 0)
        assert (manual["id"], manual["task_id"]) != (assisted["id"], assisted["task_id"])

        manual_plan, manual_state = _confirm_and_run(client, manual)
        model_plan, model_state = _confirm_and_run(client, assisted)
        comparison = {key: value for key, value in manual_plan.items() if key not in {"id", "task_id"}}
        assert comparison == {key: value for key, value in model_plan.items() if key not in {"id", "task_id"}}
        assert (comparison["goal"], comparison["method"], comparison["inputs"], comparison["critical_options"]) == (
            GOAL, METHOD, inputs, OPTIONS,
        )
        assert [(step["key"], step["tool"], step["parameters"]) for step in comparison["steps"]] == [(STEP, TOOL, PARAMETERS)]
        manual_result = _run_evidence(client, workspace, manual_plan, manual_state, asset)
        model_result = _run_evidence(client, workspace, model_plan, model_state, asset)
        for field in ("result", "qgis_version", "rule_versions", "rules"):
            assert manual_result[field] == model_result[field]
        assert manual_result["artifact_id"] != model_result["artifact_id"]
        assert manual_result["origin"]["run_id"] != model_result["origin"]["run_id"]
        assert manual_result["report_id"] != model_result["report_id"]

    assert _counts(database) == (2, 2, 2, 2, 2)
    assert fingerprint_file(SOURCE).digest == asset.fingerprint
    _write_evidence(tmp_path / "comparison.json", {"goal": GOAL, "model_preview": preview,
                    "user_changes": user_changes, "confirmed_plan_fields": comparison,
                    "manual_run": manual_result, "model_run": model_result})


def test_model_timeout_preserves_an_executable_manual_draft(tmp_path, qgis_process):
    provider = MockModelProvider(error_code=ModelErrorCode.TIMEOUT)
    app, database, workspace, asset = _project(tmp_path, qgis_process, provider)
    inputs = {"features": asset.ref.model_dump(mode="json")}

    with TestClient(app) as client:
        draft = _save_draft(client, inputs, method=METHOD, options=OPTIONS)
        before = _get(client, f"/api/tasks/{draft['task_id']}")
        failure = _post(client, "/api/model-assistance", {"goal": GOAL, "method": METHOD, "inputs": inputs}, status=504)
        assert failure["detail"]["code"] == "MODEL_TIMEOUT"
        assert _get(client, f"/api/tasks/{draft['task_id']}") == before
        assert _get(client, f"/api/plans/{draft['id']}", params={"version": draft["version"]}) == draft
        blocked = _post(client, f"/api/plans/{draft['id']}/runs", {"plan_version": draft["version"]}, status=409)
        assert blocked["detail"]["code"] == "PLAN_NOT_CONFIRMED"
        assert _counts(database) == (1, 1, 0, 0, 0)

        confirmed, state = _confirm_and_run(client, draft)
        result = _run_evidence(client, workspace, confirmed, state, asset)

    assert _counts(database) == (1, 1, 1, 1, 1)
    assert fingerprint_file(SOURCE).digest == asset.fingerprint
    _write_evidence(tmp_path / "fallback.json", {"model_error": failure["detail"]["code"],
                    "unconfirmed_run_error": blocked["detail"]["code"], "draft": draft, "manual_run": result})
