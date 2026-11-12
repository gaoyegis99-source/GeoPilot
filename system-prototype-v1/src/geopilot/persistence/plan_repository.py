"""Persist exact analysis plan versions in a caller-owned SQLite transaction."""

import sqlite3
from datetime import UTC, datetime

from geopilot.application.task_context import TaskContext
from geopilot.plans.models import AnalysisPlan, PlanStatus


def row_for_plan(connection: sqlite3.Connection, plan_id: str, version: int) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT id, version, task_id, status, plan_json FROM analysis_plan
           WHERE id = ? AND version = ?""",
        (plan_id, version),
    ).fetchone()


def plan_from_row(row: sqlite3.Row) -> AnalysisPlan:
    plan = AnalysisPlan.model_validate_json(row["plan_json"])
    if (plan.id, plan.version, plan.task_id, plan.status.value) != (
        row["id"], row["version"], row["task_id"], row["status"],
    ):
        raise ValueError("Stored plan identity or status differs from its snapshot")
    return plan


def insert_draft(
    connection: sqlite3.Connection, plan: AnalysisPlan, *, context: TaskContext | None = None,
) -> AnalysisPlan:
    """Insert a new version and its task under the caller's write transaction."""
    plan = AnalysisPlan.model_validate(plan)
    if plan.status is not PlanStatus.DRAFT:
        raise ValueError("Only draft plans can be inserted")
    if context is not None:
        context = TaskContext.model_validate(context)
        selected = {role: assessment.asset for role, assessment in context.roles.items() if assessment.asset is not None}
        if (context.task_id, context.goal, context.method, selected) != (plan.task_id, plan.goal, plan.method, plan.inputs):
            raise ValueError("The supplied context must match the draft task, goal, method and asset references")

    if row_for_plan(connection, plan.id, plan.version) is not None:
        raise ValueError(f"Plan {plan.id!r} version {plan.version} already exists")
    other_task = connection.execute(
        "SELECT 1 FROM analysis_plan WHERE id = ? AND task_id != ? LIMIT 1", (plan.id, plan.task_id),
    ).fetchone()
    if other_task is not None:
        raise ValueError(f"Plan {plan.id!r} belongs to another task")

    created_at = datetime.now(UTC).isoformat()
    connection.execute(
        """
        INSERT INTO task (id, goal_json, context_json, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            goal_json = excluded.goal_json,
            context_json = CASE
                WHEN excluded.context_json IS NOT NULL THEN excluded.context_json
                WHEN task.goal_json = excluded.goal_json THEN task.context_json
                ELSE NULL
            END
        """,
        (plan.task_id, plan.goal.model_dump_json(), None if context is None else context.model_dump_json(), created_at),
    )
    connection.execute(
        """
        INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at)
        VALUES (?, ?, ?, 'draft', ?, ?)
        """,
        (plan.id, plan.version, plan.task_id, plan.model_dump_json(), created_at),
    )
    return plan


def publish_confirmation(connection: sqlite3.Connection, plan: AnalysisPlan, *, draft_json: str) -> bool:
    """Replace only the exact draft that was checked before confirmation."""
    updated = connection.execute(
        """
        UPDATE analysis_plan SET status = 'confirmed', plan_json = ?
        WHERE id = ? AND version = ? AND task_id = ? AND status = 'draft' AND plan_json = ?
        """,
        (plan.model_dump_json(), plan.id, plan.version, plan.task_id, draft_json),
    )
    return updated.rowcount == 1
