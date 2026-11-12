import json
from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest
import yaml
from shapely.geometry import box

from geopilot.application.land_use_checks import LAND_USE_CHECKS
from geopilot.application.plan_runner import run_confirmed_plan
from geopilot.application.task_context import build_task_context
from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.assets import register_asset
from geopilot.domain.step_io import StepRequest
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.gis.vector.transition_matrix import (
    TransitionEvidenceError,
    allocate_transition_areas,
    verify_transition_outputs,
)
from geopilot.methods.land_use_overlay import preflight_land_use_step
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, save_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
CRS = "EPSG:6933"
TOLERANCE = 0.01
X = 1_000_000
KEYS = (
    "read_two_periods", "map_classification_systems", "choose_equal_area_projection", "overlay",
    "apply_boundary_assignment", "build_change_polygons", "build_transition_matrix",
)
RULES = ("classification_mapping_complete", "overlay_area_balanced", "transition_totals_match")
CLASSES = ["cropland", "wetland", "woodland"]
MATRIX_M2 = [[1000.0, 3000.0, 0.0], [0.0, 0.0, 0.0], [0.0, 4500.0, 0.0]]
ZONE_TOTALS = {
    "split_by_area": {"east": 5000.0, "outer": 0.0, "west": 3000.0, None: 500.0},
    "centroid": {"east": 4500.0, "outer": 2000.0, "west": 0.0, None: 2000.0},
    "largest_overlap": {"east": 4500.0, "outer": 2000.0, "west": 2000.0, None: 0.0},
}


def _box(left, bottom, right, top):
    return box(X + left, bottom, X + right, top)


def _write_source(directory, role, records, shapes):
    path = directory / f"{role}.gpkg"
    frame = gpd.GeoDataFrame(records, geometry=shapes, crs=CRS)
    pyogrio.write_dataframe(frame, path, driver="GPKG", layer=role)
    return path


@pytest.fixture(scope="module")
def runtime():
    catalog = load_method_catalog(ROOT / "config/methods")
    assert not catalog.issues, catalog.issues
    method = catalog.get("land-use-change", 1)
    implementations = {
        "geopilot.land_use_overlay": ROOT / "src/geopilot/methods/land_use_overlay.py",
        "geopilot.land_use_transition": ROOT / "src/geopilot/gis/tools/land_use_transition.py",
    }
    registry = ToolRegistry(python_implementations=implementations)
    for source in sorted((ROOT / "config/tools").glob("python-land-use-*.yaml")):
        contract = ToolContract.model_validate(yaml.safe_load(source.read_text(encoding="utf-8")))
        registry.register(contract, required_modules=("geopandas", "pyogrio", "pyproj", "shapely"))
    return catalog, method, registry, implementations


def _preflight(step):
    preflight_step_inputs(step)
    preflight_land_use_step(step)


def _execute(directory, runtime, *, assignment="split_by_area", merge=True, minimum=2000, all_changed=False):
    catalog, method, registry, implementations = runtime
    sources = {
        "first_classes": _write_source(directory, "first_classes", {
            "source_feature_id": ["west1-2018", "west2-2018", "east-2018"], "code": [10, 10, 20],
        }, [_box(-60, 0, 20, 100), _box(20, 0, 40, 100), _box(40, 0, 100, 100)]),
        "second_classes": _write_source(directory, "second_classes", {
            "source_feature_id": ["south-2024", "north-2024"], "class": ["01", "02"],
        }, [_box(-60, 0, 100, 25), _box(-60, 25, 100, 100)]),
        "zones": _write_source(directory, "zones", {
            "source_feature_id": ["west", "outer", "east"],
        }, [_box(0, 0, 30, 100), _box(-60, 0, 0, 100), _box(35, 0, 100, 100)]),
        "analysis_extent": _write_source(directory, "analysis_extent", {}, [_box(0, 0, 100, 100)]),
    }
    source_bytes = {role: path.read_bytes() for role, path in sources.items()}
    database = directory / "project.sqlite"
    initialize_database(database)
    selected = {role: register_asset(database, source, layer=role).ref for role, source in sources.items()}
    context = build_task_context(
        database, task_id="transition-task", goal=TaskGoal(summary="两期土地利用转移与分区"), method=method,
        selected=selected, registry=registry,
    )
    options = {
        "first_class_field": "code", "second_class_field": "class",
        "first_class_mapping": {"10": "cropland", "20": "woodland"},
        "second_class_mapping": {"01": "built" if all_changed else "cropland", "02": "wetland"},
        "first_period": "2018", "second_period": "2024", "area_crs": CRS,
        "overlay_area_tolerance": TOLERANCE, "boundary_assignment": assignment,
        "minimum_change_area": minimum, "merge_adjacent_classes": merge,
    }
    draft = build_draft(context, method, plan_id="land-use-change-plan", options=options)
    assert not draft.issues, draft.issues
    save_draft(database, draft, context=context)
    plan = confirm_plan(
        database, draft.id, draft.version, catalog=catalog, registry=registry, registered_rules=LAND_USE_CHECKS,
    )
    assert method.locked_order and tuple(step.key for step in plan.steps) == KEYS
    assert plan.check_rules == RULES
    run = run_confirmed_plan(
        database, plan.id, plan.version, workspace_root=directory / "workspace", run_id="land-use-change-run",
        registry=registry, python_implementations=implementations, input_preflight=_preflight,
        check_rules=LAND_USE_CHECKS,
    )
    assert run.execution_status == "SUCCEEDED" and run.check_status == "PASSED", run
    assert tuple(run.steps) == KEYS
    assert set(run.artifacts) == {"change_polygons", "transition_matrix", "zone_statistics"}
    assert all(step.status == "SUCCEEDED" and step.exit_code == 0 for step in run.steps.values())
    assert {role: path.read_bytes() for role, path in sources.items()} == source_bytes
    return run


