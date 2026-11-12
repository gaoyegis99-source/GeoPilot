"""Provider-neutral input, output and failures for model suggestions."""

import json
import math
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_serializer, field_validator, model_validator


_JSON_OBJECT = TypeAdapter(dict[str, JsonValue], config=ConfigDict(strict=True, allow_inf_nan=False))


def _finite_json_object(value: object) -> dict[str, JsonValue]:
    try:
        payload = _JSON_OBJECT.validate_python(value)
        json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        # Keep caller-controlled keys and values out of formatted errors.
        raise ValueError("The model request payload must be a finite UTF-8 JSON object") from None
    return payload


class _ModelContract(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, revalidate_instances="always",
        allow_inf_nan=False, hide_input_in_errors=True,
    )

    @model_validator(mode="before")
    @classmethod
    def reject_unknown_fields(cls, value: object) -> object:
        if isinstance(value, dict) and value.keys() - cls.model_fields.keys():
            raise ValueError("The model provider contract contains unexpected fields")
        return value


class ModelRequest(_ModelContract):
    """JSON assembled by the outbound context and candidate builders."""

    payload: dict[str, JsonValue] = Field(repr=False)

    @field_validator("payload", mode="plain", json_schema_input_type=dict[str, JsonValue])
    @classmethod
    def validate_payload(cls, value: object) -> dict[str, JsonValue]:
        return _finite_json_object(value)

    @field_serializer("payload")
    def serialize_payload(self, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _finite_json_object(value)


class ModelResponse(_ModelContract):
    """Provider text for the separate structured response parser."""

    text: str = Field(repr=False)

    @field_validator("text")
    @classmethod
    def require_utf8(cls, text: str) -> str:
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise ValueError("Model response text must be valid UTF-8") from None
        return text


class ModelErrorCode(StrEnum):
    TIMEOUT = "MODEL_TIMEOUT"
    NETWORK_ERROR = "MODEL_NETWORK_ERROR"
    UNAVAILABLE = "MODEL_UNAVAILABLE"
    RATE_LIMITED = "MODEL_RATE_LIMITED"
    INVALID_RESPONSE = "MODEL_INVALID_RESPONSE"


_ERRORS = {
    ModelErrorCode.TIMEOUT: ("The model request timed out.", True),
    ModelErrorCode.NETWORK_ERROR: ("The model network connection failed.", True),
    ModelErrorCode.UNAVAILABLE: ("The model service is unavailable.", True),
    ModelErrorCode.RATE_LIMITED: ("The model service is rate limited.", True),
    ModelErrorCode.INVALID_RESPONSE: ("The model service returned an invalid response.", False),
}


class ModelProviderError(Exception):
    """A normalized failure whose message contains no provider diagnostics."""

    def __init__(self, code: ModelErrorCode | str) -> None:
        try:
            self.code = ModelErrorCode(code)
        except (TypeError, ValueError):
            raise ValueError("Unsupported model provider error code") from None
        self.message, self.retryable = _ERRORS[self.code]
        super().__init__(self.message)


def validate_model_timeout(timeout: float) -> float:
    """Return positive finite timeout seconds for a provider call."""
    if type(timeout) not in (int, float):
        raise ValueError("timeout must be a positive finite number of seconds")
    try:
        seconds = float(timeout)
    except OverflowError:
        raise ValueError("timeout must be a positive finite number of seconds") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("timeout must be a positive finite number of seconds")
    return seconds


class ModelProvider(Protocol):
    def generate(self, request: ModelRequest, *, timeout: float) -> ModelResponse: ...
