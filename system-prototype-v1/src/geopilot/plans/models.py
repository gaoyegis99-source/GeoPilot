"""Versioned draft and confirmed analysis plan snapshots."""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from geopilot.domain.data_asset import DataAssetRef
from geopilot.domain.errors import _RunPath, _Text
from geopilot.domain.step_io import _Layer
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolRef
from geopilot.methods.models import (
    AssetRoleBinding,
    ExpectedArtifactBinding,
    MethodRef,
    StepOutputBinding,
    _Array,
)


class _PlanModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", revalidate_instances="always", allow_inf_nan=False
    )


class PlanStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


class PlanIssue(_PlanModel):
    code: _Text
    field: _Text
    message: _Text


GOAL_CLARIFICATION_ISSUE = PlanIssue(
    code="GOAL_AMBIGUOUS", field="goal.summary",
    message="Describe the GIS result the task should produce.",
)


class PlanOutput(_PlanModel):
    type: _Text
    target: _RunPath
    layer: _Layer | None = None


class ToolCheckRef(_PlanModel):
    name: _Text
    version: int = Field(strict=True, gt=0)


class PlanStep(_PlanModel):
    key: _Text
    tool: ToolRef | None
    inputs: dict[_Text, AssetRoleBinding | StepOutputBinding]
    parameters: dict[_Text, JsonValue]
    outputs: dict[_Text, PlanOutput]
    tool_check_rules: _Array[tuple[ToolCheckRef, ...]] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @field_validator("tool_check_rules")
    @classmethod
    def unique_tool_check_rules(cls, value: tuple[ToolCheckRef, ...] | None) -> tuple[ToolCheckRef, ...] | None:
        if value and len({rule.name for rule in value}) != len(value):
            raise ValueError("A step cannot repeat a tool check rule")
        return value


class AnalysisPlan(_PlanModel):
    id: _Text
    task_id: _Text
    version: int = Field(strict=True, gt=0)
    status: PlanStatus
    goal: TaskGoal
    inputs: dict[_Text, DataAssetRef]
    critical_options: dict[_Text, JsonValue]
    method: MethodRef
    steps: _Array[tuple[PlanStep, ...]]
    expected_artifacts: _Array[tuple[ExpectedArtifactBinding, ...]]
    check_rules: _Array[tuple[_Text, ...]]
    issues: _Array[tuple[PlanIssue, ...]]

    @property
    def has_checks(self) -> bool:
        return bool(self.check_rules or any(step.tool_check_rules for step in self.steps))

    @field_validator("status", mode="before")
    @classmethod
    def require_status_string(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("Expected a plan status string")
        return value

    @model_validator(mode="after")
    def validate_confirmed(self) -> Self:
        if self.status is PlanStatus.DRAFT:
            return self
        if not self.steps:
            raise ValueError("A confirmed plan requires at least one step")
        for step in self.steps:
            if step.tool is None:
                raise ValueError(f"Step {step.key!r} in a confirmed plan requires a tool version")
        if self.issues:
            raise ValueError("A confirmed plan cannot contain unresolved issues")
        return self
