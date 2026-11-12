"""Structured suggestions from an explicitly configured loopback Chat Completions server."""

import json
import re

from geopilot.llm.base import ModelErrorCode, ModelProviderError, ModelRequest, ModelResponse, validate_model_timeout
from geopilot.llm.client import _checked_request_json
from geopilot.llm.providers._local import MODEL_ID, SUGGESTION_INSTRUCTIONS, post_local_json
from geopilot.llm.response_parser import _reject_constant, _unique_object


_BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/\-]+=*")
_REPLY_INSTRUCTIONS = (
    SUGGESTION_INSTRUCTIONS
    + ' The required JSON fields are method {"id": string, "version": integer}, goal {"summary": string},'
    ' options (object), and missing (array of strings). An explanation string is optional.'
)
_MAX_COMPLETION_TOKENS = 768


def _response(data: bytes) -> ModelResponse:
    try:
        envelope = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if isinstance(envelope, dict) and "error" not in envelope:
            choices = envelope.get("choices")
            if isinstance(choices, list) and len(choices) == 1 and isinstance(choice := choices[0], dict):
                message = choice.get("message")
                if (
                    choice.get("finish_reason") == "stop" and isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and not (message.get("refusal") or message.get("tool_calls") or message.get("function_call"))
                ):
                    result = ModelResponse(text=message.get("content"))
                    if result.text.strip():
                        return result
    except (ValueError, RecursionError):
        pass
    raise ModelProviderError(ModelErrorCode.INVALID_RESPONSE) from None


class ChatCompletionsModelProvider:
    def __init__(self, model: str, *, port: int = 8080, api_key: str | None = None) -> None:
        if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
            raise ValueError("Use a local Chat Completions model identifier")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be an integer from 1 to 65535")
        if api_key is not None and (not isinstance(api_key, str) or not _BEARER_TOKEN.fullmatch(api_key)):
            raise ValueError("The local Chat Completions API key is invalid")
        self._model = model
        self._port = port
        self._api_key = api_key

    def generate(self, request: ModelRequest, *, timeout: float) -> ModelResponse:
        seconds = validate_model_timeout(timeout)
        prompt = _checked_request_json(request)
        body = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": _REPLY_INSTRUCTIONS},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": _MAX_COMPLETION_TOKENS,
            "stream": False,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return _response(post_local_json(self._port, "/v1/chat/completions", body, seconds, api_key=self._api_key))
