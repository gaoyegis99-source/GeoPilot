"""Open SQLite metadata connections and apply ordered schema migrations."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from os import PathLike
from pathlib import Path


# Tuple position plus one is the SQLite user_version of each migration.
_MIGRATIONS = (
    (
        """
        CREATE TABLE data_asset (
            id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (typeof(version) = 'integer' AND version > 0),
            source_key TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_json TEXT NOT NULL CHECK (typeof(source_json) = 'text'),
            fingerprint TEXT NOT NULL,
            metadata_json TEXT NOT NULL CHECK (typeof(metadata_json) = 'text'),
            availability TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, version),
            UNIQUE (source_key, version)
        )
        """,
        """
        CREATE TABLE task (
            id TEXT NOT NULL PRIMARY KEY,
            goal_json TEXT NOT NULL CHECK (typeof(goal_json) = 'text'),
            context_json TEXT CHECK (typeof(context_json) IN ('null', 'text')),
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE analysis_plan (
            id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (typeof(version) = 'integer' AND version > 0),
            task_id TEXT NOT NULL REFERENCES task (id),
            status TEXT NOT NULL CHECK (status IN ('draft', 'confirmed')),
            plan_json TEXT NOT NULL CHECK (typeof(plan_json) = 'text'),
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, version)
        )
        """,
        """
        CREATE TABLE run (
            id TEXT NOT NULL PRIMARY KEY,
            plan_id TEXT NOT NULL,
            plan_version INTEGER NOT NULL CHECK (
                typeof(plan_version) = 'integer' AND plan_version > 0
            ),
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
        )
        """,
    ),
    (
        """
        ALTER TABLE run ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0
            CHECK (typeof(cancel_requested) = 'integer' AND cancel_requested IN (0, 1))
        """,
        """
        ALTER TABLE run ADD COLUMN current_step TEXT CHECK (typeof(current_step) IN ('null', 'text'))
        """,
        """
        ALTER TABLE run ADD COLUMN current_report_id TEXT CHECK (typeof(current_report_id) IN ('null', 'text'))
        """,
        """
        CREATE TABLE run_step (
            run_id TEXT NOT NULL REFERENCES run (id),
            step_key TEXT NOT NULL,
            position INTEGER NOT NULL CHECK (typeof(position) = 'integer' AND position >= 0),
            status TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK (
                status IN ('NOT_STARTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')
            ),
            request_json TEXT CHECK (typeof(request_json) IN ('null', 'text')),
            result_json TEXT CHECK (typeof(result_json) IN ('null', 'text')),
            log_path TEXT CHECK (typeof(log_path) IN ('null', 'text')),
            started_at TEXT CHECK (typeof(started_at) IN ('null', 'text')),
            finished_at TEXT CHECK (typeof(finished_at) IN ('null', 'text')),
            PRIMARY KEY (run_id, step_key),
            UNIQUE (run_id, position)
        )
        """,
    ),
    (
        """
        CREATE TABLE artifact (
            id TEXT NOT NULL PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES run (id),
            role TEXT NOT NULL,
            uri TEXT NOT NULL CHECK (typeof(uri) = 'text'),
            layer TEXT CHECK (typeof(layer) IN ('null', 'text')),
            type TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('vector', 'raster', 'table', 'json')),
            format TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            metadata_json TEXT NOT NULL CHECK (typeof(metadata_json) = 'text'),
            source_json TEXT NOT NULL CHECK (typeof(source_json) = 'text'),
            created_at TEXT NOT NULL,
            UNIQUE (run_id, role)
        )
        """,
    ),
    (
        """
        CREATE TABLE check_report (
            id TEXT NOT NULL PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES run (id),
            status TEXT NOT NULL CHECK (
                status IN ('QUEUED', 'CHECKING', 'NOT_CHECKED', 'PASSED', 'WARNING', 'FAILED')
            ),
            report_json TEXT,
            rule_versions_json TEXT NOT NULL CHECK (typeof(rule_versions_json) = 'text'),
            created_at TEXT NOT NULL CHECK (typeof(created_at) = 'text'),
            CHECK (
                (status IN ('QUEUED', 'CHECKING') AND report_json IS NULL)
                OR (status IN ('NOT_CHECKED', 'PASSED', 'WARNING', 'FAILED') AND typeof(report_json) = 'text')
            )
        )
        """,
        "CREATE INDEX check_report_run_created ON check_report (run_id, created_at, id)",
    ),
)


@contextmanager
def open_database(path: str | PathLike[str], *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a connection with row access by name and caller-managed transactions."""
    target = Path(path).absolute().as_uri() + "?mode=ro" if read_only else path
    connection = sqlite3.connect(target, isolation_level="DEFERRED", uri=read_only)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    finally:
        connection.close()


def initialize_database(path: str | PathLike[str]) -> int:
    """Apply all pending migrations atomically and return the schema version."""
    current_version = len(_MIGRATIONS)
    with open_database(path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        stored_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= stored_version <= current_version:
            raise RuntimeError(
                f"Unsupported database schema version {stored_version}; "
                f"supported versions are 0 through {current_version}"
            )

        for version, statements in enumerate(
            _MIGRATIONS[stored_version:], start=stored_version + 1
        ):
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {version}")

    return current_version
