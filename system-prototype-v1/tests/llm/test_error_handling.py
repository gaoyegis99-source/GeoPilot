import json
import socket
import traceback
from contextlib import ExitStack
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import sleep

import pytest

from geopilot.llm.base import ModelErrorCode, ModelProviderError, ModelRequest, ModelResponse
from geopilot.llm.client import request_model_suggestion
from geopilot.llm.data_boundary import DataBoundaryError
from geopilot.llm.providers.ollama import OllamaModelProvider
from geopilot.llm.response_parser import ModelResponseError, ModelSuggestion
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.models import AnalysisPlan
from geopilot.plans.service import load_plan, save_draft


SECRET = "test-private-model-diagnostic"
MODEL = "demo/local-model:4b-instruct"
SUGGESTION = {
    "method": {"id": "select-targets", "version": 2},
    "goal": {"summary": "在范围内筛选目标"},
    "options": {"relation": "within"},
    "missing": [],
}
RESPONSE_TEXT = json.dumps(SUGGESTION, ensure_ascii=False)


def _request(goal="在分析范围内筛选目标"):
    return ModelRequest(payload={
        "context": {"goal": goal, "roles": []},
        "candidate_methods": [{
            "id": "select-targets", "version": 2, "inputs": [], "steps": [],
            "options": [{"name": "relation", "type": "enum", "required": True, "values": ["within", "intersects"]}],
        }],
        "candidate_tools": [],
    })


class FixedProvider:
    def __init__(self, *responses, mutate=None):
        self.responses = iter(responses)
        self.calls = []
        self.mutate = mutate

    def generate(self, request, *, timeout):
        self.calls.append((request.model_dump_json(), timeout))
        if self.mutate is not None:
            self.mutate(request)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return ModelResponse(text=result)


def _assert_hidden(error):
    shown = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert SECRET not in shown


def test_retries_use_the_same_checked_snapshot_and_parse_against_it():
    request = _request()
    expected = request.model_dump_json()

    def mutate(sent):
        assert sent is not request
        sent.payload["candidate_methods"].clear()
        request.payload["context"]["goal"] = "/private/source.gpkg"

    provider = FixedProvider(ModelProviderError(ModelErrorCode.NETWORK_ERROR), RESPONSE_TEXT, mutate=mutate)
    result = request_model_suggestion(provider, request, timeout=1.25, max_attempts=2)
    assert result.method.version == 2 and result.options == {"relation": "within"}
    assert provider.calls == [(expected, 1.25), (expected, 1.25)]


@pytest.mark.parametrize("code", [
    ModelErrorCode.NETWORK_ERROR, ModelErrorCode.TIMEOUT, ModelErrorCode.UNAVAILABLE, ModelErrorCode.RATE_LIMITED,
])
def test_transient_error_stops_at_requested_attempt_limit(code):
    error = ModelProviderError(code)
    provider = FixedProvider(error, error, RESPONSE_TEXT)
    with pytest.raises(ModelProviderError) as failure:
        request_model_suggestion(provider, _request(), max_attempts=2)
    assert failure.value.code == code and failure.value.retryable
    assert len(provider.calls) == 2


def test_nonretryable_provider_error_is_returned_immediately():
    provider = FixedProvider(ModelProviderError(ModelErrorCode.INVALID_RESPONSE), RESPONSE_TEXT)
    with pytest.raises(ModelProviderError) as failure:
        request_model_suggestion(provider, _request(), max_attempts=3)
    assert failure.value.code == ModelErrorCode.INVALID_RESPONSE
    assert not failure.value.retryable and len(provider.calls) == 1


@pytest.mark.parametrize("response", [f"not JSON {SECRET}", json.dumps({**SUGGESTION, "command": SECRET})])
def test_invalid_model_reply_never_retries_and_preserves_existing_draft(tmp_path, response):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    plan = AnalysisPlan(
        id="plan-existing", version=1, task_id="task-existing", status="draft",
        goal={"summary": "用户保留的目标"}, inputs={}, critical_options={"relation": "intersects"},
        method={"id": "select-targets", "version": 2}, steps=[], expected_artifacts=[], check_rules=[], issues=[],
    )
    save_draft(database, plan)
    before = database.read_bytes()
    provider = FixedProvider(response, RESPONSE_TEXT)
    with pytest.raises(ModelResponseError) as failure:
        request_model_suggestion(provider, _request(), max_attempts=3)
    assert failure.value.code == "MODEL_INVALID_RESPONSE" and len(provider.calls) == 1
    _assert_hidden(failure.value)
    assert database.read_bytes() == before
    assert load_plan(database, plan.id, plan.version) == plan
    with open_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 0


