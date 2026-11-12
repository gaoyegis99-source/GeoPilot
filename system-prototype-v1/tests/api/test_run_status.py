from datetime import UTC, datetime

from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import ExecutionStatus
from geopilot.persistence.run_repository import get_run, insert_run, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.runs.models import CheckStatus, Run
from geopilot.scheduler.loop import claim_next_run


def test_run_status_reports_committed_transitions_and_independent_checks(tmp_path):
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

    with TestClient(create_app(database)) as client:
        path = "/api/runs/test-run"
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {
            "run_id": "test-run", "execution_status": "NOT_STARTED", "check_status": "NOT_CHECKED",
            "current_step": None, "cancel_requested": False, "error": None, "current_report_id": None,
        }

        assert claim_next_run(database).id == "test-run"
        with open_database(database) as connection, connection:
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
        with open_database(database) as connection, connection:
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
