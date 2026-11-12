import csv
import json
from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest
import yaml
from shapely.geometry import GeometryCollection, LineString, box

from geopilot.application.plan_runner import execute_claimed_run, run_confirmed_plan
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.step_io import StepFileRef, StepRequest
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.gis.vector.polygon_statistics import aggregate_polygon_statistics
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.run_repository import get_run, list_run_steps
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, save_draft
from geopilot.runs.service import create_run
from geopilot.scheduler.loop import run_once
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
CRS = "EPSG:6933"
ID = "source_feature_id"
SELECT = "apply_spatial_relation"
CLIP = "clip_back_to_analysis_extent"
STATS = "aggregate_by_zone"
WORKERS = {
    "geopilot.select_spatial_relation": "select_spatial_relation.py",
    "geopilot.clip_vector_to_extent": "clip_vector_to_extent.py",
    "geopilot.polygon_zone_statistics": "polygon_zone_statistics.py",
}
WORKER_PATHS = {name: ROOT / "src/geopilot/gis/tools" / worker for name, worker in WORKERS.items()}
METHODS = ("split_by_area", "centroid", "largest_overlap")
COMMON_DETAILS = {("boundary-contact", None): ("unassigned", 0), ("west-only", "west"): ("assigned", 200)}
EXPECTED_DETAILS = {
    "split_by_area": {
        **COMMON_DETAILS,
        ("centroid-tie", "east"): ("assigned", 200), ("centroid-tie", "west"): ("assigned", 200),
        ("crossing", "east"): ("assigned", 400), ("crossing", "west"): ("assigned", 800),
        ("outside-zones", None): ("unassigned", 100),
        ("partly-uncovered", "east"): ("assigned", 100), ("partly-uncovered", None): ("assigned", 200),
    },
    "centroid": {
        **COMMON_DETAILS,
        ("centroid-tie", None): ("unassigned", 400), ("crossing", "east"): ("assigned", 1200),
        ("outside-zones", None): ("unassigned", 100), ("partly-uncovered", None): ("unassigned", 300),
    },
    "largest_overlap": {
        **COMMON_DETAILS,
        ("centroid-tie", None): ("unassigned", 400), ("crossing", "east"): ("assigned", 1200),
        ("outside-zones", None): ("unassigned", 100), ("partly-uncovered", "east"): ("assigned", 300),
    },
}
EXPECTED_TOTALS = {
    "split_by_area": (4, 2, 1900, 300),
    "centroid": (2, 4, 1400, 800),
    "largest_overlap": (3, 3, 1700, 500),
}


def _box(left, bottom, right, top):
    return box(1_000_000 + left, 2_000_000 + bottom, 1_000_000 + right, 2_000_000 + top)


def _persist(package, layer, rows):
    frame = gpd.GeoDataFrame({ID: [identifier for identifier, _ in rows]}, geometry=[shape for _, shape in rows], crs=CRS)
    pyogrio.write_dataframe(frame, package, driver="GPKG", layer=layer, promote_to_multi=False)
    return pyogrio.read_dataframe(package, layer=layer)


def _build_t14(package, *, conflicting=False, disjoint_extent=False):
    crossing = _box(60, 10, 160, 40)
    _persist(package, "targets", [
        ("crossing", crossing), ("crossing", _box(61, 10, 160, 40) if conflicting else crossing),
        ("centroid-tie", _box(80, 10, 120, 20)), ("west-only", _box(10, 32, 30, 42)),
        ("partly-uncovered", _box(190, 70, 220, 80)), ("outside-zones", _box(220, 80, 230, 90)),
        ("boundary-contact", _box(120, 5, 130, 15)), ("drop-outside", _box(150, 40, 170, 45)),
    ])
    _persist(package, "relation_area", [("relation", _box(0, 0, 240, 100))])
    extents = [("far", _box(300, 300, 320, 320))] if disjoint_extent else [
        ("first-band", _box(0, 0, 120, 20)), ("second-band", _box(0, 30, 120, 50)),
        ("outer-band", _box(190, 60, 240, 100)),
    ]
    _persist(package, "analysis_extent", extents)
    _persist(package, "zones", [("west", _box(0, 0, 100, 100)), ("east", _box(100, 0, 200, 100))])


