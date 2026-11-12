from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pyogrio
import pytest
import yaml
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.application.plan_runner import execute_claimed_recheck, execute_claimed_run
from geopilot.application.run_checks import RUN_CHECKS, RunCheckResult
from geopilot.application.task_context import build_task_context
from geopilot.artifacts.service import list_artifacts
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
SOURCE = ROOT / "tests/fixtures/vector/t13/spatial-relations.gpkg"
ROLE = "clipped_targets"
RULE = "expected_clipped_points"
RULE_VERSION = 3
INTERRUPTION = "Checker stopped after reopening the clipped point"
WORKERS = {
    "geopilot.select_spatial_relation": ROOT / "src/geopilot/gis/tools/select_spatial_relation.py",
    "geopilot.clip_vector_to_extent": ROOT / "src/geopilot/gis/tools/clip_vector_to_extent.py",
}


@pytest.fixture
def project(tmp_path):
    database = tmp_path / "project.sqlite"
    workspace = tmp_path / "workspace"
    initialize_database(database)
    registry = ToolRegistry(python_implementations=WORKERS)
    base = load_method_catalog(ROOT / "config/methods").get("spatial-select-and-clip", 1)
    method = MethodDefinition.model_validate({
        **base.model_dump(mode="json"), "check_rules": [*base.check_rules, RULE],
    })
    for step in method.steps:
        contract_file = ROOT / "config/tools" / f"{step.tool.id}.yaml"
        registry.register(ToolContract.model_validate(yaml.safe_load(contract_file.read_text(encoding="utf-8"))))
    catalog = MethodCatalog([method], ())
    selected = {
        "targets": register_asset(database, SOURCE, layer="points").ref,
        "relation_area": register_asset(database, SOURCE, layer="area").ref,
    }
    selected["analysis_extent"] = selected["relation_area"]
    context = build_task_context(
        database, task_id="recheck-task", goal=TaskGoal(summary="Find points inside the polygon"),
        method=method, selected=selected, registry=registry,
    )
    draft = build_draft(context, method, plan_id="recheck-plan", version=2, options={"spatial_relation": "within"})
    assert not draft.issues, draft.issues
    save_draft(database, draft, context=context)
    return database, workspace, registry, catalog, draft


def _check_points(context):
    output = context.artifacts[ROLE]
    points = pyogrio.read_dataframe(context.workdir / output.path, layer=output.layer)
    ids = points["source_feature_id"].tolist()
    assert ids == ["point-inside"] and points.crs.to_epsg() == 32650
    return RunCheckResult(True, "The actual clipped point matches T13", observed=ids)


def _interrupt(context):
    _check_points(context)
    raise RuntimeError(INTERRUPTION)


def _app(project, *, initial_check, recheck_check=_check_points, version=RULE_VERSION):
    database, workspace, registry, catalog, _ = project
    versions = {RULE: version}
    run_done, recheck_done = Event(), Event()
    initial_rules = {**RUN_CHECKS, RULE: initial_check}
    recheck_rules = {**RUN_CHECKS, RULE: recheck_check}

    def execute(run):
        try:
            execute_claimed_run(
                database, run, workspace_root=workspace, registry=registry, python_implementations=WORKERS,
                check_rules=initial_rules, rule_versions=versions,
            )
        finally:
            run_done.set()

    def recheck(run, attempt):
        try:
            execute_claimed_recheck(
                database, run, attempt, registry=registry, python_implementations=WORKERS,
                check_rules=recheck_rules, rule_versions=versions,
            )
        finally:
            recheck_done.set()

    app = create_app(
        database, workspace_root=workspace, registry=registry, catalog=catalog, registered_rules=initial_rules,
        execute_run=execute, execute_recheck=recheck, rule_versions=versions,
    )
    return app, run_done, recheck_done


def _start_run(client, draft):
    endpoint = f"/api/plans/{draft.id}"
    version = {"plan_version": draft.version}
    confirmation = client.post(f"{endpoint}/confirm", json=version)
    assert confirmation.status_code == 200, confirmation.text
    accepted = client.post(f"{endpoint}/runs", json=version)
    assert accepted.status_code == 202, accepted.text
    return accepted.json()["run_id"]


