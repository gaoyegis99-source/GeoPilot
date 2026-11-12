"""Persistent run status and per-step records for local execution."""

from datetime import UTC
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, model_validator

from geopilot.domain.errors import ErrorDetail, _RunPath, _Text
from geopilot.domain.step_io import ExecutionStatus, StepRequest, StepResult, _Timestamp


class CheckStatus(StrEnum):
    NOT_CHECKED = "NOT_CHECKED"
    CHECKING = "CHECKING"
    PASSED = "PASSED"
    WARNING = "WARNING"
    FAILED = "FAILED"


def _require_status_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a status string")
    return value


_ExecutionStatus = Annotated[ExecutionStatus, BeforeValidator(_require_status_text)]
_CheckStatus = Annotated[CheckStatus, BeforeValidator(_require_status_text)]


class _RunModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always", allow_inf_nan=False)


class Run(_RunModel):
    id: _Text
    plan_id: _Text
    plan_version: int = Field(strict=True, gt=0)
    workdir: _Text
    created_at: _Timestamp
    execution_status: _ExecutionStatus = ExecutionStatus.NOT_STARTED
    check_status: _CheckStatus = CheckStatus.NOT_CHECKED
    cancel_requested: StrictBool = False
    current_step: _Text | None = None
    current_report_id: _Text | None = None
    error: ErrorDetail | None = None

    @property
    def can_replan(self) -> bool:
        return self.execution_status is ExecutionStatus.FAILED or (
            self.execution_status is ExecutionStatus.SUCCEEDED and self.check_status is CheckStatus.FAILED
        )


class RunStep(_RunModel):
    run_id: _Text
    step_key: _Text
    position: int = Field(strict=True, ge=0)
    status: _ExecutionStatus = ExecutionStatus.NOT_STARTED
    request: StepRequest | None = None
    result: StepResult | None = None
    log_path: _RunPath | None = None
    started_at: _Timestamp | None = None
    finished_at: _Timestamp | None = None

    @model_validator(mode="after")
    def validate_step_record(self) -> Self:
        identity = self.run_id, self.step_key
        if self.request is not None and (self.request.run_id, self.request.step_key) != identity:
            raise ValueError("The request must belong to the same run and step")
        if self.result is not None:
            if (self.result.run_id, self.result.step_key) != identity:
                raise ValueError("The result must belong to the same run and step")
            if self.result.status is not self.status:
                raise ValueError("The result status must match the step status")

        paths = {self.log_path}
        if self.request is not None:
            paths.add(self.request.log_path)
        if self.result is not None:
            paths.add(self.result.log_path)
        if len(paths - {None}) > 1:
            raise ValueError("The step, request and result must refer to the same log")

        if (self.started_at is not None and self.finished_at is not None
                and self.finished_at.astimezone(UTC) < self.started_at.astimezone(UTC)):
            raise ValueError("finished_at cannot precede started_at")
        return self
