from pathlib import Path
from shutil import copyfile

import pytest
import yaml

from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.schema import initialize_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import save_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def project(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    source = tmp_path / "roads.gpkg"
    copyfile(ROOT / "tests/fixtures/vector/road-buffer.gpkg", source)
    asset = register_asset(database, source, layer="roads")
    tool = ToolContract.model_validate(yaml.safe_load(
        (ROOT / "config/tools/python-prepare-measurement-lines.yaml").read_text(encoding="utf-8")
    ))
    registry = ToolRegistry(python_implementations={tool.implementation})
    registry.register(tool)
    method = MethodDefinition.model_validate({
        "id": "prepare-lines", "version": 1, "locked_order": True,
        "inputs": {"lines": "vector_line"}, "critical_options": {},
        "steps": [{
            "key": "prepare", "tool": tool.ref,
            "inputs": {"source": {"asset": "lines"}},
            "parameters": {"method": {"literal": "local_projected"}, "target_crs": {"literal": "EPSG:32650"}},
            "outputs": {"roads": "vector_line"},
        }],
        "expected_artifacts": [{"role": "roads", "step": "prepare", "output": "roads", "type": "vector_line"}],
        "check_rules": ["output_exists"],
    })
    context = build_task_context(
        database, task_id="task-lines", goal=TaskGoal(summary="Prepare the measured road input"),
        method=method, selected={"lines": asset.ref}, registry=registry,
    )
    plan = build_draft(context, method, plan_id="plan-lines")
    assert not plan.issues
    save_draft(database, plan, context=context)
    return database, source, tmp_path / "workspace", plan, MethodCatalog([method], ()), registry
