import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from shutil import copyfile
from threading import Barrier

import pyogrio
import pytest

from geopilot.artifacts.service import insert_artifacts, prepare_artifacts
from geopilot.checks.recheck_service import (
    RecheckError,
    claim_recheck,
    get_active_check_attempt,
    request_recheck,
    reserve_check_attempt,
    verify_recheck_artifacts,
)
from geopilot.checks.report_service import CheckReport, get_current_report, get_report, list_report_ids, publish_report
from geopilot.data.assets import register_asset
from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import StepResult
from geopilot.persistence.run_repository import get_run, insert_run, save_run_step, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.plans.models import AnalysisPlan
from geopilot.runs.models import Run, RunStep


SOURCE = Path(__file__).resolve().parents[1] / "fixtures/vector/road-buffer.gpkg"
RUN = "run-recheck"
VERSIONS = {"output_exists": 2, "feature_count": 4}
STAMP = "2026-11-11T00:00:00Z"


def _report(attempt, status, *, reason=None):
    return CheckReport(
        id=attempt.id, run_id=attempt.run_id, status=status,
        report={"run_id": attempt.run_id, "status": status, "interruption": reason},
        rule_versions=attempt.rule_versions, created_at=attempt.created_at,
    )


@pytest.fixture
def interrupted_run(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    workspace = create_run_workspace(tmp_path / "workspace", "task", RUN)
    source = tmp_path / "source.gpkg"
    copyfile(SOURCE, source)
    asset = register_asset(database, source, layer="targets")
    reference = "intermediate/filtered.gpkg"
    output = workspace.path / reference
    pyogrio.write_dataframe(pyogrio.read_dataframe(source, layer="targets", max_features=1), output, layer="filtered")
    plan = AnalysisPlan.model_validate({
        "id": "plan", "version": 2, "task_id": "task", "status": "confirmed", "goal": {"summary": "Recheck saved points"},
        "inputs": {"features": asset.ref}, "method": {"id": "attribute-filter-export", "version": 1}, "critical_options": {},
        "steps": [{"key": "filter", "tool": {"id": "qgis-extract-by-attribute", "version": 1}, "parameters": {},
                   "inputs": {"source": {"asset": "features"}},
                   "outputs": {"filtered": {"target": reference, "layer": "filtered", "type": "vector_point"}}}],
        "expected_artifacts": [{"role": "filtered", "step": "filter", "output": "filtered", "type": "vector_point"}],
        "check_rules": list(VERSIONS), "issues": [],
    })
    result = StepResult(
        run_id=RUN, step_key="filter", status="SUCCEEDED", exit_code=0,
        outputs={"filtered": {"path": reference, "layer": "filtered", "type": "vector_point"}},
        log_path="steps/filter/process.log", warnings=[], error=None,
        started_at=STAMP, finished_at=STAMP, environment={},
    )
    reason = {"code": "CHECK_INTERRUPTED", "field": "check_rules.1", "message": "Checker stopped"}
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES ('task', '{}', ?)", (STAMP,))
        connection.execute("""
            INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at)
            VALUES ('plan', 2, 'task', 'confirmed', ?, ?)
        """, (plan.model_dump_json(), STAMP))
        run = insert_run(connection, Run(id=RUN, plan_id=plan.id, plan_version=plan.version, workdir=str(workspace.path),
                                         created_at=STAMP, execution_status="RUNNING"))
        save_run_step(connection, RunStep(run_id=RUN, step_key="filter", position=0, status=result.status, result=result))
        insert_artifacts(connection, prepare_artifacts(RUN, plan, workspace.path, {"filter": result}))
        update_run(connection, run.model_copy(update={
            "execution_status": "SUCCEEDED", "check_status": "CHECKING", "cancel_requested": True,
        }))
        initial = reserve_check_attempt(connection, RUN, VERSIONS)
        assert get_active_check_attempt(connection, RUN) == initial
        previous = _report(initial, "NOT_CHECKED", reason=reason)
        publish_report(connection, previous, error=ErrorDetail(
            **reason, phase="check", step_key="filter", suggestion="Restart the checker", log_path=None,
        ))
    return database, output, source, previous


def test_request_claim_and_publish_keep_the_previous_report_and_calculation(interrupted_run):
    database, output, source, previous = interrupted_run
    original_bytes = output.read_bytes()
    source.unlink()
    queued = request_recheck(database, RUN, rule_versions={**VERSIONS, "other_method_rule": 1})
    assert request_recheck(database, RUN, rule_versions=VERSIONS) == queued != previous.id
    with open_database(database, read_only=True) as connection:
        run = get_run(connection, RUN)
        active = get_active_check_attempt(connection, RUN)
        assert (active.id, active.run_id, active.rule_versions) == (queued, RUN, VERSIONS)
        assert (run.execution_status, run.check_status, run.current_report_id, run.cancel_requested) == (
            "SUCCEEDED", "NOT_CHECKED", previous.id, True,
        )
        assert get_current_report(connection, RUN) == previous
        assert get_report(connection, RUN, queued) is None
        assert list_report_ids(connection, RUN) == (previous.id,)
        before_steps = connection.execute("SELECT * FROM run_step WHERE run_id = ?", (RUN,)).fetchall()

    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        assert claim_recheck(connection, queued) == active
        run = get_run(connection, RUN)
        assert (run.execution_status, run.check_status, run.current_report_id, run.cancel_requested) == (
            "SUCCEEDED", "CHECKING", previous.id, False,
        )
        assert get_current_report(connection, RUN) == previous

    assert request_recheck(database, RUN, rule_versions=VERSIONS) == queued
    verify_recheck_artifacts(database, RUN)
    final = _report(active, "PASSED")
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        assert publish_report(connection, final) == final

    with open_database(database, read_only=True) as connection:
        run = get_run(connection, RUN)
        assert (run.execution_status, run.check_status, run.current_report_id) == ("SUCCEEDED", "PASSED", queued)
        assert get_current_report(connection, RUN) == final
        assert get_report(connection, RUN, previous.id) == previous
        assert get_report(connection, "another-run", queued) is None
        assert list_report_ids(connection, RUN) == (queued, previous.id)
        assert get_active_check_attempt(connection, RUN) is None
        assert connection.execute("SELECT * FROM run_step WHERE run_id = ?", (RUN,)).fetchall() == before_steps
    assert output.read_bytes() == original_bytes
    assert len(pyogrio.read_dataframe(output, layer="filtered")) == 1


@pytest.mark.parametrize("runtime_versions", [
    {"output_exists": 2}, {"output_exists": 2, "feature_count": 5}, {"output_exists": 2, "feature_count": 4.0},
])
def test_request_requires_exact_frozen_rule_versions(interrupted_run, runtime_versions):
    database, _, _, previous = interrupted_run
    with pytest.raises(RecheckError) as failure:
        request_recheck(database, RUN, rule_versions=runtime_versions)
    assert (failure.value.code, failure.value.status_code, failure.value.field) == (
        "CHECK_RULE_VERSION_MISMATCH", 409, "rule_versions.feature_count",
    )
    with open_database(database, read_only=True) as connection:
        assert list_report_ids(connection, RUN) == (previous.id,)
        assert get_active_check_attempt(connection, RUN) is None


@pytest.mark.parametrize("state", ["unknown_run", "no_report", "passed", "failed_execution"])
def test_only_interrupted_successful_runs_with_original_report_can_queue(interrupted_run, state):
    database, _, _, _ = interrupted_run
    run_id = "missing" if state == "unknown_run" else RUN
    with open_database(database) as connection, connection:
        if state == "no_report":
            connection.execute("UPDATE run SET current_report_id = NULL WHERE id = ?", (RUN,))
        elif state == "passed":
            connection.execute("UPDATE run SET check_status = 'PASSED' WHERE id = ?", (RUN,))
        elif state == "failed_execution":
            connection.execute("UPDATE run SET execution_status = 'FAILED' WHERE id = ?", (RUN,))
    with pytest.raises(RecheckError) as failure:
        request_recheck(database, run_id, rule_versions=VERSIONS)
    assert (failure.value.code, failure.value.status_code) == (
        ("RUN_NOT_FOUND", 404) if state == "unknown_run" else ("RECHECK_NOT_AVAILABLE", 409)
    )
    with open_database(database, read_only=True) as connection:
        assert get_active_check_attempt(connection, RUN) is None


@pytest.mark.parametrize(("change", "code", "field"), [
    ("readable_edit", "ARTIFACT_CHANGED", "artifacts.filtered.fingerprint"),
    ("missing", "ARTIFACT_UNREADABLE", "artifacts.filtered.path"),
])
def test_actual_artifact_changed_or_missing_rejects_enqueue(interrupted_run, change, code, field):
    database, output, _, previous = interrupted_run
    if change == "missing":
        output.unlink()
    else:
        frame = pyogrio.read_dataframe(output, layer="filtered")
        pyogrio.write_dataframe(frame, output, layer="filtered", append=True)
        assert len(pyogrio.read_dataframe(output, layer="filtered")) == 2

    with pytest.raises(RecheckError) as failure:
        request_recheck(database, RUN, rule_versions=VERSIONS)
    assert (failure.value.code, failure.value.status_code, failure.value.field) == (code, 409, field)
    with open_database(database, read_only=True) as connection:
        assert get_active_check_attempt(connection, RUN) is None
        assert get_current_report(connection, RUN) == previous


def test_claimed_attempt_can_recheck_digest_before_evaluating_rules(interrupted_run):
    database, output, _, previous = interrupted_run
    queued = request_recheck(database, RUN, rule_versions=VERSIONS)
    frame = pyogrio.read_dataframe(output, layer="filtered")
    pyogrio.write_dataframe(frame, output, layer="filtered", append=True)
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        assert claim_recheck(connection, queued).id == queued
    with pytest.raises(RecheckError) as failure:
        verify_recheck_artifacts(database, RUN)
    assert failure.value.code == "ARTIFACT_CHANGED"
    with open_database(database, read_only=True) as connection:
        assert get_current_report(connection, RUN) == previous


def test_concurrent_requests_create_one_queued_attempt(interrupted_run):
    database, _, _, previous = interrupted_run
    gate = Barrier(4, timeout=10)

    def enqueue():
        gate.wait()
        return request_recheck(database, RUN, rule_versions=VERSIONS)

    with ThreadPoolExecutor(max_workers=4) as pool:
        queued = [future.result(timeout=20) for future in [pool.submit(enqueue) for _ in range(4)]]
    assert len(set(queued)) == 1
    with open_database(database, read_only=True) as connection:
        assert [tuple(row) for row in connection.execute("SELECT id, status FROM check_report ORDER BY created_at")] == [
            (previous.id, "NOT_CHECKED"), (queued[0], "QUEUED"),
        ]


def test_rejected_reservation_does_not_return_an_unpublished_report_id(interrupted_run):
    database, _, _, previous = interrupted_run
    with open_database(database) as connection, connection:
        connection.execute("""
            CREATE TRIGGER ignore_queue_insert BEFORE INSERT ON check_report
            WHEN NEW.status = 'QUEUED' BEGIN SELECT RAISE(IGNORE); END
        """)
    with pytest.raises(sqlite3.IntegrityError):
        request_recheck(database, RUN, rule_versions=VERSIONS)
    with open_database(database, read_only=True) as connection:
        assert get_active_check_attempt(connection, RUN) is None
        assert get_current_report(connection, RUN) == previous


def test_failed_claim_keeps_the_attempt_queued_even_when_caller_commits(interrupted_run):
    database, _, _, previous = interrupted_run
    queued = request_recheck(database, RUN, rule_versions=VERSIONS)
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TRIGGER reject_checking BEFORE UPDATE OF check_status ON run
            WHEN NEW.check_status = 'CHECKING' BEGIN SELECT RAISE(ABORT, 'test claim rejected'); END
        """)
        with pytest.raises(sqlite3.IntegrityError, match="test claim rejected"):
            claim_recheck(connection, queued)
    with open_database(database, read_only=True) as connection:
        assert connection.execute("SELECT status FROM check_report WHERE id = ?", (queued,)).fetchone()[0] == "QUEUED"
        assert get_run(connection, RUN).check_status == "NOT_CHECKED"
        assert get_current_report(connection, RUN) == previous


def test_reserved_report_requires_exact_identity_and_can_only_finalize_once(interrupted_run):
    database, _, _, previous = interrupted_run
    queued = request_recheck(database, RUN, rule_versions=VERSIONS)
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        active = get_active_check_attempt(connection, RUN)
        with pytest.raises(ValueError, match="CHECKING"):
            publish_report(connection, _report(active, "PASSED"))
        claim_recheck(connection, queued)
        final = _report(active, "PASSED")
        for changed in (
            {"id": "another-id"}, {"rule_versions": {"output_exists": 2, "feature_count": 5}},
            {"created_at": active.created_at + timedelta(seconds=1)},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                publish_report(connection, final.model_copy(update=changed))
        assert get_current_report(connection, RUN) == previous
        assert get_active_check_attempt(connection, RUN) == active
        publish_report(connection, final)
        run = get_run(connection, RUN)
        update_run(connection, run.model_copy(update={"check_status": "CHECKING"}))
        with pytest.raises(sqlite3.IntegrityError):
            publish_report(connection, final.model_copy(update={
                "status": "WARNING", "report": {**final.report, "status": "WARNING"},
            }))
        assert get_current_report(connection, RUN) == final


def test_reservation_remains_active_when_pointer_update_fails(interrupted_run):
    database, _, _, previous = interrupted_run
    queued = request_recheck(database, RUN, rule_versions=VERSIONS)
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        attempt = claim_recheck(connection, queued)
        connection.execute("""
            CREATE TRIGGER reject_final_pointer BEFORE UPDATE OF current_report_id ON run
            BEGIN SELECT RAISE(ABORT, 'test report pointer rejected'); END
        """)
        with pytest.raises(sqlite3.IntegrityError, match="test report pointer rejected"):
            publish_report(connection, _report(attempt, "PASSED"))
        assert connection.in_transaction
    with open_database(database) as connection, connection:
        assert get_current_report(connection, RUN) == previous
        assert get_active_check_attempt(connection, RUN) == attempt
        assert get_report(connection, RUN, attempt.id) is None
