"""Read and write run records through caller-owned SQLite transactions."""

import sqlite3

from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import StepRequest, StepResult
from geopilot.runs.models import Run, RunStep


def _run_from_row(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"], plan_id=row["plan_id"], plan_version=row["plan_version"], workdir=row["workdir"],
        created_at=row["created_at"], execution_status=row["execution_status"], check_status=row["check_status"],
        cancel_requested=bool(row["cancel_requested"]), current_step=row["current_step"],
        current_report_id=row["current_report_id"],
        error=None if row["error_json"] is None else ErrorDetail.model_validate_json(row["error_json"]),
    )


def _run_values(run: Run) -> dict:
    return {
        **run.model_dump(mode="json", exclude={"error"}),
        "error_json": None if run.error is None else run.error.model_dump_json(),
    }


def insert_run(connection: sqlite3.Connection, run: Run) -> Run:
    """Insert a run bound to one exact plan version."""
    run = Run.model_validate(run)
    connection.execute(
        """
        INSERT INTO run (
            id, plan_id, plan_version, workdir, created_at, execution_status, check_status,
            cancel_requested, current_step, current_report_id, error_json
        ) VALUES (
            :id, :plan_id, :plan_version, :workdir, :created_at, :execution_status, :check_status,
            :cancel_requested, :current_step, :current_report_id, :error_json
        )
        """,
        _run_values(run),
    )
    return run


def get_run(connection: sqlite3.Connection, run_id: str) -> Run | None:
    row = connection.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    return None if row is None else _run_from_row(row)


def update_run(connection: sqlite3.Connection, run: Run) -> Run:
    """Replace lifecycle fields after reading the run in the caller's write transaction."""
    run = Run.model_validate(run)
    stored = get_run(connection, run.id)
    if stored is None:
        raise LookupError(f"Run {run.id!r} does not exist")
    if (stored.plan_id, stored.plan_version, stored.workdir, stored.created_at) != (
        run.plan_id, run.plan_version, run.workdir, run.created_at,
    ):
        raise ValueError("The stored run identity cannot be changed")
    connection.execute(
        """
        UPDATE run SET execution_status = :execution_status, check_status = :check_status,
            cancel_requested = :cancel_requested, current_step = :current_step,
            current_report_id = :current_report_id, error_json = :error_json
        WHERE id = :id
        """,
        _run_values(run),
    )
    return run


def _step_from_row(row: sqlite3.Row) -> RunStep:
    return RunStep(
        run_id=row["run_id"], step_key=row["step_key"], position=row["position"], status=row["status"],
        log_path=row["log_path"], started_at=row["started_at"], finished_at=row["finished_at"],
        request=None if row["request_json"] is None else StepRequest.model_validate_json(row["request_json"]),
        result=None if row["result_json"] is None else StepResult.model_validate_json(row["result_json"]),
    )


def save_run_step(connection: sqlite3.Connection, step: RunStep) -> RunStep:
    """Save a step record while preserving its position within the run."""
    step = RunStep.model_validate(step)
    values = step.model_dump(mode="json", exclude={"request", "result"})
    values["request_json"] = None if step.request is None else step.request.model_dump_json()
    values["result_json"] = None if step.result is None else step.result.model_dump_json()
    saved = connection.execute(
        """
        INSERT INTO run_step (
            run_id, step_key, position, status, request_json, result_json, log_path, started_at, finished_at
        ) VALUES (
            :run_id, :step_key, :position, :status, :request_json, :result_json, :log_path, :started_at, :finished_at
        )
        ON CONFLICT (run_id, step_key) DO UPDATE SET
            status = excluded.status, request_json = excluded.request_json, result_json = excluded.result_json,
            log_path = excluded.log_path, started_at = excluded.started_at, finished_at = excluded.finished_at
        WHERE run_step.position = excluded.position
        """,
        values,
    )
    if saved.rowcount != 1:
        raise ValueError("The stored step position cannot be changed")
    return step


def get_run_step(connection: sqlite3.Connection, run_id: str, step_key: str) -> RunStep | None:
    row = connection.execute(
        "SELECT * FROM run_step WHERE run_id = ? AND step_key = ?", (run_id, step_key),
    ).fetchone()
    return None if row is None else _step_from_row(row)


def list_run_steps(connection: sqlite3.Connection, run_id: str) -> tuple[RunStep, ...]:
    rows = connection.execute("SELECT * FROM run_step WHERE run_id = ? ORDER BY position", (run_id,))
    return tuple(_step_from_row(row) for row in rows)
