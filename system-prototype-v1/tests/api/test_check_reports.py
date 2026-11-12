import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from threading import Event

import pyogrio
import pytest
import yaml
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.application.plan_runner import execute_claimed_run
from geopilot.application.task_context import build_task_context
from geopilot.artifacts.service import list_artifacts
from geopilot.checks.artifact_checks import ARTIFACT_RULES, vector_rule
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import MethodCatalog, load_method_catalog
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.run_repository import get_run, list_run_steps
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import save_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
RULE = "filtered_feature_count"
ROLE = "filtered_features"
SOURCE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
CHECK_INTERRUPTION = "T16 checker stopped after reading the actual feature count"


@pytest.fixture
def qgis_filter_draft(tmp_path):
    executable = os.getenv("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if executable is None:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS for the real T16 QGIS process")
    tool_file = "qgis-extract-by-attribute.yaml"
    registry = ToolRegistry(qgis_process=executable)
    registry.register(ToolContract.model_validate(yaml.safe_load((CONFIG / "tools" / tool_file).read_text(encoding="utf-8"))))
    bindings = yaml.safe_load((CONFIG / "runner_bindings" / tool_file).read_text(encoding="utf-8"))

    original = load_method_catalog(CONFIG / "methods").get("attribute-filter-export", 1)
    method = MethodDefinition.model_validate({
        **original.model_dump(mode="json"), "check_rules": [*original.check_rules, RULE],
    })
    catalog = MethodCatalog([method], ())
    database = tmp_path / "project.sqlite"
    workspace = tmp_path / "workspace"
    initialize_database(database)
    asset = register_asset(database, SOURCE, layer="targets")
    context = build_task_context(
        database, task_id="t16-task", goal=TaskGoal(summary="Filter the west building"),
        method=method, selected={"features": asset.ref}, registry=registry,
    )
    draft = build_draft(
        context, method, plan_id="t16-plan", version=2,
        options={"field_name": "source_feature_id", "match_value": "building-west"},
    )
    assert not draft.issues
    save_draft(database, draft, context=context)
    return database, workspace, draft, catalog, registry, executable, bindings


def _interrupt_after_reading(check):
    def interrupted(context):
        finding, = [item for item in check(context) if item.field == f"artifacts.{ROLE}.feature_count"]
        assert (finding.observed, finding.expected) == (1, 2)
        raise RuntimeError(CHECK_INTERRUPTION)
    return interrupted