@pytest.fixture(scope="module")
def runtime():
    catalog = load_method_catalog(ROOT / "config/methods")
    method = catalog.get("polygon-zone-statistics", 1)
    registry = ToolRegistry(python_implementations=WORKERS)
    for tool_id in ("python-select-spatial-relation", "python-clip-vector-to-extent", "python-polygon-zone-statistics"):
        path = ROOT / f"config/tools/{tool_id}.yaml"
        registry.register(ToolContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))))
    return catalog, method, registry


def _confirm(tmp_path, runtime, package, assignment, *, area_crs=CRS):
    catalog, method, registry = runtime
    database = tmp_path / "t14.sqlite"
    initialize_database(database)
    selected = {role: register_asset(database, package, layer=role).ref for role in method.inputs}
    context = build_task_context(
        database, task_id="task-t14", goal=TaskGoal(summary="按原对象 ID 汇总跨区面"),
        method=method, selected=selected, registry=registry,
    )
    draft = build_draft(context, method, plan_id="plan-t14", options={
        "spatial_relation": "intersects", "boundary_assignment": assignment,
        "boundary_tie": "unassigned", "area_crs": area_crs,
    })
    assert not draft.issues, draft.issues
    save_draft(database, draft, context=context)
    plan = confirm_plan(database, draft.id, draft.version, catalog=catalog, registry=registry, registered_rules=RUN_CHECKS)
    assert method.locked_order and [step.key for step in plan.steps] == [SELECT, CLIP, STATS]
    return database, plan


def _execute(tmp_path, runtime, package, assignment, *, area_crs=CRS):
    database, plan = _confirm(tmp_path, runtime, package, assignment, area_crs=area_crs)
    return run_confirmed_plan(
        database, plan.id, plan.version, workspace_root=tmp_path / "workspace", run_id=assignment.replace("_", "-"),
        registry=runtime[2], python_implementations=WORKER_PATHS,
    )


def _output(run, role):
    return run.workdir / run.artifacts[role].path


