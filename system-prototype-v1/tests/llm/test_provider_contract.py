import json
import socket
import traceback
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from geopilot.llm.base import ModelErrorCode, ModelProvider, ModelProviderError, ModelRequest, ModelResponse
from geopilot.llm.providers.mock import MockModelProvider


SECRET = "synthetic-token-do-not-print"


def _request():
    return ModelRequest(payload={
        "context": {"goal": "选择已登记的分析方法", "roles": [{"role": "features", "feature_count": 2}]},
        "candidate_methods": [{"id": "attribute-filter-export", "version": 1}],
        "options": {"distance": 2.5, "deduplicate": True, "comment": None},
    })


def test_request_isolated_from_input_and_serialized_copies():
    source = {"context": {"goal": "筛选要素", "roles": ["features"]}, "options": {"limit": 3, "extra": None}}
    request = ModelRequest(payload=source)
    expected = json.loads(json.dumps(source))
    source["context"]["roles"].append("added-by-caller")
    source["options"]["limit"] = 10
    serialized = request.model_dump(mode="json")
    serialized["payload"]["context"]["roles"].clear()

    assert request.payload == expected
    assert json.loads(request.model_dump_json()) == {"payload": expected}
    assert ModelRequest.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValidationError):
        request.payload = {}


@pytest.mark.parametrize("value", [
    float("nan"), float("inf"), float("-inf"), (1, 2), {1, 2}, {1: "integer-key"},
    b"bytes", Path("local-path"), datetime(2026, 1, 1, tzinfo=UTC), chr(0xD800),
])
def test_rejects_nonstandard_values_in_nested_payload(value):
    with pytest.raises(ValidationError):
        ModelRequest(payload={"context": {"options": [value]}})


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_rejects_nonfinite_values_in_json_documents(constant):
    with pytest.raises(ValidationError):
        ModelRequest.model_validate_json('{"payload": {"context": {"value": ' + constant + '}}}')


def test_invalid_payload_errors_and_repr_do_not_echo_input_secrets():
    request = ModelRequest(payload={"synthetic": SECRET})
    response = ModelResponse(text=SECRET)
    assert SECRET not in repr(request) + repr(response)
    with pytest.raises(ValidationError) as payload_error:
        ModelRequest(payload={"context": {SECRET: [b"not-json"]}})
    with pytest.raises(ValidationError) as extra_error:
        ModelRequest(payload={}, api_key=SECRET)
    with pytest.raises(ValidationError) as extra_key_error:
        ModelRequest.model_validate({"payload": {}, SECRET: "unexpected key"})
    for error in (payload_error.value, extra_error.value, extra_key_error.value):
        assert SECRET not in str(error) + repr(error) + "".join(traceback.format_exception(error))


@pytest.mark.parametrize("text", ['{"candidate_id":"filter"}', "malformed JSON", "", "原始建议文本"])
def test_mock_is_deterministic_and_preserves_original_response_text(monkeypatch, capsys, caplog, text):
    def no_network(*_args, **_kwargs):
        pytest.fail("The mock attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    provider: ModelProvider = MockModelProvider(response_text=text)
    first = provider.generate(_request(), timeout=1.0)
    assert first == provider.generate(_request(), timeout=2) == ModelResponse(text=text)
    assert first.text == text
    captured = capsys.readouterr()
    assert captured.out == captured.err == caplog.text == ""


@pytest.mark.parametrize(("code", "retryable"), [
    (ModelErrorCode.TIMEOUT, True),
    (ModelErrorCode.NETWORK_ERROR, True),
    (ModelErrorCode.UNAVAILABLE, True),
    (ModelErrorCode.RATE_LIMITED, True),
    (ModelErrorCode.INVALID_RESPONSE, False),
])
def test_mock_returns_safe_fresh_structured_errors(code, retryable, capsys, caplog):
    provider = MockModelProvider(error_code=code)
    request = ModelRequest(payload={"synthetic": SECRET})
    failures = []
    for _ in range(2):
        with pytest.raises(ModelProviderError) as error:
            provider.generate(request, timeout=1)
        failure = error.value
        assert (failure.code, failure.retryable) == (code, retryable)
        assert failure.message == str(failure)
        assert SECRET not in str(failure) + repr(failure) + "".join(traceback.format_exception(failure))
        failures.append(failure)
    assert failures[0] is not failures[1]
    captured = capsys.readouterr()
    assert captured.out == captured.err == caplog.text == ""


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), float("-inf"), "5", 10 ** 500])
def test_timeout_must_be_finite_and_positive(timeout):
    provider = MockModelProvider(response_text="{}")
    with pytest.raises(ValueError, match="timeout"):
        provider.generate(_request(), timeout=timeout)


def test_mock_configuration_requires_exactly_one_safe_outcome():
    with pytest.raises(ValueError):
        MockModelProvider()
    with pytest.raises(ValueError):
        MockModelProvider(response_text="{}", error_code=ModelErrorCode.TIMEOUT)
    with pytest.raises(ValueError) as error:
        MockModelProvider(error_code=SECRET)
    assert SECRET not in "".join(traceback.format_exception(error.value))


def test_provider_revalidates_typed_request_without_accepting_raw_objects():
    provider = MockModelProvider(response_text="{}")
    with pytest.raises(TypeError):
        provider.generate({"payload": {}}, timeout=1)
    request = _request()
    request.payload["bad"] = float("nan")
    with pytest.raises(ValidationError):
        provider.generate(request, timeout=1)
    with pytest.raises(ValueError, match="finite UTF-8 JSON object"):
        request.model_dump_json()