@pytest.mark.parametrize("settings", [
    {"max_attempts": False}, {"max_attempts": 0}, {"max_attempts": 4}, {"max_attempts": 2.0},
    {"timeout": False}, {"timeout": 0}, {"timeout": float("inf")}, {"timeout": float("nan")},
])
def test_invalid_client_controls_prevent_provider_call(settings):
    provider = FixedProvider(RESPONSE_TEXT)
    with pytest.raises(ValueError):
        request_model_suggestion(provider, _request(), **settings)
    assert provider.calls == []


def test_boundary_rejects_protected_text_before_even_a_custom_provider():
    provider = FixedProvider(RESPONSE_TEXT)
    with pytest.raises(DataBoundaryError) as failure:
        request_model_suggestion(provider, _request(SECRET.upper()), protected_literals=(SECRET,))
    assert (failure.value.code, failure.value.field) == ("OUTBOUND_DATA_REJECTED", "$.payload.context.goal")
    assert provider.calls == []
    _assert_hidden(failure.value)


@dataclass
class Reply:
    status: int = 200
    body: bytes = field(default_factory=lambda: json.dumps({"response": RESPONSE_TEXT, "done": True}).encode())
    headers: dict = field(default_factory=dict)
    delay: float = 0
    body_interval: float = 0


@pytest.fixture
def http_server():
    with ExitStack() as stack:
        def start(*replies):
            pending = iter(replies)
            calls = []

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    calls.append((self.path, self.headers, body))
                    reply = next(pending, Reply(status=418))
                    if reply.delay:
                        sleep(reply.delay)
                    self.send_response(reply.status)
                    for name, value in {
                        "Content-Type": "application/json", "Content-Length": str(len(reply.body)), **reply.headers,
                    }.items():
                        self.send_header(name, value)
                    self.end_headers()
                    try:
                        if reply.body_interval:
                            for byte in reply.body[:12]:
                                self.wfile.write(bytes([byte]))
                                sleep(reply.body_interval)
                            self.wfile.write(reply.body[12:])
                        else:
                            self.wfile.write(reply.body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

                def log_message(self, *_args):
                    pass

            server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            stack.callback(thread.join)
            stack.callback(server.shutdown)
            return server.server_port, calls

        yield start


def test_actual_http_retry_posts_exact_scanned_prompt_with_fixed_envelope(http_server, monkeypatch):
    proxy_port, proxy_calls = http_server()
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy_url)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "")

    port, calls = http_server(Reply(status=429, body=SECRET.encode()), Reply())
    request = _request()
    result = request_model_suggestion(OllamaModelProvider(MODEL, port=port), request, max_attempts=2)
    assert result.options == {"relation": "within"}
    assert len(calls) == 2 and proxy_calls == []
    assert calls[0][2] == calls[1][2]
    for path, headers, body in calls:
        assert path == "/api/generate" and headers["Host"] == f"127.0.0.1:{port}"
        assert headers["Content-Type"] == "application/json" and "Authorization" not in headers
        wire = json.loads(body.decode("utf-8"))
        assert set(wire) == {"model", "prompt", "format", "system", "stream"}
        assert wire["model"] == MODEL + ":local" and wire["stream"] is False
        grammar = wire["format"]
        contract = ModelSuggestion.model_json_schema()
        assert grammar["required"] == contract["required"] and grammar["additionalProperties"] is False
        assert grammar["properties"].keys() == contract["properties"].keys()
        assert "$defs" not in grammar and grammar["properties"]["options"] == {
            "type": "object", "additionalProperties": True,
        }
        assert "payload.context.roles" in wire["system"] and "READY" in wire["system"]
        assert "inputs.roads" not in wire["system"] and request.payload["context"]["goal"] not in wire["system"]
        assert wire["prompt"] == request.model_dump_json()
        assert "在分析范围内筛选目标".encode() in body


def test_rejected_request_opens_no_socket_even_when_adapter_called_directly(http_server, monkeypatch):
    port, calls = http_server()
    provider = OllamaModelProvider(MODEL, port=port)

    def connect(*_args, **_kwargs):
        pytest.fail("Rejected request attempted socket I/O")

    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(DataBoundaryError) as failure:
        provider.generate(_request("/private/source.gpkg"), timeout=1)
    assert failure.value.field == "$.payload.context.goal" and calls == []
    with pytest.raises(DataBoundaryError):
        request_model_suggestion(provider, _request(SECRET), protected_literals=(SECRET,))
    assert calls == []


