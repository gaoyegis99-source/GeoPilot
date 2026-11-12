"""Structured error data shared by the local step adapters."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, StringConstraints


_Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
_Code = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_]*$")]


def _validate_relative_path(value: str) -> str:
    if any(character in value for character in "\\:|\x00"):
        raise ValueError("Expected a local path relative to the run directory")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Run paths cannot contain empty, current or parent directory components")
    return value


_RunPath = Annotated[
    _Text,
    AfterValidator(_validate_relative_path),
    Field(description="Slash-separated local path relative to the run root, without traversal or URI delimiters."),
]


class _StepModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always")


class ErrorDetail(_StepModel):
    code: _Code
    phase: _Text
    step_key: _Text
    field: _Text | None
    message: _Text
    suggestion: _Text
    log_path: _RunPath | None
    observed: JsonValue = None
