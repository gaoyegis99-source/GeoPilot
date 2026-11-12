"""Shared prompt and bounded HTTP transport for local model services."""

import re
import socket
from contextlib import closing, suppress
from http.client import HTTPConnection, HTTPException
from threading import Event, Timer
from time import monotonic

from geopilot.llm.base import ModelErrorCode, ModelProviderError


_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
MODEL_ID = re.compile(rf"(?:{_NAME}/){{0,2}}{_NAME}(?::{_NAME})?")
_MAX_REPLY_BYTES = 1024 * 1024
_STATUS_ERRORS = {
    408: ModelErrorCode.TIMEOUT,
    429: ModelErrorCode.RATE_LIMITED,
    500: ModelErrorCode.UNAVAILABLE,
    502: ModelErrorCode.UNAVAILABLE,
    503: ModelErrorCode.UNAVAILABLE,
    504: ModelErrorCode.UNAVAILABLE,
}
SUGGESTION_INSTRUCTIONS = (
    "Suggest one GeoPilot GIS method using the JSON envelope in the user prompt. "
    "Choose an exact method id and version from payload.candidate_methods. "
    "Return only a JSON object matching the response schema. Write goal.summary in the request language. "
    "Use only roles and option names declared by the chosen method and only offered enum values. "
    "A role marked READY in payload.context.roles is available. Omit uncertain options. "
    "In missing, list dotted paths for declared required inputs or options that remain unavailable. "
    "Use an empty missing array when all required inputs and options are available. "
    "Keep explanation brief if included. Do not provide steps, commands, paths or source data."
)


def _interrupt(sock: socket.socket, expired: Event) -> None:
    expired.set()
    with suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


def _exchange(connection: HTTPConnection, path: str, body: bytes, api_key: str | None) -> bytes:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    connection.request("POST", path, body=body, headers=headers)
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


def post_local_json(port: int, path: str, body: bytes, timeout: float, *, api_key: str | None = None) -> bytes:
    """Post to the fixed loopback host, without following redirects or logging responses."""
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

            # The deadline also applies to repeated partial reads.
            timer = Timer(remaining, _interrupt, (sock, expired))
            timer.daemon = True
            timer.start()
            try:
                data = _exchange(connection, path, body, api_key)
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
