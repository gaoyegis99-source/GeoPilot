import hashlib
import json
from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest
import yaml
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from geopilot.application.run_inputs import stage_run_inputs
from geopilot.application.task_context import build_task_context
from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.assets import register_asset
from geopilot.domain.step_io import StepRequest, StepResult
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.methods.land_use_overlay import preflight_land_use_step
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.plans.compiler import compile_step_request
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, save_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/land-use"
WORKER = ROOT / "src/geopilot/methods/land_use_overlay.py"
TOOL_SUFFIXES = ("read", "map", "project", "overlay")
READ, MAP, PROJECT, OVERLAY = (
    "read_two_periods", "map_classification_systems", "choose_equal_area_projection", "overlay",
)
FIRST, SECOND, EXTENT = "first_classes", "second_classes", "analysis_extent"
CRS = "EPSG:6933"
# Equatorial control points keep the round-trip error well below the fixed area tolerance.
X, Y = 1_000_000, 0
TOLERANCE_M2 = 0.01


def _box(left, bottom, right, top):
    return box(X + left, Y + bottom, X + right, Y + top)


@pytest.fixture(scope="module")
def runtime():
    catalog = load_method_catalog(FIXTURE)
    assert not catalog.issues, catalog.issues
    method = catalog.get("land-use-overlay", 1)
    implementations = {"geopilot.land_use_overlay": WORKER}
    registry = ToolRegistry(python_implementations=implementations)
    for suffix in TOOL_SUFFIXES:
        path = ROOT / f"config/tools/python-land-use-{suffix}.yaml"
        contract = ToolContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        registry.register(contract, required_modules=("geopandas", "pyogrio", "pyproj", "shapely"))
    return catalog, method, registry, implementations


def _create_source(directory, role, records, geometry, *, crs=CRS, geometry_type=None):
    path = directory / f"{role}.gpkg"
    frame = gpd.GeoDataFrame(records, geometry=geometry, crs=CRS).to_crs(crs)
    pyogrio.write_dataframe(frame, path, layer=role, driver="GPKG", geometry_type=geometry_type)
    return path


def _preflight(step):
    preflight_step_inputs(step)
    preflight_land_use_step(step)


def _execute(tmp_path, runtime, *, variant="standard", second_crs=CRS, options=None, preflight=_preflight):
    catalog, method, registry, implementations = runtime
    first_ids = ["west-2018", "east-2018", "outside-2018"]
    first = [_box(0, 0, 40, 100), _box(40, 0, 100, 100), _box(100, 0, 120, 100)]
    if variant == "cancelled_net":
        first[1] = unary_union([_box(39, 0, 100, 50), _box(41, 50, 100, 100)])
    elif variant == "rounding_gap":
        first[1] = _box(40.00005, 0, 100, 100)
    elif variant == "duplicate_id":
        first_ids[1] = first_ids[0]
    elif variant == "self_intersection":
        first[0] = Polygon([(X, Y), (X + 40, Y + 100), (X + 40, Y), (X, Y + 100), (X, Y)])
    paths = {
        FIRST: _create_source(tmp_path, FIRST, {
            "source_feature_id": first_ids, "old_code": [10, 20, 30], "survey": ["A"] * 3,
        }, first),
        SECOND: _create_source(tmp_path, SECOND, {
            "source_feature_id": ["south-2024", "north-2024"], "new_code": ["01", "02"], "survey": ["B"] * 2,
        }, [_box(0, 0, 100, 25), _box(0, 25, 100, 100)], crs=second_crs),
        EXTENT: _create_source(tmp_path, EXTENT, {}, [_box(0, 0, 100, 100)]),
    }
    digests = {role: hashlib.sha256(path.read_bytes()).hexdigest() for role, path in paths.items()}
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    selected = {role: register_asset(database, path, layer=role).ref for role, path in paths.items()}
    context = build_task_context(
        database, task_id="land-use-task", goal=TaskGoal(summary="核对两期土地利用叠加"),
        method=method, selected=selected, registry=registry,
    )
    selections = {
        "first_class_field": "old_code", "second_class_field": "new_code",
        "first_class_mapping": {"10": "cropland", "20": "woodland", "30": "wetland"},
        "second_class_mapping": {"01": "cropland", "02": "wetland"},
        "first_period": "2018", "second_period": "2024",
        "area_crs": CRS, "overlay_area_tolerance": TOLERANCE_M2,
    }
    selections.update(options or {})
    draft = build_draft(context, method, plan_id="land-use-plan", options=selections)
    assert not draft.issues, draft.issues
    save_draft(database, draft, context=context)
    plan = confirm_plan(database, draft.id, draft.version, catalog=catalog, registry=registry,
                        registered_rules=("output_exists",))
    assert method.locked_order and tuple(step.key for step in plan.steps) == (READ, MAP, PROJECT, OVERLAY)
    workspace = create_run_workspace(tmp_path / "workspace", context.task_id, "overlay-run")
    asset_files = stage_run_inputs(database, plan.inputs, workspace.path)
    results = {}
    requests = {}
    for step in plan.steps:
        workspace.create_step(step.key)
        request = compile_step_request(
            plan, step.key, run_id="overlay-run", asset_files=asset_files, prior_results=results,
        )
        for output in request.output_targets.values():
            (workspace.path / output.path).parent.mkdir(parents=True, exist_ok=True)
        result = run_step(
            request, registry=registry, workdir=workspace.path, python_implementations=implementations,
            input_preflight=preflight,
        )
        requests[step.key], results[step.key] = request, result
        if result.status != "SUCCEEDED":
            break
    assert {role: hashlib.sha256(path.read_bytes()).hexdigest() for role, path in paths.items()} == digests
    assert all(hashlib.sha256((workspace.path / ref.path).read_bytes()).hexdigest() == digests[role]
               for role, ref in asset_files.items())
    return workspace.path, requests, results


