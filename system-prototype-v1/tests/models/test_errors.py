from pathlib import Path

import pytest
from pydantic import ValidationError

from geopilot.domain.errors import ErrorDetail


def error_payload(**updates):
    return {
        "code": "OUTPUT_MISSING",
        "phase": "execute",
        "step_key": "create_buffer",
        "field": "outputs.buffer",
        "message": "The buffer file could not be opened.",
        "suggestion": "Check the step log.",
        "log_path": "steps/create_buffer/process.log",
        **updates,
    }


def test_error_accepts_raw_fields_and_optional_json_observations():
    payload = error_payload(observed={"bounds": [1, 2, 3, 4], "unit": None, "present": False})
    error = ErrorDetail.model_validate(payload)
    assert error.code == "OUTPUT_MISSING" and error.field == "outputs.buffer"
    assert error.model_dump(mode="json") == payload
    assert ErrorDetail.model_validate_json(error.model_dump_json()) == error


def test_preflight_error_accepts_null_field_and_log_path():
    payload = error_payload(code="CRS_UNKNOWN", phase="preflight", field=None, log_path=None)
    assert ErrorDetail.model_validate(payload).model_dump(mode="json", exclude_unset=True) == payload


@pytest.mark.parametrize("updates", [
    {"code": 7}, {"code": "not-machine-readable"}, {"phase": "  "}, {"step_key": ""},
    {"log_path": "../process.log"}, {"observed": {"area": float("nan")}},
    {"observed": Path("source.gpkg")}, {"unexpected": "field"},
])
def test_error_rejects_invalid_fields(updates):
    with pytest.raises(ValidationError):
        ErrorDetail.model_validate(error_payload(**updates))
