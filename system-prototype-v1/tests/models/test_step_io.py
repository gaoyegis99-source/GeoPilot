from datetime import datetime

import pytest
from pydantic import ValidationError

from geopilot.domain.step_io import ExecutionStatus, StepFileRef, StepRequest, StepResult


def request_payload():
    return {
        "run_id": "run-001",
        "step_key": "create_buffer",
        "tool": {"id": "qgis-buffer", "version": 1},
        "workdir_ref": "run-001",
        "inputs": {"source": {"path": "inputs/road's source.gpkg", "layer": "roads|review"}},
        "parameters": {"distance": {"value": 0.5, "unit": "kilometer"}, "enabled": True},
        "output_targets": {"buffer": {"path": "outputs/buffer.gpkg"}},
        "log_path": "steps/create_buffer/process.log",
        "cancel_path": "cancel.flag",
    }


def result_payload(**updates):
    return {
        "run_id": "run-001",
        "step_key": "create_buffer",
        "status": "SUCCEEDED",
        "exit_code": 0,
        "outputs": {"buffer": {"path": "outputs/buffer.gpkg", "layer": "buffer", "type": "vector_polygon"}},
        "log_path": "steps/create_buffer/process.log",
        "warnings": [],
        "error": None,
        "started_at": "2026-11-11T10:00:08.895683Z",
        "finished_at": "2026-11-11T10:00:12.311281Z",
        "environment": {"qgis_version": "3.44.15-Solothurn"},
        **updates,
    }


def failed_payload(exit_code):
    return result_payload(
        status="FAILED", exit_code=exit_code, outputs={}, log_path=None,
        error={
            "code": "OUTPUT_MISSING", "phase": "execute", "step_key": "create_buffer",
            "field": "outputs.buffer", "message": "Missing output", "suggestion": "Check log",
            "log_path": None,
        },
    )


def test_step_request_round_trips_raw_json_and_versioned_tool():
    payload = request_payload()
    request = StepRequest.model_validate(payload)
    assert (request.tool.id, request.tool.version) == ("qgis-buffer", 1)
    assert request.inputs["source"].layer == "roads|review"
    assert request.output_targets["buffer"].layer is None
    assert request.model_dump(mode="json", exclude_unset=True) == payload
    assert StepRequest.model_validate_json(request.model_dump_json()) == request


@pytest.mark.parametrize("path", [
    "", "/outputs/a.gpkg", "inputs\\a.gpkg", "C:inputs/a.gpkg", "inputs/a|b.gpkg",
    "inputs/a\x00b.gpkg", "inputs/./a.gpkg", "inputs/../a.gpkg", "inputs//a.gpkg", "inputs/",
])
def test_file_references_reject_nonlocal_or_ambiguous_paths(path):
    with pytest.raises(ValidationError):
        StepFileRef(path=path)


@pytest.mark.parametrize("invalid_json", [float("inf"), {"a": [float("nan")]}, (1, 2), {1: "not a JSON key"}])
def test_parameters_require_strict_finite_json(invalid_json):
    payload = request_payload()
    payload["parameters"] = {"bad": invalid_json}
    with pytest.raises(ValidationError):
        StepRequest.model_validate(payload)


def test_request_rejects_missing_and_unknown_fields_and_noninteger_version():
    payload = request_payload()
    payload["tool"]["version"] = True
    with pytest.raises(ValidationError):
        StepRequest.model_validate(payload)
    payload = request_payload()
    payload.pop("cancel_path")
    payload["unknown"] = "value"
    with pytest.raises(ValidationError) as error:
        StepRequest.model_validate(payload)
    assert {tuple(item["loc"]) for item in error.value.errors()} == {("cancel_path",), ("unknown",)}


def test_terminal_result_round_trips_and_uses_distinct_execution_status_enum():
    payload = result_payload()
    result = StepResult.model_validate(payload)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert [status.value for status in ExecutionStatus] == ["NOT_STARTED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
    assert result.outputs["buffer"].type == "vector_polygon"
    assert isinstance(result.started_at, datetime) and result.started_at.tzinfo is not None
    assert result.model_dump(mode="json") == payload
    assert StepResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("exit_code", [None, 0, 7, -15])
def test_failed_envelope_retains_start_and_output_failure_exit_codes(exit_code):
    payload = failed_payload(exit_code)
    result = StepResult.model_validate(payload)
    assert result.status is ExecutionStatus.FAILED and result.exit_code == exit_code
    assert result.error.code == "OUTPUT_MISSING"
    assert result.model_dump(mode="json", exclude_unset=True) == payload


@pytest.mark.parametrize("updates", [
    {"status": "NOT_STARTED"}, {"status": "RUNNING"}, {"exit_code": True}, {"exit_code": "0"},
    {"exit_code": 7}, {"log_path": None}, {"status": "FAILED"},
    {"started_at": "2026-11-11T10:00:08"}, {"started_at": 1780000000},
    {"finished_at": "2026-11-11T10:00:07Z"},
    {"outputs": {"buffer": {"path": "outputs/buffer.gpkg", "type": "vector_polygon"}}},
    {"outputs": {"buffer": {"path": "../buffer.gpkg", "layer": None, "type": "vector_polygon"}}},
])
def test_result_rejects_nonterminal_inconsistent_or_untyped_content(updates):
    with pytest.raises(ValidationError):
        StepResult.model_validate(result_payload(**updates))


def test_error_identity_and_log_path_match_failed_result():
    payload = failed_payload(0)
    payload["error"]["step_key"] = "other_step"
    with pytest.raises(ValidationError):
        StepResult.model_validate(payload)
    payload = failed_payload(0)
    payload["error"]["log_path"] = "steps/other_step/process.log"
    with pytest.raises(ValidationError):
        StepResult.model_validate(payload)


def test_success_rejects_error_even_when_its_identity_matches():
    payload = result_payload()
    error = failed_payload(0)["error"]
    error["log_path"] = payload["log_path"]
    payload["error"] = error
    with pytest.raises(ValidationError, match="A successful step requires"):
        StepResult.model_validate(payload)


def test_cancellation_is_a_reserved_terminal_status():
    result = StepResult.model_validate(result_payload(status="CANCELLED", exit_code=-15, outputs={}))
    assert result.status is ExecutionStatus.CANCELLED
