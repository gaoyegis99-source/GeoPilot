from pathlib import Path

import pytest
import yaml

from geopilot.api.server import _worker_paths
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.llm.candidate_validator import validate_model_candidate
from geopilot.llm.context_builder import build_outbound_context
from geopilot.llm.prompt_builder import build_model_request
from geopilot.methods.loader import MethodCatalog, load_method_catalog
from geopilot.methods.models import MethodRef
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOAL = TaskGoal(summary="统计道路缓冲内的建筑物")
METHOD = MethodRef(id="road-buffer-statistics", version=2)
OPTIONS = {
    "distance_m": {"value": 0.5, "unit": "kilometer"}, "distance_method": "local_projected",
    "distance_crs_ref": "EPSG:32650", "spatial_relation": "intersects",
    "boundary_assignment": "centroid", "boundary_tie": "unassigned",
    "statistic": "count", "output_scope": "clipped_to_analysis_extent",
}


@pytest.fixture(scope="module")
def engine():
    tools = [
        ToolContract.model_validate(yaml.safe_load(path.read_text()))
        for path in (ROOT / "config/tools").glob("*.yaml")
    ]
    wrapper = ROOT / ".environments/qgis-deb/qgis_process"
    registry = ToolRegistry(qgis_process=wrapper if wrapper.is_file() else None, python_implementations=_worker_paths(tools))
    for tool in tools:
        registry.register(tool)
    if not registry.get("qgis-buffer", 1).available:
        pytest.skip("A real QGIS Processing entry point is required")
    return load_method_catalog(ROOT / "config/methods"), registry


@pytest.fixture
def candidate(tmp_path, engine):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    catalog, registry = engine
    source = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
    assets = [register_asset(database, source, layer=layer) for layer in ("analysis_extent", "roads", "targets", "zones")]
    refs = {asset.source.layer: asset.ref for asset in assets}
    local = build_task_context(
        database, task_id="road-task", goal=GOAL, method=catalog.get(METHOD.id, METHOD.version),
        selected=refs, registry=registry,
    )
    request = build_model_request(build_outbound_context(local, assets), catalog, registry=registry, registered_rules=RUN_CHECKS)
    return {
        "database_path": database, "task_id": "road-task", "plan_id": "suggested-plan", "method": METHOD,
        "goal": GOAL, "inputs": refs, "options": OPTIONS, "request": request,
        "catalog": catalog, "registry": registry, "registered_rules": RUN_CHECKS,
    }


def test_reuses_local_method_without_confirming_or_writing(candidate):
    outcome = validate_model_candidate(**candidate)
    assert outcome.issues == ()
    plan = outcome.plan
    assert plan.status == "draft"
    definition = candidate["catalog"].get(METHOD.id, METHOD.version)
    assert [(step.key, step.tool) for step in plan.steps] == [(step.key, step.tool) for step in definition.steps]
    assert plan.inputs == candidate["inputs"] and plan.critical_options == OPTIONS
    with open_database(candidate["database_path"], read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM analysis_plan").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 0


def test_invalid_and_unknown_parameters_become_located_draft_issues(candidate):
    candidate["options"] = {**OPTIONS, "distance_m": 0, "statistic": "mean", "replace_tool": "external"}
    result = validate_model_candidate(**candidate)
    assert result.plan.status == "draft"
    fields = {issue.field for issue in result.issues}
    assert fields >= {"critical_options.distance_m", "critical_options.statistic", "critical_options.replace_tool"}
    assert result.plan.issues == result.issues


def test_rejects_unadvertised_method_and_stale_registry(candidate):
    unadvertised = validate_model_candidate(**{**candidate, "method": MethodRef(id="road-buffer-statistics", version=1)})
    assert unadvertised.plan is None and unadvertised.issues[0].code == "MODEL_CANDIDATE_UNKNOWN"
    missing = validate_model_candidate(**{**candidate, "catalog": MethodCatalog([], ())})
    assert missing.plan is None and missing.issues[0].code == "MODEL_CANDIDATE_UNAVAILABLE"
    unavailable = validate_model_candidate(**{**candidate, "registry": ToolRegistry()})
    assert unavailable.plan is None and unavailable.issues[0].code == "MODEL_CANDIDATE_UNAVAILABLE"


def test_rejects_changed_constraints_under_the_same_method_version(candidate):
    current = candidate["catalog"].get(METHOD.id, METHOD.version)
    options = current.critical_options.copy()
    options["distance_m"] = options["distance_m"].model_copy(update={"maximum": 400})
    changed = current.model_copy(update={"critical_options": options})
    result = validate_model_candidate(**{**candidate, "catalog": MethodCatalog([changed], ())})
    assert result.plan is None and result.issues[0].code == "MODEL_CANDIDATE_CHANGED"


def test_reports_local_missing_roles_without_substituting_model_data(candidate):
    candidate["inputs"] = {"roads": candidate["inputs"]["roads"]}
    result = validate_model_candidate(**candidate)
    assert result.plan is not None
    assert {issue.field for issue in result.issues if issue.code == "INPUT_REQUIRED"} == {
        "inputs.analysis_extent", "inputs.targets", "inputs.zones",
    }
