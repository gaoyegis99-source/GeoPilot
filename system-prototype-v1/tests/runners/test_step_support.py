import json

import pytest

from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.step_support import AdapterError, execute_adapter
from geopilot.tools.registry import ToolRegistry


@pytest.fixture
def step(tmp_path):
    (tmp_path / "steps" / "write").mkdir(parents=True)
    (tmp_path / "outputs").mkdir()
    registry = ToolRegistry(python_implementations=["example.writer"])
    registry.register(ToolContract.model_validate({
        "id": "write-table", "version": 1, "runner": "python", "implementation": "example.writer",
        "inputs": {}, "outputs": {"table": "table_csv"}, "parameters": {},
        "measurement_policy": {"distance": "not_applicable", "area": "not_applicable"},
        "preconditions": [], "check_rules": [],
    }))
    request = {
        "run_id": "run-1", "step_key": "write", "tool": {"id": "write-table", "version": 1},
        "workdir_ref": "run-1", "inputs": {}, "parameters": {},
        "output_targets": {"table": {"path": "outputs/table.csv"}},
        "log_path": "steps/write/process.log", "cancel_path": "cancel.flag",
    }
    return registry, request


def test_adapter_records_normalized_request_and_verified_file(step, tmp_path):
    registry, request = step

    def write_table(context):
        assert json.loads(context.request_path.read_text(encoding="utf-8")) == context.request
        (context.workdir / "outputs" / "table.csv").write_text("id,area\n1,1\n", encoding="utf-8")
        return {"worker": "unit-test"}

    result = execute_adapter(request, registry=registry, workdir=tmp_path, runner="python", invoke=write_table)
    assert result["status"] == "SUCCEEDED"
    assert result["outputs"]["table"] == {"path": "outputs/table.csv", "layer": None, "type": "table_csv"}
    assert json.loads((tmp_path / "steps/write/result.json").read_text(encoding="utf-8")) == result


def test_adapter_rejects_escaped_and_preexisting_output_without_invoking(step, tmp_path):
    registry, request = step

    def must_not_start(context):
        pytest.fail(f"adapter invoked unexpectedly: {context}")

    output = tmp_path / "outputs" / "table.csv"
    output.write_text("existing", encoding="utf-8")
    result = execute_adapter(request, registry=registry, workdir=tmp_path, runner="python", invoke=must_not_start)
    assert result["status"] == "FAILED"
    assert result["error"]["field"] == "output_targets.table.path"
    assert output.read_text(encoding="utf-8") == "existing"

    request["output_targets"]["table"]["path"] = "../table.csv"
    result = execute_adapter(request, registry=registry, workdir=tmp_path, runner="python", invoke=must_not_start)
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "INVALID_STEP_REQUEST"


def test_preflight_failure_preserves_observation_and_suggestion(step, tmp_path):
    registry, request = step

    def reject(context):
        assert context.contract.runner == "python"
        raise AdapterError(
            "The observed extent is outside the projection area",
            code="MEASUREMENT_POLICY_REJECTED",
            phase="preflight",
            field="inputs.source.crs",
            observed=(123, 40, 123.1, 40.1),
            suggestion="Use the UTM zone covering the source",
        )

    result = execute_adapter(request, registry=registry, workdir=tmp_path, runner=None, invoke=reject)

    assert result["status"] == "FAILED" and result["exit_code"] is None
    assert result["error"]["field"] == "inputs.source.crs"
    assert result["error"]["observed"] == [123, 40, 123.1, 40.1]
    assert result["error"]["suggestion"] == "Use the UTM zone covering the source"
    assert result == json.loads((tmp_path / "steps/write/result.json").read_text(encoding="utf-8"))
    log = (tmp_path / "steps/write/process.log").read_text(encoding="utf-8")
    assert "MEASUREMENT_POLICY_REJECTED" in log
    assert not any(line.startswith("$ ") for line in log.splitlines())


def test_invalid_environment_report_fails_as_structured_result(step, tmp_path):
    registry, request = step

    def report_invalid_version(context):
        (context.workdir / "outputs" / "table.csv").write_text("id,area\n1,1\n", encoding="utf-8")
        return {"python": " "}

    result = execute_adapter(
        request, registry=registry, workdir=tmp_path, runner="python", invoke=report_invalid_version
    )

    assert result["status"] == "FAILED" and result["exit_code"] == 0
    assert result["error"]["code"] == "OUTPUT_INVALID"
    assert result["error"]["field"] == "environment"
    assert not result["outputs"] and not result["environment"]
