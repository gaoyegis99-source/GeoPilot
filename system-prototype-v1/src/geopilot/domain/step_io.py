"""Versioned request and terminal result for a local GIS step."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator, AwareDatetime, BeforeValidator, Field, JsonValue, StrictInt, StringConstraints, model_validator,
)

from geopilot.domain.errors import ErrorDetail, _RunPath, _StepModel, _Text
from geopilot.domain.tool_contract import ToolRef


class ExecutionStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError("Expected a datetime or ISO 8601 timestamp")


def _validate_layer_name(value: str) -> str:
    if "\x00" in value:
        raise ValueError("A layer name cannot contain a NUL character")
    return value


_Timestamp = Annotated[AwareDatetime, BeforeValidator(_parse_timestamp)]
_Layer = Annotated[_Text, AfterValidator(_validate_layer_name)]
_Sha256 = Annotated[str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]


class StepFileRef(_StepModel):
    path: _RunPath
    layer: _Layer | None = None


class StepOutput(StepFileRef):
    layer: _Layer | None = Field(...)
    type: _Text


class StepRequest(_StepModel):
    run_id: _Text
    step_key: _Text
    tool: ToolRef
    workdir_ref: _Text
    inputs: dict[_Text, StepFileRef]
    parameters: dict[_Text, JsonValue]
    output_targets: dict[_Text, StepFileRef]
    log_path: _RunPath
    cancel_path: _RunPath


class StepReuse(_StepModel):
    cache_key: _Sha256
    source_run_id: _Text
    source_step_key: _Text
    output_sha256: _Sha256


class StepResult(_StepModel):
    run_id: _Text
    step_key: _Text
    status: Literal[ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED]
    exit_code: StrictInt | None
    outputs: dict[_Text, StepOutput]
    log_path: _RunPath | None
    warnings: list[_Text]
    error: ErrorDetail | None
    started_at: _Timestamp
    finished_at: _Timestamp
    environment: dict[_Text, _Text]
    reuse: StepReuse | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.finished_at.astimezone(UTC) < self.started_at.astimezone(UTC):
            raise ValueError("finished_at cannot precede started_at")
        if self.reuse is not None:
            if (
                self.status is not ExecutionStatus.SUCCEEDED or self.exit_code is not None
                or self.log_path is None or self.error is not None
            ):
                raise ValueError("A reused step requires success, a log path, no exit code and no error")
            if len(self.outputs) != 1 or any(
                output.type != "json" or output.layer is not None for output in self.outputs.values()
            ):
                raise ValueError("A reused step requires one JSON output without a layer")
        elif self.status is ExecutionStatus.SUCCEEDED and (
            self.exit_code != 0 or self.error is not None or self.log_path is None
        ):
            raise ValueError("A successful step requires exit code 0, a log path and no error")
        if self.status is ExecutionStatus.FAILED and self.error is None:
            raise ValueError("A failed step requires an error")
        if self.error is not None and (self.error.step_key != self.step_key or self.error.log_path != self.log_path):
            raise ValueError("The step error must use the same step key and log path as the result")
        return self
