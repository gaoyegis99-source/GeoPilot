"""Parse a model suggestion against the exact offered candidate snapshot."""

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, TypeAdapter, ValidationError, model_validator

from geopilot.domain.errors import _Text
from geopilot.domain.task import TaskGoal
from geopilot.llm.base import ModelRequest, ModelResponse, _ModelContract
from geopilot.methods.models import MethodRef
from geopilot.plans.models import PlanIssue


_FieldPath = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*$")]
_OptionName = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")]
_OptionType = Literal["number", "integer", "boolean", "enum", "string", "json", "reference", "user_defined", "user_selected"]


class ModelSuggestion(_ModelContract):
    """The public JSON reply shape, before local interpretation."""

    method: MethodRef
    goal: TaskGoal = Field(repr=False)
    options: dict[_Text, JsonValue] = Field(repr=False)
    missing: list[_FieldPath] = Field(repr=False)
    explanation: str | None = Field(default=None, repr=False)


class ParsedModelSuggestion(ModelSuggestion):
    explanation_issue: PlanIssue | None = None


class _CandidateOption(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    name: _OptionName
    type: _OptionType
    required: bool
    values: list[_Text] | None = None

    @model_validator(mode="after")
    def require_enum_values(self) -> Self:
        if self.type == "enum" and (not self.values or len(set(self.values)) != len(self.values)):
            raise ValueError("An enum candidate option requires distinct values")
        return self


class _CandidateMethod(MethodRef):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)

    options: list[_CandidateOption]


_CANDIDATES = TypeAdapter(list[_CandidateMethod], config=ConfigDict(strict=True))
_MESSAGES = {
    "MODEL_INVALID_RESPONSE": "The model response does not match the suggestion schema.",
    "MODEL_REQUEST_INVALID": "The request does not contain a valid candidate list.",
    "MODEL_CANDIDATE_UNKNOWN": "The suggested method version was not offered in the request.",
    "MODEL_OPTION_UNKNOWN": "The suggestion contains an option that was not offered for this method.",
    "MODEL_OPTION_ENUM_INVALID": "A suggested enum value is outside the offered values.",
}


class ModelResponseError(ValueError):
    """Report a safe location without echoing the model response."""

    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        self.message = _MESSAGES[code]
        super().__init__(self.message)


def _unique_object(items: list[tuple[str, object]]) -> dict:
    value = dict(items)
    if len(value) != len(items):
        raise ValueError("Repeated model response field")
    return value


def _reject_constant(_: str) -> None:
    raise ValueError("Expected a finite JSON number")


def _response_field(error: ValidationError) -> str:
    path = error.errors(include_url=False, include_context=False, include_input=False)[0]["loc"]
    nested = path[:2]
    if nested in {("method", "id"), ("method", "version"), ("goal", "summary")}:
        return ".".join(nested)
    if path and path[0] in ModelSuggestion.model_fields:
        return path[0]
    return "response"


def _check_explanation(document: dict) -> PlanIssue | None:
    text = document.get("explanation")
    if text is None:
        return None
    if isinstance(text, str):
        try:
            text.encode("utf-8")
        except UnicodeError:
            pass
        else:
            return None
    document["explanation"] = None
    return PlanIssue(
        code="MODEL_EXPLANATION_INVALID", field="explanation", message="The model explanation was not usable text.",
    )


def _decode_suggestion(response: ModelResponse) -> ParsedModelSuggestion:
    if not isinstance(response, ModelResponse):
        raise ModelResponseError("MODEL_INVALID_RESPONSE", "response")
    try:
        document = json.loads(response.text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ModelResponseError("MODEL_INVALID_RESPONSE", "response") from None
    if not isinstance(document, dict):
        raise ModelResponseError("MODEL_INVALID_RESPONSE", "response")

    issue = _check_explanation(document)
    try:
        suggestion = ModelSuggestion.model_validate(document)
        suggestion.model_dump_json()
    except ValidationError as error:
        raise ModelResponseError("MODEL_INVALID_RESPONSE", _response_field(error)) from None
    except (ValueError, RecursionError):
        raise ModelResponseError("MODEL_INVALID_RESPONSE", "response") from None
    return ParsedModelSuggestion(**suggestion.model_dump(), explanation_issue=issue)


def _offered_options(request: ModelRequest, method: MethodRef) -> dict[str, _CandidateOption]:
    if not isinstance(request, ModelRequest):
        raise ModelResponseError("MODEL_REQUEST_INVALID", "candidate_methods")
    try:
        wire = ModelRequest.model_validate(request).payload
        candidates = _CANDIDATES.validate_python(wire.get("candidate_methods"))
    except ValidationError:
        raise ModelResponseError("MODEL_REQUEST_INVALID", "candidate_methods") from None

    by_method = {}
    for candidate in candidates:
        key = candidate.id, candidate.version
        options = {option.name: option for option in candidate.options}
        if key in by_method or len(options) != len(candidate.options):
            raise ModelResponseError("MODEL_REQUEST_INVALID", "candidate_methods")
        by_method[key] = options
    key = method.id, method.version
    if key not in by_method:
        raise ModelResponseError("MODEL_CANDIDATE_UNKNOWN", "method")
    return by_method[key]


def parse_model_response(response: ModelResponse, *, request: ModelRequest) -> ParsedModelSuggestion:
    """Validate structure, method identity and enums before local draft validation."""
    suggestion = _decode_suggestion(response)
    offered = _offered_options(request, suggestion.method)
    for name, value in suggestion.options.items():
        if name not in offered:
            raise ModelResponseError("MODEL_OPTION_UNKNOWN", "options")
        option = offered[name]
        if option.type == "enum" and (type(value) is not str or value not in (option.values or ())):
            raise ModelResponseError("MODEL_OPTION_ENUM_INVALID", f"options.{name}")
    return suggestion
