import csv
import json
import os
import shutil
from pathlib import Path

import pyogrio
import pytest
import yaml

from geopilot.application.method_preconditions import METHOD_PRECONDITIONS
from geopilot.application.plan_runner import run_confirmed_plan
from geopilot.application.run_checks import RUN_CHECKS, RunCheckResult
from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, load_plan, save_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/fixtures/vector"
SOURCE = GOLDEN / "road-buffer.gpkg"
WORKERS = {
    "geopilot.planar_area": "planar_area.py",
    "geopilot.prepare_measurement_lines": "prepare_measurement_lines.py",
    "geopilot.clip_points_to_extent": "clip_points_to_extent.py",
    "geopilot.count_points_by_zone": "count_points_by_zone.py",
}


@pytest.fixture(scope="module")
def runtime():
    executable = os.getenv("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if not executable:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS for the real P3 workflow")
    registry = ToolRegistry(qgis_process=executable, python_implementations=WORKERS)
    for path in sorted((ROOT / "config/tools").glob("*.yaml")):
        registry.register(ToolContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))))
    bindings = {}
    for path in sorted((ROOT / "config/runner_bindings").glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert not bindings.keys() & document.keys()
        bindings.update(document)
    return {
        "registry": registry,
        "qgis_bindings": bindings,
        "qgis_process": executable,
        "python_implementations": {name: ROOT / "src/geopilot/gis/tools" / file for name, file in WORKERS.items()},
    }


def _confirm(tmp_path, runtime, method_id, method_version, layers, options):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    catalog = load_method_catalog(ROOT / "config/methods")
    method = catalog.get(method_id, method_version)
    refs = {role: register_asset(database, SOURCE, layer=layer).ref for role, layer in layers.items()}
    context = build_task_context(
        database, task_id="task-001", goal=TaskGoal(summary="Run the configured GIS method"),
        method=method, selected=refs, registry=runtime["registry"],
    )
    draft = build_draft(context, method, plan_id="plan-001", options=options)
    assert not draft.issues, draft.issues
    save_draft(database, draft, context=context)
    confirmed = confirm_plan(
        database, draft.id, draft.version, catalog=catalog, registry=runtime["registry"],
        registered_rules=RUN_CHECKS, precondition_checks=METHOD_PRECONDITIONS,
    )
    assert confirmed.status == "confirmed" and confirmed.method == method.ref
    assert load_plan(database, confirmed.id, confirmed.version) == confirmed
    with open_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 0
    return database, confirmed


def _read_vector(run, ref):
    return pyogrio.read_dataframe(run.workdir / ref.path, layer=ref.layer)


def _feature_ids(run, ref):
    return sorted(_read_vector(run, ref)["source_feature_id"])


def _check_persisted_artifacts(run, plan):
    manifest = json.loads((run.workdir / "outputs/manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifacts"] == {
        role: {**ref.model_dump(mode="json"), "fingerprint": fingerprint_file(run.workdir / ref.path).digest}
        for role, ref in run.artifacts.items()
    }
    report = json.loads((run.workdir / "checks/report.json").read_text(encoding="utf-8"))
    assert report["run_id"] == run.run_id and report["status"] == "PASSED"
    assert list(report["rules"]) == list(plan.check_rules)
    assert all(rule["passed"] for rule in report["rules"].values())
    return report


def _check_statistics(run, expected):
    output = run.workdir / run.artifacts["zone_statistics"].path
    with output.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    counts = {row["zone_id"] or "unassigned": int(row["count"]) for row in rows}
    counts["total"] = sum(counts.values())
    assert counts == expected["counts"]
    grouped = {row["zone_id"]: json.loads(row["source_feature_ids"]) for row in rows if row["zone_id"]}
    assert grouped == expected["zone_target_ids"]
    assert [json.loads(row["source_feature_ids"]) for row in rows if not row["zone_id"]] == [
        expected["unassigned_target_ids"]
    ]
    return output.read_bytes()


def test_road_workflow_passes_fixed_road_cases_and_reproduces_in_an_independent_run(tmp_path, runtime):
    expected = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))
    relation = expected["relations"]["intersects"]
    original = fingerprint_file(SOURCE)
    source_roads = pyogrio.read_dataframe(SOURCE, layer="roads").set_index("source_feature_id")
    source_points = pyogrio.read_dataframe(SOURCE, layer="targets").set_index("source_feature_id")
    extent = pyogrio.read_dataframe(SOURCE, layer="analysis_extent").geometry.union_all()
    distances = source_roads.geometry.distance(extent).to_dict()
    assert distances == expected["road_distance_to_extent_m"]
    database, plan = _confirm(
        tmp_path, runtime, "road-buffer-statistics", 2,
        {role: role for role in ("analysis_extent", "roads", "targets", "zones")},
        {
            "distance_m": 500, "distance_method": "local_projected", "distance_crs_ref": "EPSG:32650",
            "spatial_relation": "intersects", "boundary_assignment": "centroid",
            "boundary_tie": "unassigned", "statistic": "count", "output_scope": "clipped_to_analysis_extent",
        },
    )

    runs = [
        run_confirmed_plan(
            database, plan.id, plan.version, workspace_root=tmp_path / "workspace",
            run_id=run_id, **runtime,
        )
        for run_id in ("road-first", "road-repeat")
    ]

    statistics = []
    buffers = []
    for run in runs:
        assert run.execution_status == "SUCCEEDED" and run.check_status == "PASSED", run
        assert list(run.steps) == [step.key for step in plan.steps]
        assert all(step.status == "SUCCEEDED" and step.exit_code == 0 for step in run.steps.values())
        assert set(run.artifacts) == {"buffer_extent", "selected_targets", "zone_statistics"}
        snapshot = json.loads((run.workdir / "request.json").read_text(encoding="utf-8"))
        assert snapshot["plan"] == plan.model_dump(mode="json")
        assert {role: ref["layer"] for role, ref in snapshot["asset_files"].items()} == {
            role: role for role in plan.inputs
        }
        for ref in snapshot["asset_files"].values():
            assert fingerprint_file(run.workdir / ref["path"]).digest == original.digest
        expanded_ref = run.steps["expand_analysis_extent"].outputs["expanded_extent"]
        expanded = _read_vector(run, expanded_ref)
        assert expanded.crs.to_epsg() == 32650
        assert tuple(expanded.total_bounds) == pytest.approx((499500, 4399500, 502500, 4402500), abs=1e-6, rel=0)
        roads = run.steps["select_roads_from_expanded_extent"].outputs["roads"]
        selected_roads = _feature_ids(run, roads)
        assert "road-edge" in selected_roads, "T07 must include the road 300 m outside the extent"
        assert "road-far" not in selected_roads, "T08 must exclude the road 650 m outside the extent"
        assert selected_roads == expected["candidate_road_ids"]
        candidates = run.steps["apply_spatial_relation"].outputs["targets"]
        assert _feature_ids(run, candidates) == relation["before_clip_target_ids"]
        clipped = _read_vector(run, run.artifacts["selected_targets"]).set_index("source_feature_id").sort_index()
        clipped_ids = clipped.index.tolist()
        assert "building-edge-road" in clipped_ids
        assert "building-far-road" not in clipped_ids
        assert clipped_ids == relation["clipped_target_ids"]
        assert clipped.crs == source_points.crs
        assert clipped.geometry.geom_equals_exact(source_points.loc[clipped_ids].geometry, tolerance=0).all()
        statistics.append(_check_statistics(run, relation))
        buffer = _read_vector(run, run.artifacts["buffer_extent"])
        assert buffer.crs.to_epsg() == 32650 and buffer.total_bounds[3] > 4402000
        assert buffer.geometry.is_valid.all()
        buffers.append(buffer.geometry.union_all())
        report = _check_persisted_artifacts(run, plan)
        candidate_check = report["rules"]["outside_roads_within_influence_included"]["observed"]
        assert candidate_check["actual_ids"] == expected["candidate_road_ids"]
        assert candidate_check["missing_ids"] == candidate_check["unexpected_ids"] == []
        request = json.loads((run.workdir / "steps/create_buffer/request.json").read_text(encoding="utf-8"))
        assert request["parameters"]["distance"] == 500
        assert request["tool"] == {"id": "qgis-buffer", "version": 1}

    assert runs[0].workdir != runs[1].workdir
    assert statistics[0] == statistics[1]
    assert buffers[0].hausdorff_distance(buffers[1]) <= 1e-6
    assert abs(buffers[0].area - buffers[1].area) <= 0.01
    assert fingerprint_file(SOURCE) == original
    with open_database(database) as connection:
        recorded = connection.execute(
            "SELECT id, plan_id, plan_version, execution_status, check_status FROM run ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in recorded] == [
        ("road-first", plan.id, plan.version, "SUCCEEDED", "PASSED"),
        ("road-repeat", plan.id, plan.version, "SUCCEEDED", "PASSED"),
    ]


