"""Ollama structured output from a model explicitly selected on this host."""

import json
import re
import socket
from contextlib import closing, suppress
from http.client import HTTPConnection, HTTPException
from threading import Event, Timer
from time import monotonic

from geopilot.llm.base import ModelErrorCode, ModelProviderError, ModelRequest, ModelResponse, validate_model_timeout
from geopilot.llm.client import _checked_request_json
from geopilot.llm.response_parser import _reject_constant, _unique_object


_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
_MODEL_ID = re.compile(rf"(?:{_NAME}/){{0,2}}{_NAME}(?::{_NAME})?")
_MAX_REPLY_BYTES = 1024 * 1024
_STATUS_ERRORS = {
    408: ModelErrorCode.TIMEOUT,
    429: ModelErrorCode.RATE_LIMITED,
    500: ModelErrorCode.UNAVAILABLE,
    502: ModelErrorCode.UNAVAILABLE,
    503: ModelErrorCode.UNAVAILABLE,
    504: ModelErrorCode.UNAVAILABLE,
}
_SYSTEM = (
    "Suggest one GeoPilot GIS method using the JSON envelope in the user prompt. "
    "Choose an exact method id and version from payload.candidate_methods. "
    "Return only a JSON object matching the response schema. Write goal.summary in the request language. "
    "Use only roles and option names declared by the chosen method and only offered enum values. "
    "A role marked READY in payload.context.roles is available. Omit uncertain options. "
    "In missing, list dotted paths for declared required inputs or options that remain unavailable. "
    "Use an empty missing array when all required inputs and options are available. "
    "Keep explanation brief if included. Do not provide steps, commands, paths or source data."
)
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
    if not _MODEL_ID.fullmatch(model):
        raise ValueError("Use a local Ollama model identifier")
    tag = model.rsplit("/", 1)[-1].partition(":")[2].lower()
    if tag == "cloud" or tag.endswith("-cloud"):
        raise ValueError("Use a local Ollama model identifier")
    if not tag:
        model += ":latest"
    return f"{model}:local"


def _interrupt(sock: socket.socket, expired: Event) -> None:
    expired.set()
    with suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


def _exchange(connection: HTTPConnection, body: bytes) -> bytes:
    connection.request(
        "POST", "/api/generate", body=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with closing(connection.getresponse()) as response:
        if response.status != 200:
            raise ModelProviderError(_STATUS_ERRORS.get(response.status, ModelErrorCode.INVALID_RESPONSE))
        length = response.length
        if length is not None and length > _MAX_REPLY_BYTES:
            raise ModelProviderError(ModelErrorCode.INVALID_RESPONSE)
        data = response.read(_MAX_REPLY_BYTES + 1)
    if len(data) > _MAX_REPLY_BYTES or (length is not None and len(data) != length):
        raise ModelProviderError(ModelErrorCode.INVALID_RESPONSE)
    return data


def _post_generate(port: int, body: bytes, timeout: float) -> bytes:
    deadline = monotonic() + timeout
    expired = Event()
    with closing(HTTPConnection("127.0.0.1", port, timeout=timeout)) as connection:
        try:
            connection.connect()
            sock = connection.sock
            assert sock is not None
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock.settimeout(remaining)

            # A socket idle timeout alone can be extended by repeated partial reads.
            timer = Timer(remaining, _interrupt, (sock, expired))
            timer.daemon = True
            timer.start()
            try:
                data = _exchange(connection, body)
            finally:
                timer.cancel()
            if expired.is_set() or monotonic() >= deadline:
                raise TimeoutError
        except (ModelProviderError, OSError, HTTPException) as error:
            if expired.is_set() or isinstance(error, TimeoutError):
                code = ModelErrorCode.TIMEOUT
            elif isinstance(error, ModelProviderError):
                raise
            elif isinstance(error, OSError):
                code = ModelErrorCode.NETWORK_ERROR
            else:
                code = ModelErrorCode.INVALID_RESPONSE
            raise ModelProviderError(code) from None
    return data


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
            "system": _SYSTEM, "stream": False,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return _response(_post_generate(self._port, body, seconds))