def test_redirect_is_never_followed_or_retried(http_server):
    redirect_port, redirected = http_server(Reply())
    port, calls = http_server(Reply(status=307, headers={"Location": f"http://127.0.0.1:{redirect_port}/{SECRET}"}))
    with pytest.raises(ModelProviderError) as failure:
        request_model_suggestion(OllamaModelProvider(MODEL, port=port), _request(), max_attempts=3)
    assert failure.value.code == ModelErrorCode.INVALID_RESPONSE and not failure.value.retryable
    assert len(calls) == 1 and redirected == []
    _assert_hidden(failure.value)


@pytest.mark.parametrize(("status", "code"), [
    (400, ModelErrorCode.INVALID_RESPONSE), (401, ModelErrorCode.INVALID_RESPONSE),
    (404, ModelErrorCode.INVALID_RESPONSE), (408, ModelErrorCode.TIMEOUT),
    (429, ModelErrorCode.RATE_LIMITED), (500, ModelErrorCode.UNAVAILABLE), (503, ModelErrorCode.UNAVAILABLE),
])
def test_actual_http_error_classification_hides_remote_body(http_server, status, code):
    port, calls = http_server(Reply(status=status, body=SECRET.encode()))
    with pytest.raises(ModelProviderError) as failure:
        request_model_suggestion(OllamaModelProvider(MODEL, port=port), _request(), max_attempts=1)
    assert failure.value.code == code and len(calls) == 1
    _assert_hidden(failure.value)


@pytest.mark.parametrize("reply", [
    Reply(body=SECRET.encode()), Reply(body=b"\xff"), Reply(body=b"[1,2]"),
    Reply(body=b'{"done":false,"response":"incomplete"}'),
    Reply(body=b'{"done":true,"response":1}'),
    Reply(body=b'{"done":true,"response":"first","response":"second"}'),
    Reply(body=b'{"done":true,"response":"value"}', headers={"Content-Length": "100"}),
])
def test_invalid_ollama_envelope_is_terminal(http_server, reply):
    port, calls = http_server(reply, Reply())
    with pytest.raises(ModelProviderError) as failure:
        request_model_suggestion(OllamaModelProvider(MODEL, port=port), _request(), max_attempts=3)
    assert failure.value.code == ModelErrorCode.INVALID_RESPONSE and len(calls) == 1
    _assert_hidden(failure.value)


def test_real_connection_timeout_and_refusal_are_classified(http_server):
    port, calls = http_server(Reply(delay=0.4))
    with pytest.raises(ModelProviderError) as failure:
        request_model_suggestion(OllamaModelProvider(MODEL, port=port), _request(), timeout=0.1, max_attempts=1)
    assert failure.value.code == ModelErrorCode.TIMEOUT and len(calls) == 1

    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        provider = OllamaModelProvider(MODEL, port=unused.getsockname()[1])
        with pytest.raises(ModelProviderError) as failure:
            request_model_suggestion(provider, _request(), timeout=0.1, max_attempts=1)
        assert failure.value.code == ModelErrorCode.NETWORK_ERROR


def test_attempt_deadline_applies_to_a_response_with_regular_partial_reads(http_server):
    port, calls = http_server(Reply(body_interval=0.03))
    with pytest.raises(ModelProviderError) as failure:
        request_model_suggestion(OllamaModelProvider(MODEL, port=port), _request(), timeout=0.15, max_attempts=1)
    assert failure.value.code == ModelErrorCode.TIMEOUT and len(calls) == 1


@pytest.mark.parametrize(("model", "expected"), [
    ("plain-model", "plain-model:latest:local"),
    ("demo/local-model:4b-instruct:local", MODEL + ":local"),
])
def test_local_source_is_pinned_for_bare_and_explicitly_local_models(http_server, model, expected):
    port, calls = http_server(Reply())
    provider = OllamaModelProvider(model, port=port)
    request_model_suggestion(provider, _request(), max_attempts=1)
    assert json.loads(calls[0][2])["model"] == expected


@pytest.mark.parametrize(("model", "port"), [
    ("", 11434), ("https://example.com/local-model", 11434), ("local-model\nother", 11434),
    ("../../model", 11434), ("local-model", True), ("local-model", 0), ("local-model", 65536),
    ("some-model:cloud", 11434), ("some-model:17b-cloud", 11434), ("some-model:17b-cloud:local", 11434),
])
def test_model_name_and_port_configuration_are_strict(model, port):
    with pytest.raises(ValueError):
        OllamaModelProvider(model, port=port)
