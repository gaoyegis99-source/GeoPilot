from datetime import UTC, datetime

from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.persistence.run_repository import insert_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.runs.models import Run


def test_run_origin_joins_the_exact_plan_version_without_creating_a_run(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    created_at = datetime(2026, 11, 11, tzinfo=UTC)
    with open_database(database) as connection, connection:
        connection.executemany(
            "INSERT INTO task (id, goal_json, created_at) VALUES (?, '{}', ?)",
            [("historical-task", created_at.isoformat()), ("newer-task", created_at.isoformat())],
        )
        connection.executemany(
            "INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at) "
            "VALUES ('shared-plan', ?, ?, 'confirmed', '{}', ?)",
            [(1, "historical-task", created_at.isoformat()), (2, "newer-task", created_at.isoformat())],
        )
        insert_run(connection, Run(
            id="historical-run", plan_id="shared-plan", plan_version=1,
            workdir=str(tmp_path / "historical-run"), created_at=created_at,
        ))
        insert_run(connection, Run(
            id="newer-run", plan_id="shared-plan", plan_version=2,
            workdir=str(tmp_path / "newer-run"), created_at=created_at,
        ))
    original = database.read_bytes()
    with TestClient(create_app(database)) as client:
        for _ in range(2):
            response = client.get("/api/runs/historical-run/origin")
            assert response.status_code == 200
            assert response.json() == {
                "run_id": "historical-run", "task_id": "historical-task",
                "plan_id": "shared-plan", "plan_version": 1,
            }
        response = client.get("/api/runs/newer-run/origin")
        assert response.status_code == 200
        assert response.json() == {
            "run_id": "newer-run", "task_id": "newer-task", "plan_id": "shared-plan", "plan_version": 2,
        }
    assert database.read_bytes() == original


def test_run_origin_distinguishes_unknown_run_and_unavailable_storage(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    with TestClient(create_app(database)) as client:
        endpoint = "/api/runs/unknown/origin"
        response = client.get(endpoint)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"

        database.unlink()
        response = client.get(endpoint)
        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "STORAGE_FAILED"
        assert not database.exists()

        database.write_bytes(b"invalid SQLite file")
        response = client.get(endpoint)
        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "STORAGE_FAILED"
