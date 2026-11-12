"""Revise a plan without rewriting its prior snapshots or run references."""

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue

from geopilot.application.task_context import TaskContext
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.plan_repository import insert_draft, plan_from_row, row_for_plan
from geopilot.persistence.run_repository import get_run
from geopilot.persistence.schema import open_database
from geopilot.plans.compiler import _same_json
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.models import GOAL_CLARIFICATION_ISSUE, AnalysisPlan, PlanStatus
from geopilot.runs.models import Run
from geopilot.tools.registry import ToolRegistry


_EDITABLE_FIELDS = {"goal", "inputs", "critical_options", "method"}
_DISCARDED_OPTION_ISSUES = {"INVALID_OPTION", "UNKNOWN_OPTION", "OPTION_NOT_APPLICABLE"}


class PlanVersionConflictError(ValueError):
    code = "PLAN_VERSION_CONFLICT"
    field = "version"

    def __init__(self, plan_id: str, version: int, latest_version: int) -> None:
        self.plan_id = plan_id
        self.version = version
        self.latest_version = latest_version
        self.message = f"Plan {plan_id!r} version {version} is stale. Revise version {latest_version}."
        super().__init__(self.message)


class PlanDraftRequiredError(ValueError):
    """The answer did not target the task's active draft."""


class PlanAnswerError(ValueError):
    """The exact draft could not accept the proposed answer."""


class FailedRunConflictError(ValueError):
    """The failed run snapshot no longer matches the revision request."""


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
    answer_field: str | None = None,
    failed_run: Run | None = None,
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
        if failed_run is not None and (
            not failed_run.can_replan or (failed_run.plan_id, failed_run.plan_version) != (plan_id, version)
            or get_run(connection, failed_run.id) != failed_run
        ):
            raise FailedRunConflictError("The run or its checks changed. Review the current failure evidence.")
        if answer_field is not None:
            current_plan = connection.execute(
                "SELECT id FROM analysis_plan WHERE task_id = ? ORDER BY created_at DESC, version DESC, id LIMIT 1",
                (baseline.task_id,),
            ).fetchone()
            if baseline.status is not PlanStatus.DRAFT or current_plan["id"] != baseline.id:
                raise PlanDraftRequiredError("Open the task's current draft before answering clarification.")

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
        if answer_field and answer_field.startswith("critical_options."):
            name = answer_field.removeprefix("critical_options.")
            if name not in candidate.critical_options:
                issue = next((issue for issue in candidate.issues if issue.field == answer_field), None)
                raise PlanAnswerError(issue.message if issue else "This option does not apply to the draft.")
        goal_issue = GOAL_CLARIFICATION_ISSUE
        inactive = {f"critical_options.{name}" for name in method.inactive_variant_options(candidate.critical_options)}
        retained = tuple(
            issue for issue in baseline.issues
            if (issue.code == goal_issue.code and issue.field == goal_issue.field and context.goal == baseline.goal)
            or (answer_field is not None and issue.field != answer_field
                and issue.field not in inactive and issue.code in _DISCARDED_OPTION_ISSUES)
        )
        if retained:
            issues = tuple(issue for issue in candidate.issues if issue not in retained)
            candidate = candidate.model_copy(update={"issues": (*issues, *retained)})

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
