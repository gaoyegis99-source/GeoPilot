import json
from pathlib import Path

import pyogrio
import pytest

from geopilot.application.plan_runner import execute_claimed_run
from geopilot.application.task_context import build_task_context
from geopilot.domain.task import TaskGoal
from geopilot.persistence.run_repository import get_run, list_run_steps
from geopilot.persistence.schema import open_database
from geopilot.plans.compiler import PlanCompilationError
from geopilot.plans.service import confirm_plan
from geopilot.plans.versioning import revise_plan
from geopilot.runs.retry_service import retry_run
from geopilot.runs.service import create_run
from geopilot.scheduler.loop import run_once


WORKER = Path(__file__).resolve().parents[2] / "src/geopilot/gis/tools/prepare_measurement_lines.py"


@pytest.mark.parametrize("revise", [False, True])
def test_retry_uses_a_fresh_run_and_the_exact_confirmed_version(project, revise):
    database, _, workspace, draft, catalog, registry = project
    options = {"catalog": catalog, "registry": registry, "registered_rules": {"output_exists"}}
    confirmed = confirm_plan(database, draft.id, draft.version, **options)
    old_id = create_run(database, confirmed.id, confirmed.version, workspace_root=workspace)
    assert retry_run(database, old_id, workspace_root=workspace) == old_id

    def execute(run, implementations):
        return execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry, python_implementations=implementations,
        )

    assert run_once(database, lambda run: execute(run, {})) == old_id
    with open_database(database, read_only=True) as connection:
        old = get_run(connection, old_id)
        old_steps = list_run_steps(connection, old_id)
    assert old.execution_status == "FAILED" and old_steps[0].result.error.code == "PROCESS_START_FAILED"
    old_path = Path(old.workdir)
    request_bytes = (old_path / "request.json").read_bytes()
    log_bytes = (old_path / old_steps[0].log_path).read_bytes()

    new_plan = confirmed
    if revise:
        method = catalog.get(confirmed.method.id, confirmed.method.version)
        context = build_task_context(
            database, task_id=confirmed.task_id, goal=TaskGoal(summary="Prepare roads after correcting the goal"),
            method=method, selected=confirmed.inputs, registry=registry,
        )
        revision = revise_plan(database, confirmed.id, confirmed.version, context=context, method=method)
        assert revision.version == 2
        with pytest.raises(PlanCompilationError, match="Confirm"):
            retry_run(database, old_id, plan_version=revision.version, workspace_root=workspace)
        new_plan = confirm_plan(database, revision.id, revision.version, **options)

    new_id = retry_run(
        database, old_id, workspace_root=workspace, plan_version=new_plan.version if revise else None,
    )
    assert new_id != old_id
    with open_database(database, read_only=True) as connection:
        new = get_run(connection, new_id)
        assert list_run_steps(connection, new_id) == ()
    new_path = Path(new.workdir)
    assert new_path != old_path and new.plan_version == new_plan.version
    assert not list((new_path / "steps").iterdir()) and not list((new_path / "intermediate").iterdir())
    assert json.loads((new_path / "request.json").read_text())["plan"] == new_plan.model_dump(mode="json")
    implementation = registry.get(new_plan.steps[0].tool.id, new_plan.steps[0].tool.version).contract.implementation
    assert run_once(database, lambda run: execute(run, {implementation: WORKER})) == new_id
    with open_database(database, read_only=True) as connection:
        assert get_run(connection, old_id) == old and list_run_steps(connection, old_id) == old_steps
        new = get_run(connection, new_id)
        first, = list_run_steps(connection, new_id)
    assert new.execution_status == "SUCCEEDED" and new.check_status == "PASSED"
    assert first.position == 0 and first.status == "SUCCEEDED" and first.result.exit_code == 0
    roads = first.result.outputs["roads"]
    assert len(pyogrio.read_dataframe(new_path / roads.path, layer=roads.layer)) == 3
    assert (old_path / "request.json").read_bytes() == request_bytes
    assert (old_path / old_steps[0].log_path).read_bytes() == log_bytes


def test_unknown_run_cannot_be_retried(project):
    database, _, workspace, _, _, _ = project
    with pytest.raises(LookupError, match="unknown"):
        retry_run(database, "unknown", workspace_root=workspace)
    assert not workspace.exists()
