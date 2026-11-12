import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
import yaml
from shapely import from_wkt
from shapely.geometry import LineString, box

from geopilot.application.plan_runner import run_confirmed_plan
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.step_io import StepFileRef, StepRequest, StepResult
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.gis.vector.spatial_relations import select_within
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.compiler import PlanCompilationError, compile_plan_steps
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, save_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/fixtures/vector/t13"
METHOD = "spatial-select-and-clip"
SELECT = "apply_spatial_relation"
CLIP = "clip_back_to_analysis_extent"
WORKERS = {
    "geopilot.select_spatial_relation": "select_spatial_relation.py",
    "geopilot.clip_vector_to_extent": "clip_vector_to_extent.py",
}
TOOL_IDS = ("python-select-spatial-relation", "python-clip-vector-to-extent")
ID = "source_feature_id"

CHANGED_CLIPS = {
    "line-crosses-hole": "MULTILINESTRING ((500030 4400050, 500040 4400050), (500060 4400050, 500070 4400050))",
    "line-crosses-shell": "LINESTRING (500000 4400030, 500100 4400030)",
    "line-touch-corner": "POINT (500000 4400000)",
    "polygon-crosses-hole": "POLYGON ((500035 4400045, 500040 4400045, 500040 4400055, 500035 4400055, 500035 4400045))",
    "polygon-crosses-shell": "POLYGON ((500000 4400030, 500015 4400030, 500015 4400050, 500000 4400050, 500000 4400030))",
    "polygon-encloses-hole": (
        "POLYGON ((500030 4400030, 500070 4400030, 500070 4400070, 500030 4400070, 500030 4400030), "
        "(500040 4400040, 500040 4400060, 500060 4400060, 500060 4400040, 500040 4400040))"
    ),
    "polygon-hole-edge-outside": "LINESTRING (500040 4400045, 500040 4400055)",
    "polygon-shell-edge-outside": "LINESTRING (500000 4400070, 500000 4400080)",
    "polygon-shell-vertex-outside": "POINT (500100 4400100)",
}
PRECLIP_ADDED_IDS = {
    "points": set(),
    "lines": {"line-crosses-hole", "line-crosses-shell"},
    "polygons": {"polygon-crosses-hole", "polygon-crosses-shell", "polygon-encloses-hole"},
}


@pytest.fixture(scope="module")
def golden():
    expected = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))
    package = GOLDEN / expected["input_file"]
    return package, expected


@pytest.fixture(scope="module")
def runtime():
    catalog = load_method_catalog(ROOT / "config/methods")
    method = catalog.get(METHOD, 1)
    registry = ToolRegistry(python_implementations=WORKERS)
    for tool_id in TOOL_IDS:
        source = ROOT / f"config/tools/{tool_id}.yaml"
        contract = ToolContract.model_validate(yaml.safe_load(source.read_text(encoding="utf-8")))
        registry.register(contract, required_modules=("geopandas", "pyogrio", "pyproj", "shapely"))
    return catalog, method, registry


def _execute(tmp_path, runtime, package, layer, predicate, *, analysis_extent=None):
    catalog, method, registry = runtime
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    selected_assets = {
        "targets": register_asset(database, package, layer=layer).ref,
        "relation_area": register_asset(database, package, layer="area").ref,
        "analysis_extent": register_asset(database, analysis_extent or package, layer="area").ref,
    }
    context = build_task_context(
        database, task_id="task-t13", goal=TaskGoal(summary="先判断完整目标的关系再裁回"),
        method=method, selected=selected_assets, registry=registry,
    )
    draft = build_draft(context, method, plan_id="plan-t13", options={"spatial_relation": predicate})
    assert not draft.issues, draft.issues
    save_draft(database, draft, context=context)
    plan = confirm_plan(
        database, draft.id, draft.version, catalog=catalog, registry=registry, registered_rules=RUN_CHECKS,
    )
    assert method.locked_order and [step.key for step in plan.steps] == [SELECT, CLIP]
    run = run_confirmed_plan(
        database, plan.id, plan.version, workspace_root=tmp_path / "workspace", run_id=f"{layer}-{predicate}",
        registry=registry,
        python_implementations={name: ROOT / "src/geopilot/gis/tools" / file for name, file in WORKERS.items()},
    )
    assert run.execution_status == "SUCCEEDED" and run.check_status == "PASSED", run
    assert list(run.steps) == [SELECT, CLIP]
    return database, plan, run


def _read(run, reference):
    return pyogrio.read_dataframe(run.workdir / reference.path, layer=reference.layer)


def _assert_complete_rows(actual, original, ids):
    retained = original.loc[original[ID].isin(ids)].reset_index(drop=True)
    assert sorted(actual[ID]) == sorted(ids)
    assert actual.crs == original.crs
    assert actual.geometry.to_wkb().tolist() == retained.geometry.to_wkb().tolist()
    pd.testing.assert_frame_equal(
        actual.drop(columns=actual.geometry.name), retained.drop(columns=retained.geometry.name), check_dtype=bool(ids),
    )