@pytest.fixture(scope="module")
def split_run(tmp_path_factory, runtime):
    return _execute(tmp_path_factory.mktemp("land-use-split"), runtime)


def _json(run, reference):
    return json.loads((run.workdir / reference.path).read_text(encoding="utf-8"))


def _vector(run, reference):
    return pyogrio.read_dataframe(run.workdir / reference.path, layer=reference.layer)


def _documents(run):
    return (
        _vector(run, run.steps["overlay"].outputs["overlay_polygons"]),
        _vector(run, run.artifacts["change_polygons"]),
        _json(run, run.steps["apply_boundary_assignment"].outputs["allocations"]),
        _json(run, run.steps["build_change_polygons"].outputs["change_report"]),
        _json(run, run.artifacts["transition_matrix"]),
        _json(run, run.artifacts["zone_statistics"]),
    )


@pytest.mark.parametrize("assignment", ["split_by_area", "centroid", "largest_overlap"])
def test_seven_real_steps_conserve_matrix_zone_area_and_full_geometry(
    tmp_path, runtime, split_run, assignment,
):
    run = split_run if assignment == "split_by_area" else _execute(tmp_path, runtime, assignment=assignment)
    overlay, changes, allocations, change_report, matrix, zone_statistics = _documents(run)
    assert overlay.geometry.area.sum() == 10_000
    assert changes.geometry.area.sum() == 8500
    assert changes.crs.to_string() == CRS and changes.geometry.is_valid.all()
    assert changes["area_m2"].tolist() == [1000.0, 3000.0, 4500.0]
    assert changes["is_change"].tolist() == [False, True, True]
    assert change_report["minimum_change_area_m2"] == 2000
    assert change_report["merge_adjacent_classes"] is True
    assert change_report["pre_filter_area_m2"] == 10_000
    assert change_report["filtered_area_m2"] == 1500
    assert change_report["retained_area_m2"] == 8500
    assert change_report["pre_filter_patch_count"] == 4 and change_report["filtered_patch_count"] == 1
    assert matrix["classes"] == zone_statistics["classes"] == CLASSES
    assert matrix["areas_m2"] == MATRIX_M2
    assert matrix["total_area_m2"] == zone_statistics["total_area_m2"] == 8500
    assert matrix["changed_area_m2"] == 7500 and matrix["unchanged_area_m2"] == 1000
    assert [row["zone_id"] for row in zone_statistics["zones"]] == ["east", "outer", "west", None]
    assert {row["zone_id"]: row["total_area_m2"] for row in zone_statistics["zones"]} == ZONE_TOTALS[assignment]
    assert allocations["boundary_assignment"] == assignment

    raw = overlay.set_index("source_feature_id")
    west1 = {identifier for identifier, row in raw.iterrows() if row["first_source_feature_id"] == "west1-2018"}
    west1_allocations = [record for record in allocations["allocations"] if record["fragment_id"] in west1]
    assert {record["zone_id"] for record in west1_allocations} == (
        {"west"} if assignment == "split_by_area" else {"outer"}
    )
    assert sum(record["area_m2"] for record in west1_allocations) == 2000
    assert all(raw.loc[identifier].geometry.bounds[0] == X for identifier in west1)
    request = StepRequest.model_validate_json(
        (run.workdir / "steps/apply_boundary_assignment/request.json").read_text(encoding="utf-8")
    )
    first = _vector(run, request.inputs["first_classes"]).set_index("source_feature_id")
    assert first.loc["west1-2018"].geometry.centroid.x == X - 20
    assert first.loc["west1-2018"].geometry.bounds[0] == X - 60

    used = set()
    for row in changes.itertuples():
        assert (row.first_period, row.second_period) == ("2018", "2024")
        references = json.loads(row.source_refs)
        for source in references:
            identifier = source["fragment_id"]
            assert identifier not in used
            used.add(identifier)
            assert source["first_source_feature_id"] == raw.loc[identifier, "first_source_feature_id"]
            assert source["second_source_feature_id"] == raw.loc[identifier, "second_source_feature_id"]
            assert source["first_source_code"] == raw.loc[identifier, "first_source_code"]
            assert source["second_source_code"] == raw.loc[identifier, "second_source_code"]
        expected = raw.loc[[source["fragment_id"] for source in references]].geometry.union_all()
        assert row.geometry.equals(expected)
    filtered = {identifier for patch in change_report["filtered"] for identifier in patch["fragment_ids"]}
    assert len(used) == 5 and len(filtered) == 1 and not used.intersection(filtered)
    assert used.union(filtered) == set(raw.index)
    observed = verify_transition_outputs(
        overlay, changes, allocations, change_report, matrix, zone_statistics, area_tolerance_m2=TOLERANCE,
    )
    assert observed["polygon_area_m2"] == observed["matrix_area_m2"] == observed["zone_area_m2"] == 8500
    report = json.loads((run.workdir / "checks/report.json").read_text(encoding="utf-8"))
    assert report["status"] == "PASSED"
    assert report["completed_rules"] == list(RULES)
    assert all(report["rules"][rule]["passed"] for rule in RULES)