def test_attribute_method_uses_the_same_confirmed_execution_and_artifact_flow(tmp_path, runtime):
    database, plan = _confirm(
        tmp_path, runtime, "attribute-filter-export", 1, {"features": "targets"},
        {"field_name": "source_feature_id", "match_value": "building-west"},
    )

    run = run_confirmed_plan(
        database, plan.id, plan.version, workspace_root=tmp_path / "workspace",
        run_id="attribute-first", **runtime,
    )

    assert run.execution_status == "SUCCEEDED" and run.check_status == "PASSED", run
    assert list(run.steps) == ["filter_features"]
    assert list(run.artifacts) == ["filtered_features"]
    assert _feature_ids(run, run.artifacts["filtered_features"]) == ["building-west"]
    request = json.loads((run.workdir / "steps/filter_features/request.json").read_text(encoding="utf-8"))
    assert request["parameters"] == {"field": "source_feature_id", "operator": "equals", "value": "building-west"}
    _check_persisted_artifacts(run, plan)

    def check_expected_feature(context):
        output = context.artifacts["filtered_features"]
        observed = pyogrio.read_dataframe(context.workdir / output.path, layer=output.layer)
        ids = observed["source_feature_id"].tolist()
        return RunCheckResult(ids == ["building-east"], "Compare the filtered ID to an independent expectation", ids)

    failed_check = run_confirmed_plan(
        database, plan.id, plan.version, workspace_root=tmp_path / "workspace",
        run_id="attribute-check-failure", check_rules={"output_exists": check_expected_feature}, **runtime,
    )
    assert failed_check.execution_status == "SUCCEEDED" and failed_check.check_status == "FAILED"
    assert failed_check.steps["filter_features"].status == "SUCCEEDED"
    report = json.loads((failed_check.workdir / "checks/report.json").read_text(encoding="utf-8"))
    assert report["rules"]["output_exists"]["observed"] == ["building-west"]
    with open_database(database) as connection:
        states = connection.execute(
            "SELECT execution_status, check_status FROM run WHERE id = ?", (failed_check.run_id,)
        ).fetchone()
    assert tuple(states) == ("SUCCEEDED", "FAILED")