@pytest.mark.parametrize("layer", ["points", "lines", "polygons"])
@pytest.mark.parametrize("predicate", ["intersects", "within"])
def test_t13_runs_the_selected_relation_on_complete_objects_before_clipping(tmp_path, runtime, golden, layer, predicate):
    package, expected = golden
    input_bytes = package.read_bytes()
    source = pyogrio.read_dataframe(package, layer=layer)
    area = pyogrio.read_dataframe(package, layer="area")
    expected_ids = expected["relations"][predicate][layer]

    database, plan, run = _execute(tmp_path, runtime, package, layer, predicate)

    if layer == "lines" and predicate == "within":
        _, method, registry = runtime
        reversed_plan = plan.model_copy(update={"steps": tuple(reversed(plan.steps))})
        with pytest.raises(PlanCompilationError) as rejected:
            compile_plan_steps(reversed_plan, method, registry=registry)
        assert (rejected.value.code, rejected.value.field) == ("LOCKED_STEP_ORDER", "steps.0.key")

    selection = run.steps[SELECT].outputs["targets"]
    clips = run.steps[CLIP].outputs["targets"]
    originals = run.steps[CLIP].outputs["original_targets"]
    assert run.artifacts == {"clipped_targets": clips, "original_targets": originals}
    _assert_complete_rows(_read(run, selection), source, expected_ids)
    _assert_complete_rows(_read(run, originals), source, expected_ids)
    clipped = _read(run, clips)
    assert clipped.crs == source.crs and sorted(clipped[ID]) == expected_ids
    assert pyogrio.read_info(run.workdir / clips.path, layer=clips.layer)["geometry_type"] == "Unknown"
    source_by_id = source.set_index(ID).geometry
    for identifier, geometry in zip(clipped[ID], clipped.geometry, strict=True):
        expected_geometry = from_wkt(CHANGED_CLIPS[identifier]) if identifier in CHANGED_CLIPS else source_by_id.loc[identifier]
        assert geometry.geom_type == expected_geometry.geom_type, identifier
        assert geometry.equals(expected_geometry), identifier
        assert area.geometry.item().covers(geometry), identifier

    if predicate == "intersects":
        prematurely_clipped = set(select_within(clipped, area)[ID])
        assert prematurely_clipped == set(expected["relations"]["within"][layer]) | PRECLIP_ADDED_IDS[layer]

    snapshot = json.loads((run.workdir / "request.json").read_text(encoding="utf-8"))
    assert snapshot["plan"] == plan.model_dump(mode="json")
    requests = {}
    for step, tool_id, worker in zip((SELECT, CLIP), TOOL_IDS, WORKERS.values(), strict=True):
        directory = run.workdir / "steps" / step
        request = StepRequest.model_validate_json((directory / "request.json").read_text(encoding="utf-8"))
        requests[step] = request
        assert request.tool.model_dump() == {"id": tool_id, "version": 1}
        result = StepResult.model_validate_json((directory / "result.json").read_text(encoding="utf-8"))
        assert result == run.steps[step] and result.exit_code == 0
        log = (directory / "process.log").read_text(encoding="utf-8")
        assert any(line.startswith("$ ") and worker in line for line in log.splitlines())
    assert requests[SELECT].parameters == {"spatial_relation": predicate}
    assert requests[SELECT].inputs["targets"].model_dump() == snapshot["asset_files"]["targets"]
    assert requests[SELECT].inputs["area"].model_dump() == snapshot["asset_files"]["relation_area"]
    assert requests[CLIP].inputs["targets"] == StepFileRef(path=selection.path, layer=selection.layer)
    assert requests[CLIP].inputs["area"].model_dump() == snapshot["asset_files"]["analysis_extent"]
    with open_database(database) as connection:
        state = connection.execute("SELECT plan_version, execution_status, check_status FROM run").fetchone()
    assert tuple(state) == (plan.version, "SUCCEEDED", "PASSED")
    assert package.read_bytes() == input_bytes


@pytest.mark.parametrize("retain", [True, False], ids=["one-survivor", "no-survivors"])
def test_distinct_analysis_extent_retains_originals_only_for_nonempty_clips(tmp_path, runtime, golden, retain):
    package, expected = golden
    extent = tmp_path / "analysis-extent.gpkg"
    bounds = (500005, 4400019, 500025, 4400021) if retain else (501000, 4401000, 501010, 4401010)
    pyogrio.write_dataframe(gpd.GeoDataFrame(geometry=[box(*bounds)], crs=expected["crs"]), extent, layer="area")
    before = extent.read_bytes()
    source = pyogrio.read_dataframe(package, layer="lines")

    _, _, run = _execute(tmp_path, runtime, package, "lines", "intersects", analysis_extent=extent)

    _assert_complete_rows(_read(run, run.steps[SELECT].outputs["targets"]), source, expected["relations"]["intersects"]["lines"])
    surviving = ["line-inside"] if retain else []
    _assert_complete_rows(_read(run, run.artifacts["original_targets"]), source, surviving)
    clipped = _read(run, run.artifacts["clipped_targets"])
    assert clipped[ID].tolist() == surviving and clipped.crs == source.crs
    input_info = pyogrio.read_info(package, layer="lines")
    for reference in run.artifacts.values():
        info = pyogrio.read_info(run.workdir / reference.path, layer=reference.layer)
        assert info["fields"].tolist() == input_info["fields"].tolist()
        assert info["dtypes"].tolist() == input_info["dtypes"].tolist()
        assert info["features"] == len(surviving)
    if retain:
        assert clipped.geometry.item().equals(LineString([(500010, 4400020), (500025, 4400020)]))
    snapshot = json.loads((run.workdir / "request.json").read_text(encoding="utf-8"))
    assert snapshot["asset_files"]["relation_area"]["path"] != snapshot["asset_files"]["analysis_extent"]["path"]
    assert extent.read_bytes() == before