def _get(client, endpoint, **options):
    response = client.get(endpoint, **options)
    assert response.status_code == 200, response.text
    return response.json()


def _execution(database, run_id):
    with open_database(database, read_only=True) as connection:
        run = get_run(connection, run_id)
        steps = list_run_steps(connection, run_id)
        artifacts = list_artifacts(connection, run_id)
    return run, steps, artifacts


def _evidence(workdir, steps, artifacts):
    files = {workdir / output.uri for output in artifacts}
    files.update((workdir / "request.json", workdir / "checks/report.json"))
    for step in steps:
        log = workdir / step.log_path
        assert any(line.startswith("$ ") for line in log.read_text(encoding="utf-8").splitlines())
        files.update((log, log.with_name("request.json"), log.with_name("result.json"), log.with_name("python-result.json")))
        files.update(workdir / output.path for output in step.result.outputs.values())
    return {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in files}


@pytest.mark.parametrize("cause", ["exception", "cancel"])
def test_recheck_of_interrupted_real_run_preserves_execution_and_publishes_new_report(project, cause):
    database, _, _, _, draft = project
    entered, release = Event(), Event()
    rechecking, finish_recheck = Event(), Event()

    def wait_for_cancellation(context):
        result = _check_points(context)
        entered.set()
        if not release.wait(15):
            raise RuntimeError("Test did not release the check")
        return result

    def wait_for_repeat_request(context):
        result = _check_points(context)
        rechecking.set()
        if not finish_recheck.wait(15):
            raise RuntimeError("Test did not release the recheck")
        return result

    initial_check = _interrupt if cause == "exception" else wait_for_cancellation
    app, run_done, recheck_done = _app(project, initial_check=initial_check, recheck_check=wait_for_repeat_request)
    with TestClient(app) as client:
        run_id = _start_run(client, draft)
        endpoint = f"/api/runs/{run_id}"
        if cause == "cancel":
            try:
                assert entered.wait(15), _get(client, endpoint)
                state = _get(client, endpoint)
                assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "CHECKING")
                cancel = client.post(f"{endpoint}/cancel")
                assert cancel.status_code == 202 and cancel.json()["cancel_requested"] is True, cancel.text
            finally:
                release.set()
        assert run_done.wait(15), _get(client, endpoint)

        initial_state = _get(client, endpoint)
        assert (initial_state["execution_status"], initial_state["check_status"]) == ("SUCCEEDED", "NOT_CHECKED")
        checks_endpoint = f"{endpoint}/checks"
        initial_checks = _get(client, checks_endpoint)
        initial = initial_checks["current_report"]
        assert initial["id"] == initial_state["current_report_id"]
        assert initial_checks["report_ids"] == [initial["id"]]
        assert initial["rule_versions"][RULE] == RULE_VERSION and initial["rule_versions"]["output_exists"] == 1
        interruption = initial["report"]["interruption"]
        assert interruption["code"] == initial_state["error"]["code"]
        if cause == "exception":
            assert interruption["code"] == "CHECK_INTERRUPTED" and INTERRUPTION in interruption["message"]
            assert initial["report"]["pending_rules"] == [RULE]
        else:
            assert interruption["code"] == "CHECK_CANCELLED"

        run, steps, artifacts = _execution(database, run_id)
        assert (run.plan_id, run.plan_version) == (draft.id, draft.version)
        assert len(steps) == 2 and all(step.status == "SUCCEEDED" and step.result.exit_code == 0 for step in steps)
        workdir = Path(run.workdir)
        original_files = _evidence(workdir, steps, artifacts)

        try:
            accepted = client.post(checks_endpoint)
            assert accepted.status_code == 202, accepted.text
            new_id = accepted.json()["report_id"]
            assert new_id != initial["id"]
            assert rechecking.wait(15), _get(client, endpoint)
            in_progress = _get(client, checks_endpoint)
            assert in_progress["check_status"] == "CHECKING"
            assert in_progress["current_report"] == initial and in_progress["report_ids"] == [initial["id"]]
            with ThreadPoolExecutor(max_workers=8) as pool:
                repeats = list(pool.map(lambda _: client.post(checks_endpoint), range(16)))
            for repeat in repeats:
                assert repeat.status_code == 202 and repeat.json()["report_id"] == new_id, repeat.text
        finally:
            finish_recheck.set()
        assert recheck_done.wait(15), _get(client, endpoint)

        state = _get(client, endpoint)
        assert (state["execution_status"], state["check_status"], state["current_report_id"]) == ("SUCCEEDED", "PASSED", new_id)
        assert state["error"] is None and state["cancel_requested"] is False
        current = _get(client, checks_endpoint)
        assert current["report_ids"] == [new_id, initial["id"]]
        report = current["current_report"]
        assert (report["id"], report["run_id"], report["status"]) == (new_id, run_id, "PASSED")
        assert report["rule_versions"] == initial["rule_versions"]
        assert report["report"]["pending_rules"] == [] and report["report"]["interruption"] is None
        assert report["report"]["rules"][RULE]["observed"] == ["point-inside"]
        history = _get(client, checks_endpoint, params={"report_id": initial["id"]})
        assert history["selected_report"] == initial and history["current_report"] == report

    checked_run, checked_steps, checked_artifacts = _execution(database, run_id)
    assert (checked_run.id, checked_run.plan_id, checked_run.plan_version, checked_run.workdir) == (
        run.id, run.plan_id, run.plan_version, run.workdir,
    )
    assert (checked_steps, checked_artifacts) == (steps, artifacts)
    assert _evidence(workdir, checked_steps, checked_artifacts) == original_files
    with open_database(database, read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 1


@pytest.mark.parametrize(("change", "code"), [
    ("readable_content", "ARTIFACT_CHANGED"),
    ("missing_file", "ARTIFACT_UNREADABLE"),
    ("rule_version", "CHECK_RULE_VERSION_MISMATCH"),
    ("finished_check", "RECHECK_NOT_AVAILABLE"),
])
def test_ineligible_recheck_preserves_original_report_without_dispatch(project, change, code):
    database, _, _, _, draft = project
    initial_check = _check_points if change == "finished_check" else _interrupt
    app, run_done, _ = _app(project, initial_check=initial_check)
    with TestClient(app) as client:
        run_id = _start_run(client, draft)
        endpoint = f"/api/runs/{run_id}"
        checks_endpoint = f"{endpoint}/checks"
        assert run_done.wait(15), _get(client, endpoint)
        original_state = _get(client, endpoint)
        original_checks = _get(client, checks_endpoint)
    expected_status = "PASSED" if change == "finished_check" else "NOT_CHECKED"
    assert (original_state["execution_status"], original_state["check_status"]) == ("SUCCEEDED", expected_status)
    run, steps, artifacts = _execution(database, run_id)
    artifact, = [item for item in artifacts if item.role == ROLE]
    output = Path(run.workdir) / artifact.uri
    if change == "readable_content":
        points = pyogrio.read_dataframe(output, layer=artifact.layer)
        pyogrio.write_dataframe(points.assign(source_feature_id="changed-point"), output, layer=artifact.layer, driver="GPKG")
        assert pyogrio.read_dataframe(output, layer=artifact.layer)["source_feature_id"].tolist() == ["changed-point"]
    elif change == "missing_file":
        output.unlink()

    version = RULE_VERSION + 1 if change == "rule_version" else RULE_VERSION
    app, _, recheck_done = _app(project, initial_check=_check_points, version=version)
    with TestClient(app) as client:
        response = client.post(checks_endpoint)
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == code
        if change == "readable_content":
            assert detail["field"] == f"artifacts.{ROLE}.fingerprint"
        assert _get(client, endpoint) == original_state
        assert _get(client, checks_endpoint) == original_checks
    assert not recheck_done.is_set()
    assert _execution(database, run_id) == (run, steps, artifacts)
    with open_database(database, read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM check_report WHERE run_id = ?", (run_id,)).fetchone()[0] == 1
