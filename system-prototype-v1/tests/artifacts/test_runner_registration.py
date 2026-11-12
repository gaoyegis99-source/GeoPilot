import json
from pathlib import Path

import pyogrio
import pytest
import yaml

from geopilot.application.plan_runner import execute_claimed_run
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.application.task_context import build_task_context
from geopilot.artifacts.service import list_artifacts
from geopilot.data.assets import register_asset
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


@pytest.mark.parametrize("fault", [None, "missing", "damaged"])
def test_actual_steps_register_only_the_complete_batch_before_checks(tmp_path, monkeypatch, fault):
    database = tmp_path / "project.sqlite"
    workspace = tmp_path / "workspace"
    initialize_database(database)
    tool = ToolContract.model_validate(yaml.safe_load(
        (ROOT / "config/tools/python-prepare-measurement-lines.yaml").read_text(encoding="utf-8")
    ))
    registry = ToolRegistry(python_implementations={tool.implementation})
    registry.register(tool)
    method = MethodDefinition.model_validate({
        "id": "two-artifacts", "version": 1, "locked_order": True,
        "inputs": {"roads": "vector_line"}, "critical_options": {},
        "steps": [
            {"key": key, "tool": tool.ref, "inputs": {"source": source},
             "parameters": {"method": {"literal": "local_projected"}, "target_crs": {"literal": "EPSG:32650"}},
             "outputs": {"roads": "vector_line"}}
            for key, source in (("prepare", {"asset": "roads"}), ("finish", {"step": "prepare", "output": "roads"}))
        ],
        "expected_artifacts": [
            {"role": role, "step": step, "output": "roads", "type": "vector_line"}
            for role, step in (("prepared", "prepare"), ("finished", "finish"))
        ],
        "check_rules": ["output_exists"],
    })
    asset = register_asset(database, SOURCE, layer="roads")
    context = build_task_context(
        database, task_id="task", goal=TaskGoal(summary="Register two real line artifacts"),
        method=method, selected={"roads": asset.ref}, registry=registry,
    )
    draft = build_draft(context, method, plan_id="plan")
    save_draft(database, draft, context=context)
    plan = confirm_plan(database, draft.id, draft.version, catalog=MethodCatalog([method], ()), registry=registry,
                        registered_rules={"output_exists"})
    run_id = create_run(database, plan.id, plan.version, workspace_root=workspace)

    if fault:
        actual_run_step = step_executor.run_step

        def run_then_fault(request, **kwargs):
            result = actual_run_step(request, **kwargs)
            if request.step_key == "finish" and result.status == "SUCCEEDED":
                path = kwargs["workdir"] / result.outputs["roads"].path
                if fault == "missing":
                    path.unlink()
                else:
                    path.write_bytes(b"not a geopackage")
            return result

        monkeypatch.setattr(step_executor, "run_step", run_then_fault)

    check_calls = []

    def check(check_context):
        with open_database(database) as connection:
            run = get_run(connection, run_id)
            assert (run.execution_status, run.check_status) == ("SUCCEEDED", "CHECKING")
            check_calls.extend(record.role for record in list_artifacts(connection, run_id))
        return RUN_CHECKS["output_exists"](check_context)

    assert run_once(database, lambda run: execute_claimed_run(
        database, run, workspace_root=workspace, registry=registry, check_rules={"output_exists": check},
        python_implementations={tool.implementation: ROOT / "src/geopilot/gis/tools/prepare_measurement_lines.py"},
    )) == run_id
    with open_database(database) as connection:
        run = get_run(connection, run_id)
        records = {record.role: record for record in list_artifacts(connection, run_id)}
        steps = list_run_steps(connection, run_id)
    workdir = Path(run.workdir)
    manifest = workdir / "outputs/manifest.json"
    assert [(step.step_key, step.status, step.result.exit_code) for step in steps] == [
        ("prepare", "SUCCEEDED", 0), ("finish", "SUCCEEDED", 0),
    ]
    assert all((workdir / step.log_path).is_file() for step in steps)
    assert (workdir / steps[0].result.outputs["roads"].path).is_file()

    if fault:
        assert (run.execution_status, run.check_status) == ("FAILED", "NOT_CHECKED")
        assert run.error.code == ("OUTPUT_MISSING" if fault == "missing" else "OUTPUT_INVALID")
        assert not records and not manifest.exists() and not check_calls
    else:
        assert (run.execution_status, run.check_status) == ("SUCCEEDED", "PASSED")
        assert set(records) == set(check_calls) == {"prepared", "finished"}
        published = json.loads(manifest.read_text(encoding="utf-8"))["artifacts"]
        for role, record in records.items():
            assert record.source.inputs == {"roads": asset.ref}
            assert (record.source.plan_id, record.source.plan_version, record.source.tool) == (plan.id, plan.version, tool.ref)
            assert published[role]["fingerprint"] == record.fingerprint
            reopened = pyogrio.read_dataframe(workdir / record.uri, layer=record.layer)
            assert reopened.crs.to_epsg() == 32650
            assert sorted(reopened.source_feature_id) == ["road-edge", "road-far", "road-main"]