@pytest.mark.parametrize(("scenario", "expected_status"), [
    ("blocking", "FAILED"), ("warning", "WARNING"), ("interrupted", "NOT_CHECKED"),
])
def test_real_qgis_execution_status_is_independent_of_check_report(qgis_filter_draft, scenario, expected_status):
    database, workspace, draft, catalog, registry, executable, bindings = qgis_filter_draft
    severity = "warning" if scenario == "warning" else "blocking"
    checker = vector_rule(RULE, ROLE, expected_count=2, severity=severity)
    if scenario == "interrupted":
        checker = _interrupt_after_reading(checker)
    rules = {**ARTIFACT_RULES, RULE: checker}
    finished = Event()

    def execute(run):
        try:
            return execute_claimed_run(
                database, run, workspace_root=workspace, registry=registry,
                qgis_process=executable, qgis_bindings=bindings, artifact_rules=rules, timeout=40,
            )
        finally:
            finished.set()

    app = create_app(
        database, workspace_root=workspace, catalog=catalog, registry=registry,
        registered_rules=rules, execute_run=execute,
    )
    with TestClient(app) as client:
        plan_endpoint = f"/api/plans/{draft.id}"
        version = {"plan_version": draft.version}
        confirmation = client.post(f"{plan_endpoint}/confirm", json=version)
        assert confirmation.status_code == 200, confirmation.text
        confirmed = confirmation.json()
        assert (confirmed["id"], confirmed["version"], confirmed["status"]) == (draft.id, draft.version, "confirmed")
        assert confirmed["check_rules"] == ["output_exists", RULE]
        with open_database(database, read_only=True) as connection:
            assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 0
        assert not workspace.exists()

        accepted = client.post(f"{plan_endpoint}/runs", json=version)
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["run_id"]
        endpoint = f"/api/runs/{run_id}"
        assert finished.wait(50), client.get(endpoint).text

        checks_response = client.get(f"{endpoint}/checks")
        assert checks_response.status_code == 200, checks_response.text
        state_response = client.get(endpoint)
        assert state_response.status_code == 200, state_response.text
        artifacts_response = client.get(f"{endpoint}/artifacts")
        assert artifacts_response.status_code == 200, artifacts_response.text
        missing = client.get("/api/runs/t16-unknown/checks")
        assert missing.status_code == 404 and missing.json()["detail"]["code"] == "RUN_NOT_FOUND"

    state, checks, artifacts = state_response.json(), checks_response.json(), artifacts_response.json()
    assert (state["run_id"], state["execution_status"], state["check_status"]) == (run_id, "SUCCEEDED", expected_status), state
    assert (checks["run_id"], checks["check_status"]) == (run_id, expected_status)
    current = checks["current_report"]
    assert current["id"] == state["current_report_id"]
    assert checks["report_ids"] == [current["id"]]
    assert (current["run_id"], current["status"]) == (run_id, expected_status)
    assert set(confirmed["check_rules"]) <= current["rule_versions"].keys()
    report = current["report"]
    assert (report["run_id"], report["status"]) == (run_id, expected_status)
    phases = report["phase_findings"]
    assert set(phases) == {"input", "process", "artifact"}
    field_check, = [item for item in phases["input"] if item["rule"] == "selected_field_exists"]
    assert field_check["passed"] is True and field_check["expected"] == "source_feature_id"
    algorithm_check, = [item for item in phases["process"] if item["field"] == "engine.algorithm"]
    assert algorithm_check["passed"] is True and algorithm_check["observed"] == "native:extractbyattribute"
    assert report["rules"]["output_exists"]["passed"] is True

    if scenario == "interrupted":
        assert state["error"]["code"] == report["interruption"]["code"] == "CHECK_INTERRUPTED"
        assert CHECK_INTERRUPTION in report["interruption"]["message"]
        assert report["completed_rules"] == ["output_exists"] and report["pending_rules"] == [RULE]
    else:
        if scenario == "blocking":
            assert state["error"]["code"] == "CHECK_BLOCKED" and state["error"]["phase"] == "check"
        else:
            assert state["error"] is None
        assert report["pending_rules"] == [] and report["interruption"] is None
        assert report["completed_rules"] == ["output_exists", RULE]
        assert report["rules"][RULE]["passed"] is False
        finding, = [item for item in phases["artifact"] if (
            item["rule"] == RULE and item["field"] == f"artifacts.{ROLE}.feature_count"
        )]
        assert (finding["passed"], finding["severity"], finding["observed"], finding["expected"]) == (False, severity, 1, 2)

    assert (artifacts["run_id"], artifacts["check_status"]) == (run_id, expected_status)
    output, = artifacts["artifacts"]
    assert output["run_id"] == run_id and output["role"] == ROLE and output["readable"] is True
    assert output["source"]["inputs"]["features"] == confirmed["inputs"]["features"]

    with open_database(database, read_only=True) as connection:
        stored = get_run(connection, run_id)
        step, = list_run_steps(connection, run_id)
        registered, = list_artifacts(connection, run_id)
        stored_report, = connection.execute(
            "SELECT id, run_id, status, report_json, rule_versions_json, created_at FROM check_report WHERE run_id = ?",
            (run_id,),
        )
    assert (stored.id, stored.plan_id, stored.plan_version) == (run_id, draft.id, draft.version)
    assert (stored.execution_status, stored.check_status, stored.current_report_id) == ("SUCCEEDED", expected_status, current["id"])
    assert (stored_report["id"], stored_report["run_id"], stored_report["status"]) == (current["id"], run_id, expected_status)
    assert json.loads(stored_report["report_json"]) == report
    assert json.loads(stored_report["rule_versions_json"]) == current["rule_versions"]
    assert datetime.fromisoformat(stored_report["created_at"]) == datetime.fromisoformat(current["created_at"])
    assert (step.run_id, step.step_key, step.status, step.result.exit_code) == (run_id, "filter_features", "SUCCEEDED", 0)
    assert step.result.outputs["filtered"] == registered.output
    assert output == {**registered.model_dump(mode="json"), "readable": True}
    workdir = Path(stored.workdir)
    assert "native:extractbyattribute" in (workdir / step.result.log_path).read_text(encoding="utf-8")
    features = pyogrio.read_dataframe(workdir / registered.uri, layer=registered.layer)
    assert features.crs.to_epsg() == 32650 and features["source_feature_id"].tolist() == ["building-west"]
