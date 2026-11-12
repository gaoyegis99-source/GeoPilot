"""Save and confirm immutable versions of analysis plans."""

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path

from geopilot.application.method_preconditions import MethodPrecondition
from geopilot.application.task_context import TaskContext, build_task_context
from geopilot.checks.tool_checks import tool_check_references
from geopilot.domain.data_asset import DataAsset
from geopilot.domain.tool_contract import ParameterSpec
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition
from geopilot.methods.validator import validate_method
from geopilot.persistence.asset_repository import asset_for_ref
from geopilot.persistence.plan_repository import insert_draft, plan_from_row, publish_confirmation, row_for_plan
from geopilot.persistence.schema import open_database
from geopilot.plans.compiler import PlanCompilationError, compile_plan_steps
from geopilot.plans.draft_builder import _option_values, _selected_inputs
from geopilot.plans.models import AnalysisPlan, PlanIssue, PlanStatus, PlanStep
from geopilot.tools.parameter_normalizer import _convert_quantity
from geopilot.tools.registry import ToolRegistry


_BUILTIN_PRECONDITIONS = {"required_input_roles_available", "selected_options_match_geometry_type"}


class PlanConfirmationError(ValueError):
    def __init__(self, issues: Iterable[PlanIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(self.issues[0].message if self.issues else "The plan cannot be confirmed")


def _require_no_issues(issues: Iterable[PlanIssue]) -> None:
    issues = tuple(issues)
    if issues:
        raise PlanConfirmationError(issues)


def _plan_row(database_path: str | Path, plan_id: str, version: int) -> sqlite3.Row:
    if not isinstance(plan_id, str) or not plan_id.strip() or type(version) is not int or version < 1:
        raise ValueError("An exact plan ID and positive integer version are required")
    with open_database(database_path) as connection:
        row = row_for_plan(connection, plan_id, version)
    if row is None:
        raise LookupError(f"Plan {plan_id!r} version {version} does not exist")
    return row


def save_draft(
    database_path: str | Path, plan: AnalysisPlan, *, context: TaskContext | None = None,
) -> AnalysisPlan:
    """Save a new draft version and its task together."""
    with open_database(database_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        return insert_draft(connection, plan, context=context)


def load_plan(database_path: str | Path, plan_id: str, version: int) -> AnalysisPlan:
    """Read a single exact snapshot."""
    return plan_from_row(_plan_row(database_path, plan_id, version))


def _validated_checks(
    plan: AnalysisPlan, method: MethodDefinition, registry: ToolRegistry, registered_rules: Iterable[str],
    precondition_checks: Mapping[str, MethodPrecondition] | None,
) -> list[tuple[int, MethodPrecondition]]:
    issues = [
        PlanIssue(code=issue.code, field=issue.field, message=issue.message)
        for issue in validate_method(method, registry=registry, registered_rules=registered_rules, options=plan.critical_options)
    ]
    if plan.check_rules != method.check_rules:
        issues.append(PlanIssue(code="CHECK_RULE_MISMATCH", field="check_rules", message="Check rules differ from the method"))
    if plan.expected_artifacts != method.expected_artifacts:
        issues.append(PlanIssue(
            code="ARTIFACT_BINDING_MISMATCH", field="expected_artifacts", message="Expected artifacts differ from the method",
        ))

    available_checks = precondition_checks or {}
    checks = []
    for index, name in enumerate(method.preconditions):
        if name in _BUILTIN_PRECONDITIONS:
            continue
        check = available_checks.get(name)
        if not callable(check):
            issues.append(PlanIssue(
                code="UNKNOWN_PRECONDITION", field=f"preconditions.{index}", message=f"No registered check for {name!r}",
            ))
        else:
            checks.append((index, check))
    _require_no_issues(issues)
    return checks


def _validate_inputs(database_path: str | Path, plan: AnalysisPlan, method: MethodDefinition, registry: ToolRegistry) -> None:
    _require_no_issues(
        PlanIssue(code="UNKNOWN_ASSET_ROLE", field=f"inputs.{role}", message=f"The method does not declare {role!r}")
        for role in plan.inputs if role not in method.inputs
    )
    context = build_task_context(
        database_path, task_id=plan.task_id, goal=plan.goal, method=method, selected=plan.inputs, registry=registry,
        options=plan.critical_options,
    )
    issues: list[PlanIssue] = []
    _selected_inputs(context, method, issues)
    _require_no_issues(issues)


def _validate_options(plan: AnalysisPlan, method: MethodDefinition) -> None:
    issues: list[PlanIssue] = []
    options = plan.critical_options.copy()
    for name, value in plan.critical_options.items():
        spec = method.critical_options.get(name)
        if (isinstance(spec, ParameterSpec) and spec.type in {"integer", "number"}
                and spec.unit is not None and isinstance(value, Mapping)):
            try:
                options[name] = _convert_quantity(spec, value)
            except (ValueError, OverflowError) as error:
                issues.append(PlanIssue(code="INVALID_OPTION", field=f"critical_options.{name}", message=str(error)))
    _require_no_issues(issues)

    values = _option_values(method, options, issues)
    for name in values:
        if name not in options:
            issues.append(PlanIssue(
                code="OPTION_REQUIRED", field=f"critical_options.{name}",
                message=f"Save the declared value for {name!r} in the draft before confirmation",
            ))
    _require_no_issues(issues)


def _run_preconditions(
    database_path: str | Path, plan: AnalysisPlan, checks: list[tuple[int, MethodPrecondition]],
) -> None:
    if not checks:
        return
    assets: dict[str, DataAsset] = {}
    issues: list[PlanIssue] = []
    with open_database(database_path) as connection:
        for role, reference in plan.inputs.items():
            asset = asset_for_ref(connection, reference)
            if asset is None:
                issues.append(PlanIssue(
                    code="SOURCE_UNREADABLE", field=f"inputs.{role}", message="The selected asset version no longer exists",
                ))
            else:
                assets[role] = asset
    _require_no_issues(issues)

    for index, check in checks:
        try:
            issues.extend(check(plan.model_copy(deep=True), assets.copy()))
        except (ValueError, OSError, LookupError) as error:
            issues.append(PlanIssue(code="METHOD_PRECONDITION_FAILED", field=f"preconditions.{index}", message=str(error)))
    _require_no_issues(issues)


def _freeze_tool_checks(
    previews: tuple[PlanStep, ...], steps: tuple[PlanStep, ...], registry: ToolRegistry,
) -> tuple[PlanStep, ...]:
    frozen = []
    for index, (preview, step) in enumerate(zip(previews, steps, strict=True)):
        contract = registry.get(step.tool.id, step.tool.version).contract
        rules = tool_check_references(contract.check_rules)
        if rules is None or (preview.tool_check_rules is not None and preview.tool_check_rules != rules):
            raise PlanConfirmationError((PlanIssue(
                code="TOOL_CHECK_RULE_MISMATCH", field=f"steps.{index}.tool_check_rules",
                message="Tool check rules or versions changed. Generate a new draft.",
            ),))
        if rules or preview.tool_check_rules is not None:
            step = step.model_copy(update={"tool_check_rules": rules})
        frozen.append(step)
    return tuple(frozen)


def confirm_plan(
    database_path: str | Path, plan_id: str, version: int, *, catalog: MethodCatalog, registry: ToolRegistry,
    registered_rules: Iterable[str], precondition_checks: Mapping[str, MethodPrecondition] | None = None,
) -> AnalysisPlan:
    """Recheck a draft and publish the same version with normalized steps."""
    row = _plan_row(database_path, plan_id, version)
    plan = plan_from_row(row)
    if plan.status is PlanStatus.CONFIRMED:
        return plan
    _require_no_issues(plan.issues)
    try:
        method = catalog.get_enabled(plan.method.id, plan.method.version)
    except KeyError as error:
        raise PlanConfirmationError((PlanIssue(
            code="METHOD_UNAVAILABLE", field="method", message="The exact method version is unavailable",
        ),)) from error

    checks = _validated_checks(plan, method, registry, registered_rules, precondition_checks)
    _validate_inputs(database_path, plan, method, registry)
    _validate_options(plan, method)
    try:
        steps = compile_plan_steps(plan, method, registry=registry)
    except PlanCompilationError as error:
        raise PlanConfirmationError((PlanIssue(code=error.code, field=error.field, message=error.message),)) from error
    steps = _freeze_tool_checks(plan.steps, steps, registry)
    if not plan.check_rules and not any(step.tool_check_rules for step in steps):
        raise PlanConfirmationError((PlanIssue(
            code="MISSING_CHECK_RULES", field="check_rules", message="Declare at least one method or tool check rule",
        ),))
    _run_preconditions(database_path, plan, checks)
    confirmed = AnalysisPlan.model_validate({
        **plan.model_dump(mode="python"), "status": PlanStatus.CONFIRMED, "steps": steps,
    })

    with open_database(database_path) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if publish_confirmation(connection, confirmed, draft_json=row["plan_json"]):
            return confirmed
        current = row_for_plan(connection, plan_id, version)
        if current is not None and current["status"] == PlanStatus.CONFIRMED.value:
            return plan_from_row(current)
        raise PlanConfirmationError((PlanIssue(
            code="PLAN_CHANGED", field="version", message="The draft changed during confirmation",
        ),))