@pytest.mark.parametrize("assignment", METHODS)
def test_t14_three_real_subprocesses_match_manual_details_and_unique_totals(tmp_path, runtime, assignment):
    package = tmp_path / "t14.gpkg"
    _build_t14(package)
    before = package.read_bytes()

    run = _execute(tmp_path, runtime, package, assignment)

    assert run.execution_status == "SUCCEEDED" and run.check_status == "PASSED", run
    assert list(run.steps) == [SELECT, CLIP, STATS]
    for step, worker in zip(run.steps.values(), WORKERS.values(), strict=True):
        assert step.exit_code == 0
        log = (run.workdir / step.log_path).read_text(encoding="utf-8")
        assert any(line.startswith("$ ") and worker in line for line in log.splitlines())
    selection = run.steps[SELECT].outputs["targets"]
    assert len(pyogrio.read_dataframe(run.workdir / selection.path, layer=selection.layer)) == 8
    clipped = pyogrio.read_dataframe(_output(run, "clipped_targets"), layer=run.artifacts["clipped_targets"].layer)
    originals = pyogrio.read_dataframe(_output(run, "original_targets"), layer=run.artifacts["original_targets"].layer)
    assert len(clipped) == len(originals) == 7
    assert "drop-outside" not in set(clipped[ID])
    crossing = clipped.loc[clipped[ID].eq("crossing"), "geometry"]
    assert len(crossing) == 2 and all(len(geometry.geoms) == 2 and geometry.area == 1200 for geometry in crossing)
    crossing_parts = {_box(60, 10, 120, 20).bounds, _box(60, 30, 120, 40).bounds}
    assert all({part.bounds for part in geometry.geoms} == crossing_parts for geometry in crossing)
    original_crossing = originals.loc[originals[ID].eq("crossing"), "geometry"]
    assert len(original_crossing) == 2 and all(geometry.equals(_box(60, 10, 160, 40)) for geometry in original_crossing)
    assert clipped.loc[clipped[ID].eq("boundary-contact"), "geometry"].item().geom_type == "LineString"

    with _output(run, "allocation_details").open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == [ID, "zone_id", "object_status", "area_m2"]
        rows = list(reader)
    details = {(row[ID], row["zone_id"] or None): (row["object_status"], float(row["area_m2"])) for row in rows}
    assert len(details) == len(rows) and details == EXPECTED_DETAILS[assignment]
    summary = json.loads(_output(run, "zone_statistics").read_text(encoding="utf-8"))
    assigned, unassigned, assigned_area, unassigned_area = EXPECTED_TOTALS[assignment]
    assert summary["counts"] == {"total": 6, "assigned": assigned, "unassigned": unassigned}
    assert summary["areas_m2"] == {"total": 2200, "assigned": assigned_area, "unassigned": unassigned_area, "uncovered": 300}
    assert summary["boundary_assignment"] == assignment and summary["boundary_tie"] == "unassigned"
    assert summary["area_crs"] == CRS and summary["area_unit"] == "square_meter"
    assert summary["unassigned_source_feature_ids"] == sorted({row[ID] for row in rows if row["object_status"] == "unassigned"})
    assert [zone["zone_id"] for zone in summary["zones"]] == ["east", "west"]
    for zone in summary["zones"]:
        contributions = {identifier: area for (identifier, zone_id), (_, area) in details.items() if zone_id == zone["zone_id"]}
        assert zone["distinct_count"] == len(contributions)
        assert zone["area_m2"] == sum(contributions.values())
        assert zone["source_feature_ids"] == sorted(contributions)

    request = StepRequest.model_validate_json((run.workdir / "steps" / STATS / "request.json").read_text(encoding="utf-8"))
    assert request.parameters == {"boundary_assignment": assignment, "boundary_tie": "unassigned", "area_crs": CRS,
                                  "area_unit": "square_meter"}
    for role in ("targets", "original_targets"):
        output = run.steps[CLIP].outputs[role]
        assert request.inputs[role] == StepFileRef(path=output.path, layer=output.layer)
    stats_log = (run.workdir / request.log_path).read_text(encoding="utf-8")
    measurements = [json.loads(line.removeprefix("measurement: ")) for line in stats_log.splitlines() if line.startswith("measurement: ")]
    assert {record["role"] for record in measurements} == {"targets", "original_targets", "zones"}
    assert all((record["method"], record["crs"], record["unit"]) == ("equal_area", CRS, "square_meter")
               for record in measurements)
    assert package.read_bytes() == before


def test_clipped_fragments_with_one_id_union_areal_parts_without_double_counting(tmp_path):
    package = tmp_path / "fragments.gpkg"
    clips = _persist(package, "clips", [
        ("parcel", _box(0, 0, 15, 10)), ("parcel", _box(10, 0, 20, 10)),
        ("parcel", GeometryCollection([_box(2, 2, 3, 3), LineString([(1000000, 2000000), (1000001, 2000000)])])),
    ])
    originals = _persist(package, "originals", [("parcel", _box(0, 0, 20, 10)), ("not-clipped", _box(30, 30, 40, 40))])
    zones = _persist(package, "zones", [("west", _box(0, 0, 10, 10)), ("east", _box(10, 0, 20, 10))])

    stats = aggregate_polygon_statistics(clips, originals, zones, boundary_assignment="split_by_area", area_crs=CRS)

    assert stats.summary["counts"] == {"total": 1, "assigned": 1, "unassigned": 0}
    assert stats.summary["areas_m2"] == {"total": 200, "assigned": 200, "unassigned": 0, "uncovered": 0}
    assert [(row.source_feature_id, row.zone_id, row.area_m2) for row in stats.details] == [
        ("parcel", "east", 100), ("parcel", "west", 100),
    ]
    with pytest.raises(ValueError, match=r"original_targets.*parcel"):
        aggregate_polygon_statistics(clips, originals.iloc[1:], zones, boundary_assignment="centroid", area_crs=CRS)