def _read(workdir, result, role):
    ref = result.outputs[role]
    return pyogrio.read_dataframe(workdir / ref.path, layer=ref.layer)


def _log(workdir, result):
    return (workdir / result.log_path).read_text(encoding="utf-8")


def _report(workdir, result):
    ref = result.outputs["overlay_report"]
    return json.loads((workdir / ref.path).read_text(encoding="utf-8"))


@pytest.mark.parametrize("second_crs", [CRS, "EPSG:4326"])
def test_overlay_uses_confirmed_steps_and_preserves_two_real_periods(tmp_path, runtime, second_crs):
    workdir, requests, results = _execute(tmp_path, runtime, second_crs=second_crs)
    assert list(results) == [READ, MAP, PROJECT, OVERLAY]
    for step, result in results.items():
        assert result.status == "SUCCEEDED" and result.exit_code == 0, result
        log = _log(workdir, result)
        assert str(WORKER) in log and "exit_code: 0" in log
        assert "shapely" in result.environment and "pyproj" in result.environment
        assert StepResult.model_validate_json((workdir / "steps" / step / "result.json").read_text()) == result
        persisted = StepRequest.model_validate_json((workdir / "steps" / step / "request.json").read_text())
        assert persisted == requests[step]
    assert _read(workdir, results[READ], SECOND).crs.to_string() == second_crs
    assert _read(workdir, results[PROJECT], SECOND).crs.to_string() == CRS
    assert set(_read(workdir, results[MAP], FIRST)["source_class_code"]) == {"10", "20", "30"}
    assert requests[READ].inputs[FIRST].layer == FIRST and requests[READ].inputs[SECOND].layer == SECOND
    assert requests[OVERLAY].inputs[FIRST].path == results[PROJECT].outputs[FIRST].path

    polygons = _read(workdir, results[OVERLAY], "overlay_polygons")
    expected = {
        ("west-2018", "south-2024"): ("10", "01", "cropland", "cropland", False, _box(0, 0, 40, 25), 1000),
        ("west-2018", "north-2024"): ("10", "02", "cropland", "wetland", True, _box(0, 25, 40, 100), 3000),
        ("east-2018", "south-2024"): ("20", "01", "woodland", "cropland", True, _box(40, 0, 100, 25), 1500),
        ("east-2018", "north-2024"): ("20", "02", "woodland", "wetland", True, _box(40, 25, 100, 100), 4500),
    }
    assert polygons.crs.to_string() == CRS and polygons.geometry.is_valid.all()
    assert len(polygons) == 4 and polygons["source_feature_id"].is_unique
    assert set(zip(polygons["first_source_feature_id"], polygons["second_source_feature_id"])) == expected.keys()
    for row in polygons.itertuples():
        code1, code2, cls1, cls2, changed, shape, area = expected[
            row.first_source_feature_id, row.second_source_feature_id
        ]
        assert (row.first_period, row.second_period) == ("2018", "2024")
        assert (row.first_source_code, row.second_source_code) == (code1, code2)
        assert (row.from_class, row.to_class, row.is_change) == (cls1, cls2, changed)
        assert row.area_m2 == pytest.approx(area, abs=TOLERANCE_M2)
        assert row.geometry.area == pytest.approx(area, abs=TOLERANCE_M2)
        assert row.geometry.symmetric_difference(shape).area < TOLERANCE_M2
    report = _report(workdir, results[OVERLAY])
    assert report["area_crs"] == CRS and report["area_unit"] == "square_meter"
    assert report["area_tolerance_m2"] == TOLERANCE_M2
    assert report["analysis_extent_m2"] == 10_000
    assert report["overlay_area_m2"] == pytest.approx(10_000, abs=TOLERANCE_M2)
    assert report["fragment_count"] == 4
    for role in (FIRST, SECOND):
        assert report["periods"][role]["covered_area_m2"] == pytest.approx(10_000, abs=TOLERANCE_M2)
        assert report["periods"][role]["overlap_area_m2"] < TOLERANCE_M2
        assert report["periods"][role]["uncovered_area_m2"] < TOLERANCE_M2


