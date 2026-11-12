"""Ollama structured output from a model explicitly selected on this host."""

import json

from geopilot.llm.base import ModelErrorCode, ModelProviderError, ModelRequest, ModelResponse, validate_model_timeout
from geopilot.llm.client import _checked_request_json
from geopilot.llm.providers._local import MODEL_ID, SUGGESTION_INSTRUCTIONS, post_local_json
from geopilot.llm.response_parser import _reject_constant, _unique_object


# Ollama 0.24 rejects the full Pydantic schema. The parser enforces the full contract locally.
_STRING = {"type": "string"}
_FORMAT = {
    "type": "object",
    "required": ["method", "goal", "options", "missing"],
    "additionalProperties": False,
    "properties": {
        "method": {
            "type": "object", "required": ["id", "version"], "additionalProperties": False,
            "properties": {"id": _STRING, "version": {"type": "integer"}},
        },
        "goal": {
            "type": "object", "required": ["summary"], "additionalProperties": False,
            "properties": {"summary": _STRING},
        },
        "options": {"type": "object", "additionalProperties": True},
        "missing": {"type": "array", "items": _STRING},
        "explanation": _STRING,
    },
}


def _local_model(model: str) -> str:
    if not isinstance(model, str):
        raise ValueError("Use a local Ollama model identifier")
    if model.lower().endswith(":local"):
        model = model[:-len(":local")]
    if not MODEL_ID.fullmatch(model):
        raise ValueError("Use a local Ollama model identifier")
    tag = model.rsplit("/", 1)[-1].partition(":")[2].lower()
    if tag == "cloud" or tag.endswith("-cloud"):
        raise ValueError("Use a local Ollama model identifier")
    if not tag:
        model += ":latest"
    return f"{model}:local"


def _response(data: bytes) -> ModelResponse:
    try:
        envelope = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if (isinstance(envelope, dict) and envelope.get("done") is True
                and "error" not in envelope and not (envelope.get("remote_host") or envelope.get("remote_model"))):
            response = ModelResponse(text=envelope.get("response"))
            if response.text.strip():
                return response
    except (ValueError, RecursionError):
        pass
    raise ModelProviderError(ModelErrorCode.INVALID_RESPONSE) from None


class OllamaModelProvider:
    def __init__(self, model: str, *, port: int = 11434) -> None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be an integer from 1 to 65535")
        self._model = _local_model(model)
        self._port = port

    def generate(self, request: ModelRequest, *, timeout: float) -> ModelResponse:
        seconds = validate_model_timeout(timeout)
        prompt = _checked_request_json(request)
        body = json.dumps({
            "model": self._model, "prompt": prompt, "format": _FORMAT,
            "system": SUGGESTION_INSTRUCTIONS, "stream": False,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return _response(post_local_json(self._port, "/api/generate", body, seconds))