def test_checker_interruption_preserves_real_execution_and_artifacts(tmp_path, runtime):
    database, plan = _confirm(
        tmp_path, runtime, "attribute-filter-export", 1, {"features": "targets"},
        {"field_name": "source_feature_id", "match_value": "building-west"},
    )

    def interrupted_check(context):
        assert context.steps["filter_features"].status == "SUCCEEDED"
        raise RuntimeError("Checker interrupted after feature export")

    run = run_confirmed_plan(
        database, plan.id, plan.version, workspace_root=tmp_path / "workspace",
        run_id="attribute-check-interrupted", check_rules={"output_exists": interrupted_check}, **runtime,
    )

    assert run.execution_status == "SUCCEEDED" and run.check_status == "NOT_CHECKED"
    assert _feature_ids(run, run.artifacts["filtered_features"]) == ["building-west"]
    assert run.error.code == "CHECK_INTERRUPTED"
    report = json.loads((run.workdir / "checks/report.json").read_text(encoding="utf-8"))
    assert report["status"] == "NOT_CHECKED"
    assert not report["rules"]
    assert report["completed_rules"] == []
    assert report["pending_rules"] == ["output_exists"]
    assert report["interruption"]["code"] == "CHECK_INTERRUPTED"
    with open_database(database) as connection:
        row = connection.execute(
            "SELECT execution_status, check_status, error_json FROM run WHERE id = ?", (run.run_id,)
        ).fetchone()
    assert tuple(row)[:2] == ("SUCCEEDED", "NOT_CHECKED")
    saved_error = json.loads(row["error_json"])
    assert "Checker interrupted after feature export" in saved_error["message"]
    assert saved_error["observed"] == report
