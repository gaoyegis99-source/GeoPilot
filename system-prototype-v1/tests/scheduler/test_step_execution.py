import json
from pathlib import Path

import pyogrio
import pytest
import yaml
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.application.plan_runner import execute_claimed_run
from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.run_repository import get_run, list_run_steps
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, save_draft
from geopilot.runs.service import create_run
from geopilot.scheduler import step_executor
from geopilot.scheduler.loop import run_once
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"


@pytest.mark.parametrize("fault", [None, "unavailable", "missing_output", "corrupt_output"])
def test_claimed_run_uses_real_steps_in_order_and_stops_on_failure(tmp_path, monkeypatch, fault):
    db = tmp_path / "project.sqlite"
    workspace = tmp_path / "workspace"
    initialize_database(db)
    tool = ToolContract.model_validate(yaml.safe_load(
        (ROOT / "config/tools/python-prepare-measurement-lines.yaml").read_text(encoding="utf-8")
    ))
    middle = tool.model_copy(update={"id": "prepare-lines-middle"})
    registry = ToolRegistry(python_implementations={tool.implementation})
    registry.register(tool)
    registry.register(middle)
    method = MethodDefinition.model_validate({
        "id": "ordered-lines", "version": 1, "locked_order": True,
        "inputs": {"lines": "vector_line"}, "critical_options": {},
        "steps": [
            {"key": key, "tool": bound_tool.ref, "inputs": {"source": source},
             "parameters": {"method": {"literal": "local_projected"}, "target_crs": {"literal": "EPSG:32650"}},
             "outputs": {"roads": "vector_line"}}
            for key, bound_tool, source in (
                ("prepare", tool, {"asset": "lines"}),
                ("verify", middle, {"step": "prepare", "output": "roads"}),
                ("finalize", tool, {"step": "verify", "output": "roads"}),
            )
        ],
        "expected_artifacts": [{"role": "roads", "step": "finalize", "output": "roads", "type": "vector_line"}],
        "check_rules": ["output_exists"],
    })
    original = fingerprint_file(SOURCE).digest
    asset = register_asset(db, SOURCE, layer="roads")
    context = build_task_context(
        db, task_id="task", goal=TaskGoal(summary="Prepare measured road lines"),
        method=method, selected={"lines": asset.ref}, registry=registry,
    )
    draft = build_draft(context, method, plan_id="plan")
    save_draft(db, draft, context=context)
    confirmed = confirm_plan(
        db, draft.id, draft.version, catalog=MethodCatalog([method], ()), registry=registry,
        registered_rules={"output_exists"},
    )
    run_id = create_run(db, confirmed.id, confirmed.version, workspace_root=workspace)
    if fault == "unavailable":
        registry = ToolRegistry(python_implementations={tool.implementation})
        registry.register(tool)
    if fault in {"missing_output", "corrupt_output"}:
        run_step = step_executor.run_step

        def run_then_change_file(request, **kwargs):
            result = run_step(request, **kwargs)
            if request.step_key == "prepare" and result.status == "SUCCEEDED":
                target = kwargs["workdir"] / result.outputs["roads"].path
                if fault == "missing_output":
                    target.unlink()
                else:
                    target.write_bytes(b"not a geopackage")
            return result

        monkeypatch.setattr(step_executor, "run_step", run_then_change_file)

    def execute(run):
        return execute_claimed_run(
            db, run, workspace_root=workspace, registry=registry,
            python_implementations={
                tool.implementation: ROOT / "src/geopilot/gis/tools/prepare_measurement_lines.py",
            },
        )

    with TestClient(create_app(db)) as client:
        path = f"/api/runs/{run_id}"
        assert client.get(path).json()["execution_status"] == "NOT_STARTED"
        assert run_once(db, execute) == run_id
        response = client.get(path)
        assert response.status_code == 200
        status = response.json()
    with open_database(db) as connection:
        run = get_run(connection, run_id)
        steps = list_run_steps(connection, run_id)
    assert status == {
        "run_id": run_id, "execution_status": run.execution_status.value,
        "check_status": run.check_status.value, "current_step": run.current_step,
        "cancel_requested": run.cancel_requested, "current_report_id": run.current_report_id,
        "error": run.error.model_dump(mode="json") if run.error else None,
    }
    assert [step.step_key for step in steps] == ["prepare", "verify", "finalize"]
    assert [step.position for step in steps] == [0, 1, 2]
    first, second, third = steps
    path = Path(run.workdir)
    assert first.status == "SUCCEEDED" and first.result.exit_code == 0
    assert (path / first.log_path).is_file()
    assert "input_check:" in (path / first.log_path).read_text(encoding="utf-8")
    first_output = first.result.outputs["roads"]
    assert second.request.inputs["source"].path == first_output.path
    assert second.request.inputs["source"].layer == first_output.layer
    if fault:
        assert (run.execution_status, run.check_status) == ("FAILED", "NOT_CHECKED")
        assert run.current_step == "verify"
        assert second.status == "FAILED"
        assert second.result.error.code == {
            "unavailable": "TOOL_UNAVAILABLE", "missing_output": "OUTPUT_MISSING", "corrupt_output": "OUTPUT_INVALID",
        }[fault]
        if fault != "unavailable":
            assert second.result.exit_code is None
            assert not (path / "steps/verify").exists()
        assert third.status == "NOT_STARTED" and third.request is None and third.result is None
        assert not (path / "steps/finalize").exists()
    else:
        assert (run.execution_status, run.check_status) == ("SUCCEEDED", "PASSED")
        assert [step.result.exit_code for step in steps] == [0, 0, 0]
        manifest = json.loads((path / "outputs/manifest.json").read_text(encoding="utf-8"))
        output = manifest["artifacts"]["roads"]
        roads = pyogrio.read_dataframe(path / output["path"], layer=output["layer"])
        assert sorted(roads["source_feature_id"]) == ["road-edge", "road-far", "road-main"]
    assert fingerprint_file(SOURCE).digest == original