def test_conflicting_originals_fail_in_the_statistics_child_and_publish_no_tables(tmp_path, runtime):
    package = tmp_path / "conflicting.gpkg"
    _build_t14(package, conflicting=True)

    run = _execute(tmp_path, runtime, package, "centroid")

    assert run.execution_status == "FAILED" and run.check_status == "NOT_CHECKED"
    assert all(run.steps[key].status == "SUCCEEDED" for key in (SELECT, CLIP))
    statistics = run.steps[STATS]
    assert statistics.error.code == "PROCESS_FAILED" and statistics.exit_code == 1
    assert not statistics.outputs and not run.artifacts
    log = (run.workdir / statistics.log_path).read_text(encoding="utf-8")
    assert "original_targets" in log and "conflicting" in log and "crossing" in log
    assert not list((run.workdir / "intermediate" / STATS).glob("*.csv"))
    assert not list((run.workdir / "intermediate" / STATS).glob("*.json"))


def test_unmatched_explicit_area_crs_rejects_the_statistics_before_dispatch(tmp_path, runtime):
    package = tmp_path / "t14.gpkg"
    _build_t14(package)

    run = _execute(tmp_path, runtime, package, "largest_overlap", area_crs="EPSG:8857")

    assert run.execution_status == "FAILED" and run.check_status == "NOT_CHECKED"
    assert all(run.steps[key].status == "SUCCEEDED" for key in (SELECT, CLIP))
    statistics = run.steps[STATS]
    assert statistics.exit_code is None and statistics.error.code == "INVALID_PARAMETER"
    assert statistics.error.field == "parameters.area_crs" and not statistics.outputs
    log = (run.workdir / statistics.log_path).read_text(encoding="utf-8")
    assert not any(line.startswith("$ ") for line in log.splitlines())


def test_empty_surviving_input_has_zero_objects_and_areas(tmp_path):
    package = tmp_path / "t14.gpkg"
    _build_t14(package)
    originals = pyogrio.read_dataframe(package, layer="targets")
    zones = pyogrio.read_dataframe(package, layer="zones")

    stats = aggregate_polygon_statistics(
        originals.iloc[:0], originals, zones, boundary_assignment="largest_overlap", area_crs=CRS,
    )

    assert not stats.details
    assert stats.summary["counts"] == {"total": 0, "assigned": 0, "unassigned": 0}
    assert stats.summary["areas_m2"] == {"total": 0, "assigned": 0, "unassigned": 0, "uncovered": 0}


def test_empty_clip_writes_zero_totals_through_the_claimed_run(tmp_path, runtime):
    package = tmp_path / "empty-survivors.gpkg"
    _build_t14(package, disjoint_extent=True)
    database, plan = _confirm(tmp_path, runtime, package, "centroid")
    workspace = tmp_path / "workspace"
    run_id = create_run(database, plan.id, plan.version, workspace_root=workspace)

    assert run_once(database, lambda run: execute_claimed_run(
        database, run, workspace_root=workspace, registry=runtime[2], python_implementations=WORKER_PATHS,
    )) == run_id

    with open_database(database) as connection:
        recorded = get_run(connection, run_id)
        steps = list_run_steps(connection, run_id)
    assert (recorded.execution_status, recorded.check_status) == ("SUCCEEDED", "PASSED"), recorded.error
    assert [step.step_key for step in steps] == [SELECT, CLIP, STATS]
    assert [step.result.exit_code for step in steps] == [0, 0, 0]
    workdir = Path(recorded.workdir)
    log = (workdir / steps[-1].log_path).read_text(encoding="utf-8")
    checks = [json.loads(line.removeprefix("input_check: ")) for line in log.splitlines() if line.startswith("input_check: ")]
    assert checks and all(check["passed"] for check in checks)
    assert any(line.startswith("$ ") and "polygon_zone_statistics.py" in line for line in log.splitlines())
    for reference in steps[1].result.outputs.values():
        assert pyogrio.read_dataframe(workdir / reference.path, layer=reference.layer).empty
    outputs = steps[-1].result.outputs
    with (workdir / outputs["allocation_details"].path).open(encoding="utf-8", newline="") as stream:
        rows = csv.DictReader(stream)
        assert rows.fieldnames == [ID, "zone_id", "object_status", "area_m2"]
        assert list(rows) == []
    summary = json.loads((workdir / outputs["zone_statistics"].path).read_text(encoding="utf-8"))
    assert summary["counts"] == {"total": 0, "assigned": 0, "unassigned": 0}
    assert summary["areas_m2"] == {"total": 0, "assigned": 0, "unassigned": 0, "uncovered": 0}