def test_minimum_applies_after_optional_merge_and_preserves_diagonal(tmp_path, runtime):
    run = _execute(tmp_path, runtime, merge=False)
    _, changes, _, change_report, matrix, zones = _documents(run)
    assert changes["area_m2"].tolist() == [500.0, 500.0, 4500.0]
    assert changes["is_change"].tolist() == [False, False, True]
    assert change_report["filtered_patch_count"] == 3
    assert change_report["filtered_area_m2"] == 4500
    assert matrix["classes"] == CLASSES
    assert matrix["areas_m2"] == [[1000.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 4500.0, 0.0]]
    assert matrix["total_area_m2"] == zones["total_area_m2"] == 5500


def test_all_filtered_keeps_valid_empty_polygon_layer_and_stable_class_order(tmp_path, runtime):
    run = _execute(tmp_path, runtime, all_changed=True, minimum=10_000)
    _, changes, _, change_report, matrix, zones = _documents(run)
    assert changes.empty and changes.crs.to_string() == CRS
    assert change_report["filtered_area_m2"] == 10_000 and change_report["retained_patch_count"] == 0
    assert matrix["classes"] == zones["classes"] == ["built", "cropland", "wetland", "woodland"]
    assert matrix["areas_m2"] == [[0.0] * 4] * 4
    assert matrix["total_area_m2"] == zones["total_area_m2"] == 0


def test_actual_outputs_detect_provenance_and_matrix_cell_corruption(split_run, tmp_path):
    overlay, changes, allocations, report, matrix, zones = _documents(split_run)
    references = json.loads(changes.loc[0, "source_refs"])
    references[0]["first_source_feature_id"] = "unrelated-source"
    altered = changes.copy()
    altered.loc[0, "source_refs"] = json.dumps(references)
    path = tmp_path / "altered.gpkg"
    pyogrio.write_dataframe(altered, path, layer="changes", driver="GPKG")
    reopened = pyogrio.read_dataframe(path, layer="changes")
    with pytest.raises(TransitionEvidenceError, match="source") as source:
        verify_transition_outputs(overlay, reopened, allocations, report, matrix, zones, area_tolerance_m2=TOLERANCE)
    assert "source_refs" in source.value.field
    matrix["areas_m2"][0][0] -= 1
    matrix["areas_m2"][0][1] += 1
    matrix_path = tmp_path / "altered.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    with pytest.raises(TransitionEvidenceError) as cell:
        verify_transition_outputs(
            overlay, changes, allocations, report, json.loads(matrix_path.read_text(encoding="utf-8")),
            zones, area_tolerance_m2=TOLERANCE,
        )
    assert cell.value.field.startswith("transition_matrix.areas_m2")


def test_integer_zone_identifiers_remain_traceable(split_run, tmp_path):
    run = split_run
    request = StepRequest.model_validate_json(
        (run.workdir / "steps/apply_boundary_assignment/request.json").read_text(encoding="utf-8")
    )
    zones = _vector(run, request.inputs["zones"]).assign(source_feature_id=[2, 3, 1])
    path = tmp_path / "integer-zones.gpkg"
    pyogrio.write_dataframe(zones, path, layer="zones", driver="GPKG")
    result = allocate_transition_areas(
        _vector(run, request.inputs["overlay_polygons"]), _vector(run, request.inputs["first_classes"]),
        _vector(run, request.inputs["second_classes"]), pyogrio.read_dataframe(path, layer="zones"),
        boundary_assignment="split_by_area", boundary_tie="unassigned", area_tolerance_m2=TOLERANCE,
    )
    assert result["zone_ids"] == ["1", "2", "3"]
    assert {row["zone_id"] for row in result["allocations"]} == {"1", "2", None}
