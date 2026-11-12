import sqlite3
from contextlib import closing

import pytest

from geopilot.persistence.schema import _MIGRATIONS, initialize_database, open_database


CREATED_AT = "2026-11-11T00:00:00Z"
ASSET_INSERT = """
    INSERT INTO data_asset (
        id, version, source_key, source_type, source_json, fingerprint,
        metadata_json, availability, created_at
    ) VALUES (?, ?, ?, 'local_file', '{"path":"sample.tif"}', ?, '{}', 'AVAILABLE', ?)
"""
PLAN_INSERT = """
    INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at)
    VALUES (?, ?, ?, ?, '{}', ?)
"""
RUN_INSERT = """
    INSERT INTO run (id, plan_id, plan_version, execution_status, check_status, workdir, created_at)
    VALUES (?, ?, ?, 'NOT_STARTED', 'NOT_CHECKED', 'tasks/task-1/runs/run-1', ?)
"""


def test_initialize_creates_v4_metadata_schema(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    assert initialize_database(path) == 4

    with open_database(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()
        assert isinstance(version, sqlite3.Row)
        assert version[0] == 4
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            row["name"] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        assert {"data_asset", "task", "analysis_plan", "run", "run_step", "artifact", "check_report"} <= tables

        asset_columns = {row["name"]: row["type"] for row in connection.execute("PRAGMA table_info(data_asset)")}
        assert asset_columns["source_json"] == asset_columns["metadata_json"] == "TEXT"
        assert "BLOB" not in asset_columns.values()
        run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(run)")}
        assert {"execution_status", "check_status", "workdir", "error_json",
                "cancel_requested", "current_step", "current_report_id"} <= run_columns
        report_columns = {row["name"] for row in connection.execute("PRAGMA table_info(check_report)")}
        assert {"id", "run_id", "status", "report_json", "rule_versions_json", "created_at"} <= report_columns


def test_asset_version_and_source_version_are_unique(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    initialize_database(path)

    with open_database(path) as connection:
        connection.execute(ASSET_INSERT, ("asset-1", 1, "sample.tif", "fingerprint-1", CREATED_AT))
        connection.execute(ASSET_INSERT, ("asset-1", 2, "sample.tif", "fingerprint-2", CREATED_AT))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(ASSET_INSERT, ("asset-1", 1, "another.tif", "duplicate", CREATED_AT))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(ASSET_INSERT, ("asset-2", 1, "sample.tif", "duplicate", CREATED_AT))
        for invalid_version in (0, 1.5):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(ASSET_INSERT, ("asset-3", invalid_version, "invalid.tif", "invalid", CREATED_AT))


def test_plan_and_run_foreign_keys_require_existing_versions(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    initialize_database(path)

    with open_database(path) as connection:
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES ('task-1', '{}', ?)", (CREATED_AT,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(PLAN_INSERT, ("orphan", 1, "missing-task", "draft", CREATED_AT))

        connection.execute(PLAN_INSERT, ("plan-1", 1, "task-1", "confirmed", CREATED_AT))
        connection.execute(PLAN_INSERT, ("plan-2", 2, "task-1", "confirmed", CREATED_AT))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(RUN_INSERT, ("orphan-run", "plan-1", 2, CREATED_AT))
        connection.execute(RUN_INSERT, ("run-1", "plan-1", 1, CREATED_AT))


def test_repeat_initialization_preserves_committed_rows(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    initialize_database(path)
    with open_database(path) as connection:
        connection.execute(ASSET_INSERT, ("asset-1", 1, "sample.tif", "fingerprint-1", CREATED_AT))
        connection.execute(ASSET_INSERT, ("asset-1", 2, "sample.tif", "fingerprint-2", CREATED_AT))
        connection.commit()

    assert initialize_database(path) == 4
    with open_database(path) as connection:
        versions = connection.execute("SELECT id, version, fingerprint FROM data_asset ORDER BY version").fetchall()
        assert [tuple(row) for row in versions] == [
            ("asset-1", 1, "fingerprint-1"),
            ("asset-1", 2, "fingerprint-2"),
        ]


def test_migration_conflict_rolls_back_schema_and_version(tmp_path):
    path = tmp_path / "conflicting.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE run (sentinel TEXT)")
        connection.execute("INSERT INTO run VALUES ('pre-existing')")
        connection.commit()

    with pytest.raises((sqlite3.DatabaseError, RuntimeError)):
        initialize_database(path)

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        tables = connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall()
        assert tables == [("run",)]
        assert connection.execute("SELECT sentinel FROM run").fetchall() == [("pre-existing",)]


def test_newer_schema_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 99")
        connection.execute("CREATE TABLE future_data (sentinel TEXT)")
        connection.execute("INSERT INTO future_data VALUES ('newer-schema')")
        connection.commit()

    with pytest.raises((ValueError, RuntimeError, sqlite3.DatabaseError)):
        initialize_database(path)

    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
        tables = connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall()
        assert tables == [("future_data",)]
        assert connection.execute("SELECT sentinel FROM future_data").fetchall() == [("newer-schema",)]


def test_open_database_closes_connection_without_committing(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    initialize_database(path)

    with open_database(path) as connection:
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES ('task-1', '{}', ?)", (CREATED_AT,))
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")

    with open_database(path) as reopened:
        assert reopened.execute("SELECT id FROM task").fetchall() == []


def _create_legacy_run(path):
    with open_database(path) as connection, connection:
        connection.executescript("""
            CREATE TABLE task (
                id TEXT NOT NULL PRIMARY KEY, goal_json TEXT NOT NULL,
                context_json TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE analysis_plan (
                id TEXT NOT NULL, version INTEGER NOT NULL, task_id TEXT NOT NULL REFERENCES task (id),
                status TEXT NOT NULL, plan_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (id, version)
            );
            CREATE TABLE run (
                id TEXT NOT NULL PRIMARY KEY, plan_id TEXT NOT NULL,
                plan_version INTEGER NOT NULL CHECK (typeof(plan_version) = 'integer' AND plan_version > 0),
                execution_status TEXT NOT NULL CHECK (
                    execution_status IN ('NOT_STARTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')
                ),
                check_status TEXT NOT NULL CHECK (
                    check_status IN ('NOT_CHECKED', 'CHECKING', 'PASSED', 'WARNING', 'FAILED')
                ),
                workdir TEXT NOT NULL CHECK (typeof(workdir) = 'text'),
                error_json TEXT CHECK (typeof(error_json) IN ('null', 'text')),
                created_at TEXT NOT NULL,
                FOREIGN KEY (plan_id, plan_version) REFERENCES analysis_plan (id, version)
            );
            PRAGMA user_version = 1;
        """)
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES ('task-1', '{}', ?)", (CREATED_AT,))
        connection.execute(PLAN_INSERT, ("plan-1", 2, "task-1", "confirmed", CREATED_AT))
        connection.execute(RUN_INSERT, ("run-1", "plan-1", 2, CREATED_AT))
        connection.execute("UPDATE run SET execution_status = 'SUCCEEDED', check_status = 'FAILED', error_json = ?",
                           ('{"code":"CHECK_FAILED"}',))
        return tuple(connection.execute("SELECT * FROM run").fetchone())


def test_v2_migration_preserves_legacy_rows_and_inserts(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    original = _create_legacy_run(path)

    assert initialize_database(path) == 4
    assert initialize_database(path) == 4

    with open_database(path) as connection, connection:
        row = connection.execute("SELECT * FROM run WHERE id = 'run-1'").fetchone()
        assert tuple(row) == original + (0, None, None)
        assert connection.execute("SELECT count(*) FROM run_step").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(RUN_INSERT, ("bad-version", "plan-1", 1, CREATED_AT))
        connection.execute(RUN_INSERT, ("legacy-insert", "plan-1", 2, CREATED_AT))
        defaults = connection.execute("SELECT cancel_requested, current_step, current_report_id FROM run WHERE id = 'legacy-insert'")
        assert tuple(defaults.fetchone()) == (0, None, None)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE run SET cancel_requested = 2 WHERE id = 'legacy-insert'")
        for position, status, request in ((-1, "NOT_STARTED", None), (0.5, "RUNNING", None),
                                          (0, "PASSED", None), (0, "NOT_STARTED", sqlite3.Binary(b"{}"))):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO run_step (run_id, step_key, position, status, request_json)
                       VALUES ('legacy-insert', 'buffer', ?, ?, ?)""", (position, status, request),
                )


def test_v2_migration_rolls_back_column_additions_on_conflict(tmp_path):
    path = tmp_path / "legacy-conflict.sqlite3"
    original = _create_legacy_run(path)
    with open_database(path) as connection, connection:
        connection.execute("CREATE TABLE run_step (sentinel TEXT)")
        connection.execute("INSERT INTO run_step VALUES ('pre-existing')")

    with pytest.raises(sqlite3.OperationalError):
        initialize_database(path)

    with open_database(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert tuple(connection.execute("SELECT * FROM run").fetchone()) == original
        assert connection.execute("SELECT sentinel FROM run_step").fetchone()[0] == "pre-existing"


def test_latest_schema_preserves_a_v2_run_step_and_its_plan_version(tmp_path):
    path = tmp_path / "legacy-v2.sqlite3"
    _create_legacy_run(path)
    with open_database(path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        for statement in _MIGRATIONS[1]:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")
        connection.execute("UPDATE run SET cancel_requested = 1, current_step = 'buffer', current_report_id = 'report-1'")
        connection.execute(
            """INSERT INTO run_step (run_id, step_key, position, status, log_path, started_at, finished_at)
               VALUES ('run-1', 'buffer', 0, 'SUCCEEDED', 'steps/buffer/process.log', ?, ?)""", (CREATED_AT, CREATED_AT),
        )
        previous_run = tuple(connection.execute("SELECT * FROM run").fetchone())
        previous_step = tuple(connection.execute("SELECT * FROM run_step").fetchone())

    assert initialize_database(path) == initialize_database(path) == 4
    with open_database(path) as connection:
        assert tuple(connection.execute("SELECT * FROM run").fetchone()) == previous_run
        assert tuple(connection.execute("SELECT * FROM run_step").fetchone()) == previous_step
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM artifact").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO artifact (id, run_id, role, uri, type, kind, format, fingerprint,
                                         metadata_json, source_json, created_at)
                   VALUES ('a-1', 'missing-run', 'roads', 'intermediate/roads.gpkg', 'vector_line',
                           'vector', 'GPKG', 'test', '{}', '{}', ?)""", (CREATED_AT,),
            )
