import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from geopilot.checks.report_service import CheckReport, get_current_report, list_report_ids, publish_report, summarize_check_status
from geopilot.domain.errors import ErrorDetail
from geopilot.persistence.run_repository import get_run, insert_run, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.runs.models import CheckStatus, Run


START = datetime(2026, 11, 11, tzinfo=UTC)
RUN_ID = "run-report"


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "metadata.sqlite"
    assert initialize_database(path) == 4
    with open_database(path) as connection, connection:
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES ('task', '{}', ?)", (START.isoformat(),))
        connection.execute("""
            INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at)
            VALUES ('plan', 1, 'task', 'confirmed', '{}', ?)
        """, (START.isoformat(),))
        insert_run(connection, Run(
            id=RUN_ID, plan_id="plan", plan_version=1, workdir=str(tmp_path / "run"), created_at=START,
            execution_status="SUCCEEDED", check_status="CHECKING", current_step="filter",
        ))
    return path


def _report(report_id, status, *, offset=1, interruption=None):
    return CheckReport(
        id=report_id, run_id=RUN_ID, status=status, created_at=START + timedelta(seconds=offset),
        rule_versions={"feature_ids": 2, "output_exists": 1},
        report={
            "run_id": RUN_ID, "status": status,
            "rules": {"feature_ids": {"passed": False, "observed": ["building-west"], "expected": ["building-east"]}},
            "completed_rules": ["feature_ids"], "pending_rules": ["output_exists"] if interruption else [],
            "interruption": interruption,
        },
    )


@pytest.mark.parametrize(("findings", "interrupted", "status"), [
    ([{"passed": True, "severity": "blocking"}, {"passed": True, "severity": "warning"}], False, CheckStatus.PASSED),
    ([{"passed": True, "severity": "blocking"}, {"passed": False, "severity": "warning"}], False, CheckStatus.WARNING),
    ([{"passed": False, "severity": "blocking"}, {"passed": False, "severity": "warning"}], False, CheckStatus.FAILED),
    ([{"passed": False, "severity": "blocking"}], True, CheckStatus.NOT_CHECKED),
])
def test_severity_summary_uses_completed_findings_and_prioritizes_interruption(findings, interrupted, status):
    assert summarize_check_status(findings, interrupted=interrupted) is status


@pytest.mark.parametrize("finding", [{"passed": 1, "severity": "warning"}, {"passed": False, "severity": "error"}])
def test_severity_summary_rejects_invalid_json_findings(finding):
    with pytest.raises(ValueError):
        summarize_check_status([finding])


def test_report_and_run_pointer_are_visible_together_after_commit(database):
    report = _report("report-failed", "FAILED")
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        assert get_current_report(connection, RUN_ID) is None and list_report_ids(connection, RUN_ID) == ()
        assert publish_report(connection, report) == report
        assert get_current_report(connection, RUN_ID) == report
        with open_database(database, read_only=True) as reader:
            run = get_run(reader, RUN_ID)
            assert run.check_status is CheckStatus.CHECKING and run.current_report_id is None
            assert get_current_report(reader, RUN_ID) is None and list_report_ids(reader, RUN_ID) == ()

    with open_database(database, read_only=True) as connection:
        run = get_run(connection, RUN_ID)
        assert (run.execution_status, run.check_status, run.current_report_id) == ("SUCCEEDED", "FAILED", report.id)
        assert run.error is None and run.current_step == "filter" and run.plan_version == 1
        assert get_current_report(connection, RUN_ID) == report
        assert list_report_ids(connection, RUN_ID) == (report.id,)


def test_interrupted_publication_preserves_history_and_excludes_queued_rows(database):
    first = _report("first", "FAILED")
    reason = {"code": "CHECK_INTERRUPTED", "field": "check_rules.1", "message": "Checker stopped"}
    final = _report("interrupted", "NOT_CHECKED", offset=2, interruption=reason)
    error = ErrorDetail(
        code="CHECK_INTERRUPTED", phase="check", step_key="filter", field="check_rules.1",
        message="Checker stopped", suggestion="Review the pending checks.", log_path=None,
    )
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        publish_report(connection, first)
        checking = get_run(connection, RUN_ID).model_copy(update={"check_status": CheckStatus.CHECKING})
        update_run(connection, checking)
        publish_report(connection, final, error=error)
        connection.execute("""
            INSERT INTO check_report (id, run_id, status, report_json, rule_versions_json, created_at)
            VALUES ('queued', ?, 'QUEUED', NULL, '{}', ?)
        """, (RUN_ID, (START + timedelta(seconds=3)).isoformat()))

    with open_database(database, read_only=True) as connection:
        run = get_run(connection, RUN_ID)
        assert (run.execution_status, run.check_status, run.current_report_id) == ("SUCCEEDED", "NOT_CHECKED", final.id)
        assert run.error == error
        assert get_current_report(connection, RUN_ID) == final
        assert list_report_ids(connection, RUN_ID) == (final.id, first.id)
        old = connection.execute("SELECT status, report_json, rule_versions_json FROM check_report WHERE id = ?", (first.id,)).fetchone()
        assert old["status"] == "FAILED" and json.loads(old["report_json"]) == first.report
        assert json.loads(old["rule_versions_json"]) == first.rule_versions


def test_sqlite_failure_after_report_insert_rolls_back_even_when_caller_commits(database):
    first = _report("previous", "FAILED")
    blocked = _report("blocked", "WARNING", offset=2)
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        publish_report(connection, first)
        update_run(connection, get_run(connection, RUN_ID).model_copy(update={"check_status": CheckStatus.CHECKING}))
        connection.execute("""
            CREATE TRIGGER refuse_current_report BEFORE UPDATE OF current_report_id ON run
            WHEN NEW.current_report_id = 'blocked'
            BEGIN SELECT RAISE(ABORT, 'refuse pointer update'); END
        """)
        with pytest.raises(sqlite3.IntegrityError, match="refuse pointer update"):
            publish_report(connection, blocked)
        assert connection.in_transaction
        assert get_current_report(connection, RUN_ID) == first
        assert list_report_ids(connection, RUN_ID) == (first.id,)

    with open_database(database, read_only=True) as connection:
        run = get_run(connection, RUN_ID)
        assert (run.execution_status, run.check_status, run.current_report_id) == ("SUCCEEDED", "CHECKING", first.id)
        assert connection.execute("SELECT count(*) FROM check_report WHERE id = ?", (blocked.id,)).fetchone()[0] == 0


def test_requires_active_transaction_and_final_report_ids_cannot_be_reused(database):
    first = _report("fixed-report", "FAILED")
    with open_database(database) as connection:
        with pytest.raises(ValueError, match="transaction"):
            publish_report(connection, first)
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            publish_report(connection, first)
            with pytest.raises(ValueError, match="CHECKING"):
                publish_report(connection, _report("unclaimed", "PASSED"))
            update_run(connection, get_run(connection, RUN_ID).model_copy(update={"check_status": CheckStatus.CHECKING}))
            with pytest.raises(sqlite3.IntegrityError):
                publish_report(connection, _report(first.id, "PASSED", offset=2))
        assert get_current_report(connection, RUN_ID) == first
        assert list_report_ids(connection, RUN_ID) == (first.id,)
