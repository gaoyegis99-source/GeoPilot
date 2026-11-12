"""Project local plan diagnostics into versioned user clarification items."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

from geopilot.application.task_context import InputReadiness, TaskContext
from geopilot.domain.data_asset import DataAssetRef
from geopilot.domain.errors import _Text
from geopilot.domain.tool_contract import ParameterSpec
from geopilot.methods.models import MethodDefinition, MethodRef
from geopilot.plans.models import AnalysisPlan, PlanIssue, PlanStatus


_Kind = Literal["goal", "input", "option", "validation", "confirmation"]
_Choice = DataAssetRef | StrictStr | StrictBool
_GOAL_ISSUE = PlanIssue(
    code="GOAL_AMBIGUOUS", field="goal.summary",
    message="Describe the GIS result the task should produce.",
)


class ClarificationItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    kind: _Kind
    code: _Text
    field: _Text
    reason: _Text
    required: Literal[True] = True
    blocks: Literal["confirmation", "execution"]
    options: tuple[_Choice, ...] = ()

    @model_validator(mode="after")
    def check_scope(self) -> Self:
        expected_block = "execution" if self.kind == "confirmation" else "confirmation"
        if self.blocks != expected_block:
            raise ValueError("Clarification item has the wrong blocking stage")
        if self.kind == "input":
            if not all(isinstance(value, DataAssetRef) for value in self.options):
                raise ValueError("Input options must be exact asset references")
        elif self.kind == "option":
            if any(isinstance(value, DataAssetRef) for value in self.options):
                raise ValueError("Parameter options must be values from the option contract")
        elif self.options:
            raise ValueError("This clarification kind has no predefined options")
        return self


class ClarificationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    schema_version: Literal[1] = 1
    task_id: _Text
    plan_id: _Text
    plan_version: Annotated[int, Field(strict=True, gt=0)]
    method: MethodRef
    status: Literal["blocked", "ready_for_confirmation", "confirmed"]
    items: tuple[ClarificationItem, ...]

    @model_validator(mode="after")
    def check_status(self) -> Self:
        stages = tuple(item.blocks for item in self.items)
        if self.status == "blocked" and (not stages or set(stages) != {"confirmation"}):
            raise ValueError("A blocked draft requires unresolved issues")
        if self.status == "ready_for_confirmation" and stages != ("execution",):
            raise ValueError("A ready draft requires an explicit confirmation item")
        if self.status == "confirmed" and stages:
            raise ValueError("A confirmed plan cannot have unresolved clarification items")
        return self


def require_goal_clarification(plan: AnalysisPlan) -> AnalysisPlan:
    """Add a local blocking issue when a planning flow determines the goal is unclear."""
    if plan.status is not PlanStatus.DRAFT:
        raise ValueError("Only a draft can require goal clarification")
    if any(issue.code == _GOAL_ISSUE.code and issue.field == _GOAL_ISSUE.field for issue in plan.issues):
        return plan
    return plan.model_copy(update={"issues": (*plan.issues, _GOAL_ISSUE)})


def _asset_options(context: TaskContext | None, role: str) -> tuple[DataAssetRef, ...]:
    if context is None or context.candidates is None:
        return ()
    assets = (
        assessment.asset for assessment in context.candidates.get(role, ())
        if assessment.status is InputReadiness.READY and assessment.asset is not None
    )
    return tuple(dict.fromkeys(assets))


def _parameter_options(spec) -> tuple[str | bool, ...]:
    if isinstance(spec, ParameterSpec):
        if spec.type == "enum":
            return spec.values or ()
        if spec.type == "boolean":
            return (False, True)
    return ()


def _from_issue(
    issue: PlanIssue, method: MethodDefinition, context: TaskContext | None,
) -> ClarificationItem:
    root, _, name = issue.field.partition(".")
    kind: _Kind = "validation"
    options: tuple[_Choice, ...] = ()
    if root == "goal":
        kind = "goal"
    elif root == "inputs" and name in method.inputs:
        kind, options = "input", _asset_options(context, name)
    elif root == "critical_options" and name in method.critical_options:
        kind, options = "option", _parameter_options(method.critical_options[name])
    return ClarificationItem(
        kind=kind, code=issue.code, field=issue.field, reason=issue.message,
        blocks="confirmation", options=options,
    )


def build_clarification_report(
    plan: AnalysisPlan, method: MethodDefinition, *, context: TaskContext | None = None,
) -> ClarificationReport:
    """Use only issues and choices from the matching local plan and method."""
    if plan.method != method.ref:
        raise ValueError("Clarification requires the exact method version")
    if context is not None:
        selected = {role: item.asset for role, item in context.roles.items() if item.asset is not None}
        if (context.task_id, context.method, context.goal, selected) != (
            plan.task_id, plan.method, plan.goal, plan.inputs,
        ):
            raise ValueError("Clarification context does not match the plan")

    items = ()
    if plan.status is PlanStatus.CONFIRMED:
        status = "confirmed"
    elif plan.issues:
        status = "blocked"
        items = tuple(_from_issue(issue, method, context) for issue in plan.issues)
    else:
        status = "ready_for_confirmation"
        items = (ClarificationItem(
            kind="confirmation", code="USER_CONFIRMATION_REQUIRED", field="status",
            reason="Review and confirm this plan version before starting a run.", blocks="execution",
        ),)
    return ClarificationReport(
        task_id=plan.task_id, plan_id=plan.id, plan_version=plan.version,
        method=plan.method, status=status, items=items,
    )
