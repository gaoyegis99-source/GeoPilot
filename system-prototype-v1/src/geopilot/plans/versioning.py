"""Revise a plan without rewriting its prior snapshots or run references."""

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue

from geopilot.application.task_context import TaskContext
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.plan_repository import insert_draft, plan_from_row, row_for_plan
from geopilot.persistence.schema import open_database
from geopilot.plans.compiler import _same_json
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.models import AnalysisPlan, PlanStatus
from geopilot.tools.registry import ToolRegistry


_EDITABLE_FIELDS = {"goal", "inputs", "critical_options", "method"}


class PlanVersionConflictError(ValueError):
    code = "PLAN_VERSION_CONFLICT"
    field = "version"

    def __init__(self, plan_id: str, version: int, latest_version: int) -> None:
        self.plan_id = plan_id
        self.version = version
        self.latest_version = latest_version
        self.message = f"Plan {plan_id!r} version {version} is stale. Revise version {latest_version}."
        super().__init__(self.message)


def _refresh_current_context(connection: sqlite3.Connection, plan: AnalysisPlan, context: TaskContext) -> None:
    if context.criteria is None:
        return
    connection.execute(
        """
        UPDATE task SET goal_json = ?, context_json = ? WHERE id = ? AND (?, ?) = (
            SELECT id, version FROM analysis_plan WHERE task_id = ?
            ORDER BY created_at DESC, version DESC, id LIMIT 1
        )
        """,
        (context.goal.model_dump_json(), context.model_dump_json(), plan.task_id, plan.id, plan.version, plan.task_id),
    )


def revise_plan(
    database_path: str | Path,
    plan_id: str,
    version: int,
    *,
    context: TaskContext,
    method: MethodDefinition,
    options: Mapping[str, JsonValue] | None = None,
    registry: ToolRegistry | None = None,
) -> AnalysisPlan:
    """Build an edited draft against an exact baseline and allocate its next version."""
    if not isinstance(plan_id, str) or not plan_id.strip() or type(version) is not int or version < 1:
        raise ValueError("An exact plan ID and positive integer version are required")
    context = TaskContext.model_validate(context)
    method = MethodDefinition.model_validate(method)

    with open_database(database_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = row_for_plan(connection, plan_id, version)
        if row is None:
            raise LookupError(f"Plan {plan_id!r} version {version} does not exist")
        baseline = plan_from_row(row)
        if context.task_id != baseline.task_id:
            raise ValueError("A revision must belong to the original task")

        latest_version = connection.execute(
            "SELECT max(version) FROM analysis_plan WHERE id = ?", (plan_id,),
        ).fetchone()[0]
        latest = baseline
        if version != latest_version:
            latest = plan_from_row(row_for_plan(connection, plan_id, latest_version))
        if options is None and method.ref == baseline.method:
            options = baseline.critical_options
        candidate = build_draft(
            context, method, plan_id=plan_id, version=latest_version + 1, options=options, registry=registry,
        )

        if latest.status is PlanStatus.DRAFT and _same_json(
            latest.model_dump(mode="json", exclude={"version"}), candidate.model_dump(mode="json", exclude={"version"}),
        ):
            if version == latest_version:
                _refresh_current_context(connection, latest, context)
            return latest
        if version != latest_version:
            raise PlanVersionConflictError(plan_id, version, latest_version)
        if latest.status is PlanStatus.CONFIRMED and not candidate.issues:
            prior_rules = {step.key: step.tool_check_rules for step in latest.steps if step.tool_check_rules is not None}
            rules_unchanged = registry is None or all(
                step.key not in prior_rules or step.tool_check_rules == prior_rules[step.key] for step in candidate.steps
            )
            if rules_unchanged and _same_json(
                latest.model_dump(mode="json", include=_EDITABLE_FIELDS),
                candidate.model_dump(mode="json", include=_EDITABLE_FIELDS),
            ):
                _refresh_current_context(connection, latest, context)
                return latest
        return insert_draft(connection, candidate, context=context)
