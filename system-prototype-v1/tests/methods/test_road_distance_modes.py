import os
import shutil
from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest
import yaml
from fastapi.testclient import TestClient
from shapely.geometry import LineString, box

from geopilot.api.app import create_app
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.application.task_context import ContextCriteria, build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import load_method_catalog
from geopilot.methods.validator import validate_method
from geopilot.persistence.schema import initialize_database
from geopilot.plans.compiler import PlanCompilationError, compile_plan_steps
from geopilot.plans.models import AnalysisPlan
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
ROAD = "road-buffer-statistics"
ROLES = ("analysis_extent", "roads", "targets", "zones")
LOCAL = (
    "qgis-expand-analysis-extent", "qgis-select-roads-by-location", "python-prepare-measurement-lines", "qgis-buffer",
    "qgis-select-points-by-location", "python-clip-points-to-extent", "python-count-points-by-zone",
)
GEODESIC = (
    "python-expand-wgs84-analysis-extent", "python-select-wgs84-roads", "python-prepare-wgs84-measurement-lines",
    "python-wgs84-geodesic-buffer", *LOCAL[4:],
)
OPTIONS = {
    "distance_m": {"value": 0.5, "unit": "kilometer"}, "spatial_relation": "intersects",
    "boundary_assignment": "centroid", "boundary_tie": "unassigned", "statistic": "count",
    "output_scope": "clipped_to_analysis_extent",
}


@pytest.fixture(scope="module")
def catalog():
    return load_method_catalog(ROOT / "config/methods")


@pytest.fixture(scope="module")
def registry():
    bundled = ROOT / ".environments/qgis-deb/qgis_process"
    executable = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if executable is None and bundled.is_file():
        executable = str(bundled)
    if executable is None:
        pytest.skip("A QGIS process is required for the road method's real contracts")
    contracts = [ToolContract.model_validate(yaml.safe_load(
        (ROOT / "config/tools" / f"{name}.yaml").read_text(encoding="utf-8"),
    )) for name in dict.fromkeys((*LOCAL, *GEODESIC))]
    registered = ToolRegistry(
        qgis_process=executable,
        python_implementations={contract.implementation for contract in contracts if contract.runner == "python"},
    )
    for contract in contracts:
        registered.register(contract)
    return registered


@pytest.fixture
def project(tmp_path):
    source = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    inputs = {role: register_asset(database, source, layer=role).ref.model_dump(mode="json") for role in ROLES}
    targets = pyogrio.read_dataframe(source, layer="targets").to_crs("EPSG:4326")
    mixed = tmp_path / "targets-wgs84.gpkg"
    pyogrio.write_dataframe(targets, mixed, layer="targets")
    return database, inputs, register_asset(database, mixed).ref.model_dump(mode="json")


def _tools(steps):
    return tuple((step.tool.id, step.tool.version) if step.tool is not None else None for step in steps)


def test_formal_road_method_has_two_exact_branches_and_preserves_version_two(catalog):
    previous = catalog.get(ROAD, 2)
    current = catalog.get(ROAD)
    assert current.version == 3 and current.locked_order
    assert _tools(previous.steps) == tuple((tool, 1) for tool in LOCAL)
    assert current.critical_options["distance_method"].values == ("local_projected", "geodesic")
    assert [step.key for step in current.steps] == [step.key for step in previous.steps]
    for mode, tools in (("local_projected", LOCAL), ("geodesic", GEODESIC)):
        steps = current.steps_for({"distance_method": mode})
        assert _tools(steps) == tuple((tool, 1) for tool in tools)
        assert [step.key for step in steps] == [step.key for step in previous.steps]
    assert current.steps_for({})[0].tool is None


