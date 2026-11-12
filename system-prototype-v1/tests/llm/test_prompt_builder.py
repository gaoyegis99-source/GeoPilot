from pathlib import Path

import pytest
import yaml

from geopilot.api.server import _worker_paths
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.llm.context_builder import build_outbound_context
from geopilot.llm.prompt_builder import build_model_request
from geopilot.methods.loader import MethodCatalog, load_method_catalog
from geopilot.persistence.schema import initialize_database
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
QGIS = ROOT / ".environments/qgis-deb/qgis_process"


@pytest.fixture(scope="module")
def registry():
    tool_contracts = [
        ToolContract.model_validate(yaml.safe_load(path.read_text()))
        for path in (ROOT / "config/tools").glob("*.yaml")
    ]
    registry = ToolRegistry(qgis_process=QGIS if QGIS.is_file() else None, python_implementations=_worker_paths(tool_contracts))
    for contract in tool_contracts:
        registry.register(contract)
    if not registry.get("qgis-extract-by-attribute", 1).available:
        pytest.skip("A real QGIS Processing entry point is required for candidate discovery")
    return registry


@pytest.fixture
def context(tmp_path):
    db = tmp_path / "catalog.sqlite"
    initialize_database(db)
    path = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
    asset = register_asset(db, path, layer="targets")
    method = load_method_catalog(ROOT / "config/methods").get("attribute-filter-export", 1)
    task = build_task_context(
        db, task_id="local-task-id", goal=TaskGoal(summary=f"从 {path} 筛选建筑"), method=method,
        selected={"features": asset.ref},
    )
    return build_outbound_context(task, [asset])


def test_only_real_available_method_and_tool_versions_are_serialized(context, registry):
    catalog = load_method_catalog(ROOT / "config/methods")
    request = build_model_request(context, catalog, registry=registry, registered_rules=RUN_CHECKS)
    wire = request.model_dump(mode="json")["payload"]
    assert set(wire) == {"context", "candidate_methods", "candidate_tools"}
    methods = {(item["id"], item["version"]): item for item in wire["candidate_methods"]}
    assert ("attribute-filter-export", 1) in methods
    assert ("road-buffer-statistics", 1) not in methods
    road = methods[("road-buffer-statistics", 2)]
    assert [step["key"] for step in road["steps"]] == [
        step.key for step in catalog.get("road-buffer-statistics", 2).steps
    ]
    options = {item["name"]: item for item in road["options"]}
    assert options["distance_m"] == {"name": "distance_m", "type": "number", "unit": "meter",
                                     "minimum_exclusive": 0, "required": True}
    assert options["spatial_relation"]["values"] == ["intersects", "within"]
    assert options["distance_crs_ref"]["required_for"] == "local_projected"
    assert {"id": "qgis-extract-by-attribute", "version": 1} in wire["candidate_tools"]
    assert len(wire["candidate_tools"]) == len({(item["id"], item["version"]) for item in wire["candidate_tools"]})
    serialized = request.model_dump_json()
    for prohibited in ("road-buffer.gpkg", "local-task-id", str(ROOT), "implementation", "native:buffer", "4400300"):
        assert prohibited not in serialized


def test_new_registered_method_changes_candidates_without_a_prompt_template_change(context, registry):
    original = load_method_catalog(ROOT / "config/methods").get("attribute-filter-export", 1)
    copied = original.model_copy(update={"id": "attribute-filter-alternative", "version": 3})
    bad_label = original.model_copy(update={"id": "/private/data"})
    missing_tool = original.model_copy(update={"id": "unavailable", "steps": (
        original.steps[0].model_copy(update={"tool": None}),
    )})
    catalog = MethodCatalog([original, copied, bad_label, missing_tool], ())
    payload = build_model_request(context, catalog, registry=registry, registered_rules=RUN_CHECKS).payload
    assert [(entry["id"], entry["version"]) for entry in payload["candidate_methods"]] == [
        ("attribute-filter-alternative", 3), ("attribute-filter-export", 1),
    ]
    assert payload["candidate_tools"] == [{"id": "qgis-extract-by-attribute", "version": 1}]


def test_missing_registered_rules_do_not_become_candidates(context, registry):
    method = load_method_catalog(ROOT / "config/methods").get("attribute-filter-export", 1)
    payload = build_model_request(context, MethodCatalog([method], ()), registry=registry, registered_rules=()).payload
    assert payload["candidate_methods"] == []
    assert payload["candidate_tools"] == []
