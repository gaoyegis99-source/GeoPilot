from pathlib import Path

import pytest
import yaml

from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.run_repository import get_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, save_draft
from geopilot.runs.service import create_run
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def queued_run(tmp_path):
    database = tmp_path / "project.sqlite"
    workspace = tmp_path / "workspace"
    initialize_database(database)
    tool = ToolContract.model_validate(yaml.safe_load(
        (ROOT / "config/tools/python-prepare-measurement-lines.yaml").read_text(encoding="utf-8")
    ))
    middle = tool.model_copy(update={"id": "prepare-measurement-middle", "implementation": "test.measurement_middle"})
    registry = ToolRegistry(python_implementations={tool.implementation, middle.implementation})
    registry.register(tool)
    registry.register(middle)
    method = MethodDefinition.model_validate({
        "id": "ordered-lines", "version": 1, "locked_order": True,
        "inputs": {"lines": "vector_line"}, "critical_options": {},
        "steps": [
            {"key": key, "tool": selected_tool.ref, "inputs": {"source": source},
             "parameters": {"method": {"literal": "local_projected"}, "target_crs": {"literal": "EPSG:32650"}},
             "outputs": {"roads": "vector_line"}}
            for key, selected_tool, source in (
                ("prepare", tool, {"asset": "lines"}),
                ("consume", middle, {"step": "prepare", "output": "roads"}),
                ("finalize", tool, {"step": "consume", "output": "roads"}),
            )
        ],
        "expected_artifacts": [{"role": "roads", "step": "finalize", "output": "roads", "type": "vector_line"}],
        "check_rules": ["output_exists"],
    })
    asset = register_asset(database, ROOT / "tests/fixtures/vector/road-buffer.gpkg", layer="roads")
    context = build_task_context(
        database, task_id="task", goal=TaskGoal(summary="Prepare roads with cancellation"), method=method,
        selected={"lines": asset.ref}, registry=registry,
    )
    draft = build_draft(context, method, plan_id="plan")
    save_draft(database, draft, context=context)
    confirm_plan(
        database, draft.id, draft.version, catalog=MethodCatalog([method], ()),
        registry=registry, registered_rules={"output_exists"},
    )
    run_id = create_run(database, draft.id, draft.version, workspace_root=workspace)
    with open_database(database) as connection:
        path = Path(get_run(connection, run_id).workdir)
    return database, workspace, registry, tool.implementation, run_id, path