def test_geodesic_context_uses_the_road_arc_and_keeps_candidates_while_distance_is_unset(catalog, registry, tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    package = tmp_path / "scope-and-roads.gpkg"
    near = LineString([(12.0, 65.0), (14.0, 65.0)])
    # The T04 independent reference locates this point 999.73–999.75 m from the arc
    # and 1370.82 m from the coordinate chord.
    x, y = 13.074030959235484, 65.01229514782054
    scope = box(x - 0.00002, y - 0.00002, x + 0.00002, y + 0.00002)
    far = LineString([(12.0, 64.98), (14.0, 64.98)])
    shapes = {"analysis_extent": scope, "near": near, "far": far}
    for name, geometry in shapes.items():
        frame = gpd.GeoDataFrame({"source_feature_id": [name]}, geometry=[geometry], crs="EPSG:4326")
        pyogrio.write_dataframe(frame, package, layer=name)
    refs = {name: register_asset(database, package, layer=name).ref for name in shapes}
    method = catalog.get(ROAD, 3)
    arguments = {
        "task_id": "road-candidates", "goal": TaskGoal(summary="核对高纬度测地弧"),
        "method": method, "registry": registry, "criteria": ContextCriteria(expected_artifact="buffer_extent"),
        "selected": {"analysis_extent": refs["analysis_extent"], "roads": refs["near"]},
    }
    pending = build_task_context(database, **arguments, options={"distance_method": "geodesic"})
    assert {entry.asset for entry in pending.candidates["roads"]} == {refs["near"], refs["far"]}
    measured = build_task_context(database, **arguments, options={"distance_method": "geodesic", "distance_m": 1000})
    assert {entry.asset for entry in measured.candidates["roads"]} == {refs["near"]}
    assert measured.roles["roads"].status == "READY"


def test_road_method_identifies_missing_source_id_in_context_and_draft(catalog, registry, project, tmp_path):
    database, inputs, _ = project
    incomplete = tmp_path / "roads-without-id.gpkg"
    roads = pyogrio.read_dataframe(ROOT / "tests/fixtures/vector/road-buffer.gpkg", layer="roads")
    pyogrio.write_dataframe(roads.drop(columns="source_feature_id"), incomplete, layer="roads")
    bad_ref = register_asset(database, incomplete).ref.model_dump(mode="json")
    request = {
        "goal": {"summary": "核对道路源对象编号"}, "method": {"id": ROAD, "version": 3},
        "inputs": {**inputs, "roads": bad_ref}, "critical_options": {**OPTIONS, "distance_method": "geodesic"},
    }
    with TestClient(create_app(database, catalog=catalog, registry=registry)) as client:
        preview = client.post("/api/tasks/context-preview", json=request)
        assert preview.status_code == 200, preview.text
        context = preview.json()
        assert context["roles"]["roads"]["asset"] == bad_ref
        assert context["roles"]["roads"]["code"] == "INPUT_FIELD_MISSING"
        assert "source_feature_id" in context["roles"]["roads"]["message"]
        assert [entry["asset"] for entry in context["candidates"]["roads"]] == [inputs["roads"]]
        task = client.post("/api/tasks", json={"goal": request["goal"]}).json()["task_id"]
        draft = client.post(f"/api/tasks/{task}/plans", json=request)
        assert draft.status_code == 200, draft.text
        assert any(
            issue["code"] == "INPUT_FIELD_MISSING" and issue["field"] == "inputs.roads" for issue in draft.json()["issues"]
        )


@pytest.mark.parametrize(("version", "mode"), [(2, "local_projected"), (3, "local_projected"), (3, "geodesic")])
def test_real_catalog_preview_draft_and_confirmation_for_each_mode(catalog, registry, project, version, mode):
    database, inputs, mixed_target = project
    selected = {**inputs, "targets": mixed_target} if mode == "geodesic" else inputs
    options = {**OPTIONS, "distance_method": mode}
    if mode == "local_projected":
        options["distance_crs_ref"] = "EPSG:32650"
    method = catalog.get(ROAD, version)
    assert validate_method(method, registry=registry, registered_rules=RUN_CHECKS) == ()

    with TestClient(create_app(database, catalog=catalog, registry=registry)) as client:
        entry = next(entry for entry in client.get("/api/methods").json()["methods"] if
                     (entry["definition"]["id"], entry["definition"]["version"]) == (ROAD, version))
        assert entry["available"], entry
        request = {"goal": {"summary": "用 500 米道路影响范围分区统计"}, "method": method.ref.model_dump(mode="json"),
                   "inputs": selected, "critical_options": options, "criteria": {"expected_artifact": "zone_statistics"}}
        context = client.post("/api/tasks/context-preview", json=request)
        assert context.status_code == 200, context.text
        assert {role: value["status"] for role, value in context.json()["roles"].items()} == dict.fromkeys(ROLES, "READY")
        task = client.post("/api/tasks", json={"goal": request["goal"]}).json()["task_id"]
        response = client.post(f"/api/tasks/{task}/plans", json=request)
        assert response.status_code == 200 and not response.json()["issues"], response.text
        draft = AnalysisPlan.model_validate(response.json())
        response = client.post(f"/api/plans/{draft.id}/confirm", json={"plan_version": draft.version})
        assert response.status_code == 200, response.text
        plan = AnalysisPlan.model_validate(response.json())

    expected = GEODESIC if mode == "geodesic" else LOCAL
    assert _tools(draft.steps) == _tools(plan.steps) == tuple((tool, 1) for tool in expected)
    assert plan.steps[0].parameters["distance"] == plan.steps[3].parameters["distance"] == 500
    assert compile_plan_steps(plan, method, registry=registry) == tuple(
        step.model_copy(update={"tool_check_rules": None}) for step in plan.steps
    )
    for step in plan.steps:
        contract = registry.get(step.tool.id, step.tool.version).contract
        assert [rule.name for rule in step.tool_check_rules] == list(contract.check_rules)
    if mode == "geodesic":
        assert "distance_crs_ref" not in plan.critical_options
        assert plan.steps[0].parameters == {"distance": 500, "target_crs": "EPSG:4326"}
        assert plan.steps[2].parameters == {"method": "geodesic", "target_crs": "EPSG:4326"}
        assert plan.steps[4].inputs["source"].output == "targets"
        assert plan.steps[5].inputs["extent"].output == "analysis_extent"
        assert plan.steps[6].inputs["zones"].output == "zones"
        altered = plan.model_copy(update={"steps": (*plan.steps[:3], plan.steps[3].model_copy(update={
            "tool": catalog.get(ROAD, 2).steps[3].tool,
        }), *plan.steps[4:])})
        with pytest.raises(PlanCompilationError, match="tool version differs"):
            compile_plan_steps(altered, method, registry=registry)


@pytest.mark.parametrize(("options", "issue", "field"), [
    ({}, "OPTION_REQUIRED", "critical_options.distance_method"),
    ({"distance_method": "geodesic", "distance_crs_ref": "EPSG:32650"},
     "OPTION_NOT_APPLICABLE", "critical_options.distance_crs_ref"),
    ({"distance_method": "geodesic", "distance_m": {"value": 2.1, "unit": "kilometer"}},
     "INVALID_PARAMETER", "critical_options.distance_m"),
])
def test_mode_dependent_options_are_rejected_in_the_draft(catalog, registry, project, options, issue, field):
    database, inputs, _ = project
    request = {"goal": {"summary": "核对明确选择的道路距离方式"}, "method": {"id": ROAD, "version": 3},
               "inputs": inputs, "critical_options": {**OPTIONS, **options}, "criteria": {}}
    with TestClient(create_app(database, catalog=catalog, registry=registry)) as client:
        task = client.post("/api/tasks", json={"goal": request["goal"]}).json()["task_id"]
        response = client.post(f"/api/tasks/{task}/plans", json=request)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert (issue, field) in {(item["code"], item["field"]) for item in draft["issues"]}
    if "distance_method" not in options:
        assert all(draft["steps"][index]["tool"] is None for index in range(4))