def test_all_missing_codes_block_mapping_before_launch(tmp_path, runtime):
    workdir, requests, results = _execute(tmp_path, runtime, options={
        "first_class_mapping": {"10": "cropland"}, "second_class_mapping": {"01": "cropland"},
    })
    assert list(results) == [READ, MAP]
    blocked = results[MAP]
    assert blocked.status == "FAILED" and blocked.exit_code is None and blocked.outputs == {}
    error = blocked.error
    assert (error.code, error.phase, error.step_key, error.field) == (
        "METHOD_PRECONDITION_FAILED", "preflight", MAP, "parameters.first_class_mapping",
    )
    assert error.observed == {FIRST: ["20", "30"], SECOND: ["02"]}
    log = _log(workdir, blocked)
    assert "classification_mapping_complete" in log and "$ " not in log
    assert StepResult.model_validate_json((workdir / "steps" / MAP / "result.json").read_text()) == blocked
    assert not any((workdir / ref.path).exists() for ref in requests[MAP].output_targets.values())
    assert not (workdir / "steps" / PROJECT).exists()


def test_mapping_worker_also_rejects_unknown_code_without_hook(tmp_path, runtime):
    workdir, requests, results = _execute(
        tmp_path, runtime, options={"second_class_mapping": {"1": "cropland", "2": "wetland"}},
        preflight=None,
    )
    assert list(results) == [READ, MAP]
    blocked = results[MAP]
    assert blocked.status == "FAILED" and blocked.exit_code == 1
    log = _log(workdir, blocked)
    assert str(WORKER) in log and "parameters.second_class_mapping" in log
    assert '"01"' in log and '"02"' in log
    assert not any((workdir / ref.path).exists() for ref in requests[MAP].output_targets.values())


def test_overlap_and_gap_cannot_cancel_in_area_balance(tmp_path, runtime):
    workdir, requests, results = _execute(tmp_path, runtime, variant="cancelled_net")
    assert list(results) == [READ, MAP, PROJECT, OVERLAY]
    assert results[PROJECT].status == "SUCCEEDED"
    failed = results[OVERLAY]
    assert failed.status == "FAILED" and failed.exit_code == 1 and failed.outputs == {}
    log = _log(workdir, failed)
    assert '"clipped_area_sum_m2": 10000.0' in log
    assert '"overlap_area_m2": 50.0' in log and '"uncovered_area_m2": 50.0' in log
    assert not any((workdir / ref.path).exists() for ref in requests[OVERLAY].output_targets.values())


def test_small_coverage_rounding_is_reported_and_uses_the_confirmed_tolerance(tmp_path, runtime):
    workdir, _, results = _execute(tmp_path, runtime, variant="rounding_gap")
    assert results[OVERLAY].status == "SUCCEEDED"
    report = _report(workdir, results[OVERLAY])
    assert report["periods"][FIRST]["uncovered_area_m2"] == pytest.approx(0.005, abs=1e-7)
    assert report["overlay_area_m2"] == pytest.approx(9_999.995, abs=1e-7)


def test_non_equal_area_crs_does_not_produce_projected_outputs(tmp_path, runtime):
    workdir, requests, results = _execute(tmp_path, runtime, options={"area_crs": "EPSG:3857"})
    assert list(results) == [READ, MAP, PROJECT]
    assert results[PROJECT].status == "FAILED" and results[PROJECT].exit_code == 1
    assert "Web Mercator" in _log(workdir, results[PROJECT])
    assert not any((workdir / ref.path).exists() for ref in requests[PROJECT].output_targets.values())


@pytest.mark.parametrize("variant", ["duplicate_id", "self_intersection"])
def test_bad_source_identity_or_geometry_fails_before_mapping(tmp_path, runtime, variant):
    workdir, requests, results = _execute(tmp_path, runtime, variant=variant)
    assert list(results) == [READ]
    assert results[READ].status == "FAILED"
    log = _log(workdir, results[READ])
    if variant == "duplicate_id":
        assert "source_feature_id" in log and "unique" in log.lower()
    else:
        assert "geometry" in log and "invalid" in log.lower()
    assert not any((workdir / ref.path).exists() for ref in requests[READ].output_targets.values())
