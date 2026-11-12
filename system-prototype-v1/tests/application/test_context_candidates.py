from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest
import yaml
from fastapi.testclient import TestClient
from shapely.geometry import LineString, Point, Polygon, box

from geopilot.api.app import create_app
from geopilot.application.task_context import ContextCriteria, build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
CRS = "EPSG:32632"
GOAL = TaskGoal(summary="在分析范围内筛选点对象并导出结果")


@pytest.fixture
def project(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    catalog = load_method_catalog(ROOT / "config/methods")

    def add(name, geometries, *, fields=None, crs=CRS):
        frame = gpd.GeoDataFrame(fields or {}, geometry=geometries, crs=CRS)
        if crs is None:
            frame = frame.set_crs(None, allow_override=True)
        elif crs != CRS:
            frame = frame.to_crs(crs)
        file = tmp_path / f"{name}.gpkg"
        pyogrio.write_dataframe(frame, file, layer=name)
        return register_asset(database, file, layer=name).ref

    return database, catalog, add


def _points(project):
    _, _, add = project
    shell = box(500000, 5100000, 500100, 5100100)
    hole = box(500030, 5100030, 500070, 5100070)
    scope = add("scope", [Polygon(shell.exterior.coords, [hole.exterior.coords])])
    return scope, {
        "matching": add("matching", [Point(500010, 5100010)], fields={"class_code": ["A"]}, crs="EPSG:4326"),
        "outside": add("outside", [Point(502010, 5100010)], fields={"class_code": ["A"]}),
        "hole": add("hole", [Point(500050, 5100050)], fields={"class_code": ["A"]}),
        "no_field": add("no_field", [Point(500020, 5100020)], fields={"other": ["A"]}),
        "no_crs": add("no_crs", [Point(500020, 5100020)], fields={"class_code": ["A"]}, crs=None),
        "wrong_type": add("wrong_type", [box(500010, 5100010, 500020, 5100020)], fields={"class_code": ["A"]}),
    }


def test_actual_candidates_use_task_geometry_field_and_scope_and_keep_selected_diagnosis(project):
    database, catalog, _ = project
    scope, samples = _points(project)
    method = catalog.get("attribute-filter-export", 1)
    criteria = ContextCriteria(scope=scope, object_types={"features": "vector_point"}, expected_artifact="filtered_features")
    options = {"field_name": "class_code", "match_value": "A"}

    for label, code in (("outside", "INPUT_SCOPE_DISJOINT"), ("hole", "INPUT_SCOPE_DISJOINT"),
                        ("no_field", "INPUT_FIELD_MISSING"), ("no_crs", "CRS_UNKNOWN")):
        context = build_task_context(
            database, task_id="task", goal=GOAL, method=method, selected={"features": samples[label]},
            criteria=criteria, options=options,
        )
        assert context.roles["features"].asset == samples[label]
        assert context.roles["features"].code == code and context.roles["features"].message
        candidates = {item.asset: item.status for item in context.candidates["features"]}
        assert candidates == {samples["matching"]: "READY", samples["no_crs"]: "UNKNOWN"}


def test_road_scope_uses_confirmed_distance_and_output_lineage(project):
    database, catalog, add = project
    area = add("area", [box(500000, 5100000, 500100, 5100100)])
    near = add("near", [LineString([(500400, 5100000), (500400, 5100100)])])
    far = add("far", [LineString([(500900, 5100000), (500900, 5100100)])])
    method = catalog.get("road-buffer-statistics", 2)
    args = dict(database_path=database, task_id="task", goal=GOAL, method=method,
                selected={"analysis_extent": area}, criteria=ContextCriteria(expected_artifact="buffer_extent"))

    pending = build_task_context(**args)
    assert {item.asset for item in pending.candidates["roads"]} == {near, far}
    assert all(item.status != "BLOCKED" for item in pending.candidates["roads"])
    assessed = build_task_context(**args, options={"distance_m": {"value": 0.5, "unit": "kilometer"}, "distance_crs_ref": CRS})
    assert {item.asset for item in assessed.candidates["roads"]} == {near}
    assert set(assessed.candidates) == {"analysis_extent", "roads"}
    assert {"targets", "zones"} <= assessed.roles.keys()


@pytest.mark.parametrize(("crs", "code"), [
    ("EPSG:3857", "MEASUREMENT_POLICY_REJECTED"),
    ("EPSG:4326", "MEASUREMENT_POLICY_REJECTED"),
    ("invalid-crs", "CRS_UNKNOWN"),
])
def test_invalid_road_measurement_crs_points_the_draft_to_the_option(project, crs, code):
    database, catalog, add = project
    area = add("extent", [box(500000, 5100000, 500100, 5100100)])
    roads = add("roads", [LineString([(500010, 5100010), (500090, 5100090)])],
                fields={"source_feature_id": ["road"]})
    method = catalog.get("road-buffer-statistics", 3)
    options = {"distance_method": "local_projected", "distance_m": 500, "distance_crs_ref": crs}
    context = build_task_context(
        database, task_id="task", goal=GOAL, method=method, criteria=ContextCriteria(),
        selected={"analysis_extent": area, "roads": roads}, options=options,
    )

    assessment = context.roles["roads"]
    assert (assessment.asset, assessment.code, assessment.field) == (roads, code, "critical_options.distance_crs_ref")
    issues = build_draft(context, method, plan_id="bad-measurement-crs", options=options).issues
    assert any(issue.code == code and issue.field == assessment.field for issue in issues)
    assert all(issue.field != "inputs.roads" for issue in issues)


def test_independent_reference_polygons_are_not_required_to_overlap(project):
    database, catalog, add = project
    relation = add("relation", [box(500000, 5100000, 500100, 5100100)])
    extent = add("extent", [box(500500, 5100000, 500600, 5100100)])
    points = add("points", [Point(500010, 5100010), Point(500510, 5100010)])
    lines = add("lines", [LineString([(500010, 5100010), (500510, 5100010)])])
    method = catalog.get("spatial-select-and-clip", 1)

    context = build_task_context(
        database, task_id="task", goal=GOAL, method=method,
        selected={"relation_area": relation, "analysis_extent": extent},
        criteria=ContextCriteria(object_types={"targets": "vector_point"}),
    )

    assert context.roles["relation_area"].status == context.roles["analysis_extent"].status == "READY"
    assert {item.asset for item in context.candidates["targets"]} == {points}
    assert lines not in {item.asset for item in context.candidates["targets"]}


@pytest.mark.parametrize("geometries", [[Point(501000, 5100000)], []], ids=["disjoint", "empty-layer"])
def test_explicit_target_can_still_produce_a_valid_empty_result(project, geometries):
    database, catalog, add = project
    area = add("area", [box(500000, 5100000, 500100, 5100100)])
    targets = add("targets", geometries)
    method = catalog.get("spatial-select-and-clip", 1)
    options = {"spatial_relation": "intersects"}
    context = build_task_context(
        database, task_id="task", goal=GOAL, method=method, criteria=ContextCriteria(), options=options,
        selected={"targets": targets, "relation_area": area, "analysis_extent": area},
    )

    assessment = context.roles["targets"]
    assert (assessment.asset, assessment.status, assessment.code) == (targets, "READY", "INPUT_SCOPE_DISJOINT")
    assert "允许生成空结果" in assessment.message
    assert targets not in {item.asset for item in context.candidates["targets"]}
    assert not build_draft(context, method, plan_id="empty-results", options=options).issues


def test_saved_explicit_scope_uses_the_exact_asset_version(project):
    database, catalog, add = project
    scope, samples = _points(project)
    add("scope", [box(501000, 5100000, 501100, 5100100)])
    method = catalog.get("attribute-filter-export", 1)
    context = build_task_context(
        database, task_id="task", goal=GOAL, method=method, selected={"features": samples["matching"]},
        criteria=ContextCriteria(scope=scope), options={"field_name": "class_code"},
    )

    assessment = context.roles["features"]
    assert (assessment.status, assessment.code) == ("UNKNOWN", "INPUT_SCOPE_UNVERIFIED")
    assert "Source content differs" in assessment.message
    assert samples["matching"] in {item.asset for item in context.candidates["features"]}


def test_selected_reference_role_reports_its_own_missing_crs(project):
    database, catalog, add = project
    relation = add("relation", [box(500000, 5100000, 500100, 5100100)], crs=None)
    method = catalog.get("spatial-select-and-clip", 1)
    context = build_task_context(
        database, task_id="task", goal=GOAL, method=method, criteria=ContextCriteria(),
        selected={"relation_area": relation},
    )

    assessment = context.roles["relation_area"]
    assert (assessment.asset, assessment.status, assessment.code) == (relation, "UNKNOWN", "CRS_UNKNOWN")
    assert "缺少 CRS" in assessment.message


def test_context_preview_and_saved_context_use_normal_http_with_legacy_shape(project):
    database, catalog, _ = project
    scope, samples = _points(project)
    payload = {"goal": GOAL.model_dump(mode="json"), "method": {"id": "attribute-filter-export", "version": 1},
               "inputs": {"features": samples["hole"].model_dump(mode="json")},
               "critical_options": {"field_name": "class_code", "match_value": "A"},
               "criteria": {"scope": scope.model_dump(mode="json"), "expected_artifact": "filtered_features"}}
    with TestClient(create_app(database, catalog=catalog, registry=ToolRegistry())) as client:
        preview = client.post("/api/tasks/context-preview", json=payload)
        assert preview.status_code == 200, preview.text
        assert preview.json()["roles"]["features"]["code"] == "INPUT_SCOPE_DISJOINT"
        with open_database(database, read_only=True) as connection:
            assert connection.execute("SELECT count(*) FROM task").fetchone()[0] == 0

        task = client.post("/api/tasks", json={"goal": payload["goal"]}).json()["task_id"]
        draft = client.post(f"/api/tasks/{task}/plans", json=payload)
        assert draft.status_code == 200, draft.text
        assert any(issue["code"] == "INPUT_SCOPE_DISJOINT" for issue in draft.json()["issues"])
        saved = client.get(f"/api/tasks/{task}").json()["context"]
        assert saved["criteria"]["scope"] == payload["criteria"]["scope"]
        assert saved["candidates"] == preview.json()["candidates"]

        legacy = client.post(f"/api/tasks/{task}/context", json={"method": payload["method"], "inputs": payload["inputs"]})
        assert legacy.status_code == 200, legacy.text
        assert set(legacy.json()) == {"task_id", "goal", "method", "roles"}


def test_revising_only_candidate_scope_updates_the_task_context(project):
    database, catalog, add = project
    original, samples = _points(project)
    narrowed = add("narrowed", [box(500005, 5100005, 500025, 5100025)])
    payload = {
        "goal": GOAL.model_dump(mode="json"), "method": {"id": "attribute-filter-export", "version": 1},
        "inputs": {"features": samples["matching"].model_dump(mode="json")},
        "critical_options": {"field_name": "class_code", "match_value": "A"},
        "criteria": {"scope": original.model_dump(mode="json")},
    }
    with TestClient(create_app(database, catalog=catalog, registry=ToolRegistry())) as client:
        task = client.post("/api/tasks", json={"goal": payload["goal"]}).json()["task_id"]
        endpoint = f"/api/tasks/{task}/plans"
        first = client.post(endpoint, json=payload)
        assert first.status_code == 200 and not first.json()["issues"], first.text

        revised = client.post(endpoint, json={
            **payload, "base_plan_id": first.json()["id"], "base_plan_version": first.json()["version"],
            "criteria": {"scope": narrowed.model_dump(mode="json")},
        })
        assert revised.status_code == 200 and revised.json() == first.json(), revised.text
        saved = client.get(f"/api/tasks/{task}").json()
        assert len(saved["plans"]) == 1
        assert saved["context"]["criteria"]["scope"] == narrowed.model_dump(mode="json")


def test_reusing_a_confirmed_plan_updates_only_the_task_criteria(project):
    database, catalog, add = project
    area = add("area", [box(500000, 5100000, 500100, 5100100)])
    targets = add("targets", [Point(500010, 5100010)])
    method = catalog.get("spatial-select-and-clip", 1)
    contracts = [ToolContract.model_validate(yaml.safe_load(
        (ROOT / "config/tools" / f"{step.tool.id}.yaml").read_text(encoding="utf-8")
    )) for step in method.steps]
    registry = ToolRegistry(python_implementations={contract.implementation for contract in contracts})
    for contract in contracts:
        registry.register(contract)
    payload = {
        "goal": GOAL.model_dump(mode="json"), "method": method.ref.model_dump(mode="json"),
        "inputs": {role: ref.model_dump(mode="json") for role, ref in {
            "targets": targets, "relation_area": area, "analysis_extent": area,
        }.items()},
        "critical_options": {"spatial_relation": "intersects"},
        "criteria": {"expected_artifact": "clipped_targets"},
    }
    with TestClient(create_app(database, catalog=catalog, registry=registry)) as client:
        task = client.post("/api/tasks", json={"goal": payload["goal"]}).json()["task_id"]
        endpoint = f"/api/tasks/{task}/plans"
        draft = client.post(endpoint, json=payload)
        assert draft.status_code == 200 and not draft.json()["issues"], draft.text
        plan_id, version = draft.json()["id"], draft.json()["version"]
        confirmation = client.post(f"/api/plans/{plan_id}/confirm", json={"plan_version": version})
        assert confirmation.status_code == 200, confirmation.text
        with open_database(database, read_only=True) as connection:
            original = connection.execute("SELECT plan_json FROM analysis_plan").fetchone()[0]

        changed = {"expected_artifact": "original_targets"}
        revised = client.post(endpoint, json={
            **payload, "base_plan_id": plan_id, "base_plan_version": version, "criteria": changed,
        })
        assert revised.status_code == 200 and revised.json() == confirmation.json(), revised.text
        assert client.get(f"/api/tasks/{task}").json()["context"]["criteria"]["expected_artifact"] == "original_targets"
        with open_database(database, read_only=True) as connection:
            assert [row[0] for row in connection.execute("SELECT plan_json FROM analysis_plan")] == [original]
