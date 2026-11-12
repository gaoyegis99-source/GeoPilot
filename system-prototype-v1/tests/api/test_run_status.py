import json
import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from geopilot.api import app as app_module
from geopilot.api.app import create_app
from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import ExecutionStatus
from geopilot.persistence.run_repository import get_run, insert_run, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.runs.models import CheckStatus, Run
from geopilot.scheduler.loop import claim_next_run


@pytest.fixture
def run_database(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    with open_database(database) as connection, connection:
        connection.execute("INSERT INTO task VALUES ('task', '{}', NULL, '2026-11-11')")
        connection.execute(
            "INSERT INTO analysis_plan VALUES ('plan', 1, 'task', 'confirmed', '{}', '2026-11-11')"
        )
        insert_run(connection, Run(
            id="test-run", plan_id="plan", plan_version=1, workdir=str(tmp_path / "test-run"),
            created_at=datetime(2026, 11, 11, tzinfo=UTC),
        ))
    return database


def test_run_status_reports_committed_transitions_and_independent_checks(run_database):
    with TestClient(create_app(run_database)) as client:
        path = "/api/runs/test-run"
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {
            "run_id": "test-run", "execution_status": "NOT_STARTED", "check_status": "NOT_CHECKED",
            "current_step": None, "cancel_requested": False, "error": None, "current_report_id": None,
        }

        assert claim_next_run(run_database).id == "test-run"
        with open_database(run_database) as connection, connection:
            run = get_run(connection, "test-run")
            update_run(connection, run.model_copy(update={"current_step": "clip", "cancel_requested": True}))
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {
            "run_id": "test-run", "execution_status": "RUNNING", "check_status": "NOT_CHECKED",
            "current_step": "clip", "cancel_requested": True, "error": None, "current_report_id": None,
        }

        error = ErrorDetail(
            code="CHECK_FAILED", phase="check", step_key="clip", field="area",
            message="Area exceeds expected range", suggestion="Review report evidence",
            log_path=None, observed={"area": 27.2},
        )
        with open_database(run_database) as connection, connection:
            run = get_run(connection, "test-run")
            update_run(connection, run.model_copy(update={
                "execution_status": ExecutionStatus.SUCCEEDED, "check_status": CheckStatus.FAILED,
                "cancel_requested": False, "current_report_id": "report-1", "error": error,
            }))
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {
            "run_id": "test-run", "execution_status": "SUCCEEDED", "check_status": "FAILED",
            "current_step": "clip", "cancel_requested": False, "error": error.model_dump(mode="json"),
            "current_report_id": "report-1",
        }


def test_run_status_distinguishes_missing_run_and_storage_error(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    with TestClient(create_app(database)) as client:
        response = client.get("/api/runs/unknown")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"

        database.unlink()
        response = client.get("/api/runs/unknown")
        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "STORAGE_FAILED"
        assert not database.exists()

        database.write_bytes(b"invalid SQLite file")
        response = client.get("/api/runs/unknown")
        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "STORAGE_FAILED"


def _status_failure_event(response, caplog):
    assert response.status_code == 500
    assert response.json() == {
        "detail": {"code": "STORAGE_FAILED", "message": "Could not read the run record."},
    }
    records = [record for record in caplog.records if record.name == "geopilot.api.app"]
    record, = records
    assert record.exc_info is None
    assert record.stack_info is None
    return json.loads(record.getMessage())


def test_run_status_logs_exclusive_sqlite_lock_and_recovers(run_database, caplog):
    with TestClient(create_app(run_database)) as client:
        with open_database(run_database) as writer, writer:
            assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            writer.execute("BEGIN EXCLUSIVE")
            event = _status_failure_event(client.get("/api/runs/test-run"), caplog)

        assert event.pop("elapsed_ms") >= 4_000
        assert event == {
            "event": "run_status_read_failed", "error_kind": "sqlite",
            "sqlite_primary_code": sqlite3.SQLITE_BUSY, "sqlite_extended_code": sqlite3.SQLITE_BUSY,
        }
        caplog.clear()

        recovered = client.get("/api/runs/test-run")
        assert recovered.status_code == 200
        assert recovered.json() == {
            "run_id": "test-run", "execution_status": "NOT_STARTED", "check_status": "NOT_CHECKED",
            "current_step": None, "cancel_requested": False, "error": None, "current_report_id": None,
        }
        missing = client.get("/api/runs/unknown")
        assert missing.status_code == 404
        assert missing.json() == {"detail": {"code": "RUN_NOT_FOUND", "message": "Run 'unknown' does not exist."}}
        assert not caplog.records


def test_run_status_logs_invalid_stored_model_without_input_values(run_database, caplog):
    invalid_error = json.dumps({
        "log_path": "/private/user-data.shp",
        "code": "SELECT private_data",
        "message": "private user input",
    })
    with open_database(run_database) as connection, connection:
        connection.execute("UPDATE run SET error_json = ? WHERE id = 'test-run'", (invalid_error,))

    with TestClient(create_app(run_database)) as client:
        event = _status_failure_event(client.get("/api/runs/test-run"), caplog)

    assert event.pop("elapsed_ms") >= 0
    assert event == {"event": "run_status_read_failed", "error_kind": "state_validation"}


def test_run_status_logs_distinct_primary_and_extended_sqlite_codes(run_database, caplog, monkeypatch):
    def fail_read(*args):
        error = sqlite3.OperationalError("private SQLite error details")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY_RECOVERY
        raise error

    monkeypatch.setattr(app_module, "get_run_status", fail_read)
    with TestClient(create_app(run_database)) as client:
        event = _status_failure_event(client.get("/api/runs/test-run"), caplog)

    assert event.pop("elapsed_ms") >= 0
    assert event == {
        "event": "run_status_read_failed", "error_kind": "sqlite",
        "sqlite_primary_code": sqlite3.SQLITE_BUSY, "sqlite_extended_code": sqlite3.SQLITE_BUSY_RECOVERY,
    }
