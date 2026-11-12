import csv
import json
from pathlib import Path
from shutil import copyfile

import geopandas as gpd
import numpy as np
import pyogrio
import pytest
import rasterio
from affine import Affine
from shapely import Polygon

from geopilot.application.run_inputs import stage_run_inputs
from geopilot.artifacts.service import insert_artifacts, prepare_artifacts
from geopilot.checks.artifact_checks import NumericTolerance, check_run_artifacts, raster_match_rule, vector_rule
from geopilot.data.assets import register_asset
from geopilot.domain.step_io import StepResult
from geopilot.persistence.run_repository import get_run, insert_run, save_run_step, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.plans.models import AnalysisPlan
from geopilot.runs.models import Run, RunStep


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
VECTOR = FIXTURES / "vector/road-buffer.gpkg"
RASTERS = FIXTURES / "raster"
RASTER_REFERENCE = json.loads((RASTERS / "expected.json").read_text(encoding="utf-8"))
VECTOR_REFERENCE = json.loads((FIXTURES / "vector/expected.json").read_text(encoding="utf-8"))["relations"]["intersects"]
STAMP = "2026-11-11T00:00:00Z"


def _case(tmp_path, *, outputs, input_sources, rules):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    workspace = create_run_workspace(tmp_path / "workspace", "task", "run")
    inputs = {role: register_asset(database, source, layer=layer).ref for role, (source, layer) in input_sources.items()}
    declarations = {}
    files = {}
    for role, (source, kind, layer) in outputs.items():
        relative = f"intermediate/{role}{source.suffix}"
        files[role] = workspace.path / relative
        copyfile(source, files[role])
        declarations[role] = {"type": kind, "target": relative, "layer": layer}
    plan = AnalysisPlan.model_validate({
        "id": "plan", "version": 2, "task_id": "task", "status": "confirmed", "goal": {"summary": "Check real outputs"},
        "inputs": inputs, "method": {"id": "artifact-check-fixture", "version": 1}, "critical_options": {},
        "steps": [{"key": "produce", "tool": {"id": "fixture-tool", "version": 1}, "parameters": {},
                   "inputs": {role: {"asset": role} for role in inputs}, "outputs": declarations}],
        "expected_artifacts": [{"role": role, "step": "produce", "output": role, "type": item["type"]}
                               for role, item in declarations.items()],
        "check_rules": rules, "issues": [],
    })
    asset_files = stage_run_inputs(database, plan.inputs, workspace.path)
    workspace.request_path.write_text(json.dumps({
        "run_id": "run", "plan": plan.model_dump(mode="json"),
        "asset_files": {role: reference.model_dump(mode="json") for role, reference in asset_files.items()},
    }), encoding="utf-8")
    result = StepResult(
        run_id="run", step_key="produce", status="SUCCEEDED", exit_code=0,
        outputs={role: {"path": item["target"], "type": item["type"], "layer": item["layer"]}
                 for role, item in declarations.items()},
        log_path="steps/produce/process.log", warnings=[], error=None,
        started_at=STAMP, finished_at=STAMP, environment={},
    )
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES ('task', '{}', ?)", (STAMP,))
        connection.execute(
            """INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at)
               VALUES ('plan', 2, 'task', 'confirmed', ?, ?)""", (plan.model_dump_json(), STAMP),
        )
        run = insert_run(connection, Run(id="run", plan_id="plan", plan_version=2, workdir=str(workspace.path),
                                         created_at=STAMP, execution_status="RUNNING"))
        save_run_step(connection, RunStep(run_id="run", step_key="produce", position=0, status=result.status, result=result))
        prepared = prepare_artifacts("run", plan, workspace.path, {"produce": result})
        insert_artifacts(connection, prepared)
        update_run(connection, run.model_copy(update={"execution_status": "SUCCEEDED", "check_status": "CHECKING"}))
    return database, files


def _selected(path, identifiers):
    frame = pyogrio.read_dataframe(VECTOR, layer="targets")
    pyogrio.write_dataframe(frame.loc[frame.source_feature_id.isin(identifiers)], path, layer="selected")
    return path


@pytest.mark.parametrize("fault", [None, "wrong_id", "outside"])
def test_reuses_exact_vector_scope_and_detail_summary_checks(tmp_path, fault):
    ids = VECTOR_REFERENCE["clipped_target_ids"].copy()
    if fault == "outside":
        ids.append("building-outside-extent")
    selected = _selected(tmp_path / "selected.gpkg", ids)
    summary = tmp_path / "zone-statistics.csv"
    west = VECTOR_REFERENCE["zone_target_ids"]["zone-west"].copy()
    if fault == "wrong_id":
        west[0] = "wrong-but-same-count"
    unassigned = VECTOR_REFERENCE["unassigned_target_ids"] + (["building-outside-extent"] if fault == "outside" else [])
    with summary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("zone_id", "assignment_status", "count", "source_feature_ids"))
        for zone, status, members in (("zone-west", "assigned", west),
                                      ("zone-east", "assigned", VECTOR_REFERENCE["zone_target_ids"]["zone-east"]),
                                      ("", "unassigned", unassigned)):
            writer.writerow((zone, status, len(members), json.dumps(members)))
    scope_rule = "output_clipped_to_analysis_extent"
    summary_rule = "detail_count_matches_summary"
    database, _ = _case(tmp_path, outputs={"selected_targets": (selected, "vector_point", "selected"),
                                           "zone_statistics": (summary, "table_csv", None)},
                        input_sources={"analysis_extent": (VECTOR, "analysis_extent")},
                        rules=[scope_rule, summary_rule, "measurement_policy_satisfied"])

    evidence = check_run_artifacts(database, "run")

    assert not any(item.rule == "measurement_policy_satisfied" for item in evidence)
    extent, = [item for item in evidence if item.rule == scope_rule]
    counts, = [item for item in evidence if item.rule == summary_rule]
    assert extent.passed is (fault != "outside") and counts.passed is (fault != "wrong_id")
    assert extent.observed["outside_ids"] == (["building-outside-extent"] if fault == "outside" else [])
    assert counts.observed["summary_total_count"] == len(ids)
    assert counts.observed["unexpected_ids"] == (["wrong-but-same-count"] if fault == "wrong_id" else [])
    assert all(item.phase == "artifact" and item.severity == "blocking" for item in evidence)


@pytest.mark.parametrize("severity", ["blocking", "warning"])
def test_readable_vector_can_fail_an_explicit_count_rule(tmp_path, severity):
    selected = _selected(tmp_path / "filtered.gpkg", ["building-west"])
    name = "filtered_feature_count"
    database, _ = _case(tmp_path, outputs={"filtered_features": (selected, "vector_point", "selected")},
                        input_sources={"analysis_extent": (VECTOR, "analysis_extent")}, rules=[name])
    rules = {name: vector_rule(name, "filtered_features", scope_role="analysis_extent", expected_count=2, severity=severity)}

    evidence = check_run_artifacts(database, "run", rules=rules)

    failed, = [item for item in evidence if item.passed is False]
    assert (failed.rule, failed.field, failed.severity, failed.observed, failed.expected) == (
        name, "artifacts.filtered_features.feature_count", severity, 1, 2,
    )
    assert all(item.passed is True for item in check_run_artifacts(database, "run", rules={}))
    with open_database(database) as connection:
        assert get_run(connection, "run").check_status == "CHECKING"


def test_readable_geopackage_with_invalid_polygon_fails_geometry_rule(tmp_path):
    shape = Polygon([(500000, 4400000), (500010, 4400010), (500000, 4400010), (500010, 4400000)])
    path = tmp_path / "invalid.gpkg"
    pyogrio.write_dataframe(gpd.GeoDataFrame({"source_feature_id": ["self-crossing"]}, geometry=[shape], crs=32650),
                           path, layer="polygons")
    database, _ = _case(tmp_path, outputs={"areas": (path, "vector_polygon", "polygons")},
                        input_sources={}, rules=["polygon_geometry"])

    results = check_run_artifacts(database, "run", rules={"polygon_geometry": vector_rule("polygon_geometry", "areas")})

    failure, = [item for item in results if item.passed is False]
    assert failure.rule == "polygon_geometry" and failure.field == "artifacts.areas.geometry"
    assert failure.observed["invalid_rows"] == 1


def test_changed_artifact_blocks_integrity_and_skips_configured_rule(tmp_path):
    selected = _selected(tmp_path / "filtered.gpkg", ["building-west"])
    name = "feature_count"
    database, files = _case(tmp_path, outputs={"points": (selected, "vector_point", "selected")},
                            input_sources={}, rules=[name])
    source = files["points"]
    pyogrio.write_dataframe(pyogrio.read_dataframe(selected, layer="selected"), source, layer="selected", append=True)

    evidence = check_run_artifacts(database, "run", rules={name: vector_rule(name, "points", expected_count=1)})

    integrity, = [item for item in evidence if item.rule == "artifact_integrity" and item.passed is False]
    assert integrity.field == "artifacts.points.fingerprint"
    skipped, = [item for item in evidence if item.rule == name]
    assert skipped.passed is None


@pytest.mark.parametrize("fault", [None, "edge_pixel", "interior_pixel", "mask", "transform"])
def test_actual_raster_grid_mask_and_dn_values(tmp_path, fault):
    reference = RASTERS / "two-band.tif"
    output = tmp_path / "result.tif"
    copyfile(reference, output)
    with rasterio.open(output, "r+") as dataset:
        if fault == "transform":
            dataset.transform = dataset.transform * Affine.translation(1, 0)
        if fault in {"edge_pixel", "interior_pixel", "mask"}:
            band = dataset.read(1)
            if fault == "interior_pixel":
                band[1, 1] = 113
            else:
                band[0, 0] = 102 if fault == "edge_pixel" else 65535
            dataset.write(band, 1)
    name = "dn_grid_matches_reference"
    database, _ = _case(tmp_path, outputs={"image": (output, "raster", None)},
                        input_sources={"reference_image": (reference, None)}, rules=[name])
    tolerance = NumericTolerance(absolute=0, relative=0, source="P0-06 integer DN values are exact")
    rules = {name: raster_match_rule(name, "image", reference_role="reference_image", tolerance=tolerance)}

    evidence = check_run_artifacts(database, "run", rules=rules)

    grid, = [item for item in evidence if item.rule == name and item.field == "artifacts.image.grid"]
    pixels, = [item for item in evidence if item.rule == name and item.field == "artifacts.image.pixels"]
    assert grid.passed is (fault != "transform")
    assert pixels.passed is (None if fault == "transform" else fault is None)
    if fault is None:
        assert pixels.observed["common_valid_pixels"] == RASTER_REFERENCE["two-band.tif"]["common_valid_pixels"]
        assert pixels.observed["edge"]["common_valid_pixels"] == 9
        assert pixels.observed["interior"]["common_valid_pixels"] == 1
    if fault == "edge_pixel":
        assert pixels.observed["value_mismatches"] == 1 and pixels.observed["first_value_mismatch"]["expected"] == 101
        assert pixels.observed["edge"]["value_mismatches"] == 1
        assert pixels.observed["interior"]["value_mismatches"] == 0
    if fault == "interior_pixel":
        assert pixels.observed["value_mismatches"] == 1 and pixels.observed["first_value_mismatch"]["expected"] == 112
        assert pixels.observed["interior"]["value_mismatches"] == 1
        assert pixels.observed["edge"]["value_mismatches"] == 0
    if fault == "mask":
        assert pixels.observed["mask_mismatches"] == 1
        assert pixels.observed["edge"]["mask_mismatches"] == 1
    assert pixels.expected["tolerance"]["source"] == tolerance.source


@pytest.mark.parametrize("ulps", [1, 2])
def test_float32_tolerance_uses_explicit_independent_reference(tmp_path, ulps):
    reference = RASTERS / "dem-plane.tif"
    output = tmp_path / "ramp.tif"
    copyfile(reference, output)
    with rasterio.open(output, "r+") as dataset:
        band = dataset.read(1)
        assert band[3, 6] == RASTER_REFERENCE["dem-plane.tif"]["pixels"][0][3][6] == 160
        for _ in range(ulps):
            band[3, 6] = np.nextafter(band[3, 6], np.float32(np.inf))
        dataset.write(band, 1)
    name = "elevation_matches_reference"
    database, _ = _case(tmp_path, outputs={"elevation": (output, "raster", None)},
                        input_sources={"reference_dem": (reference, None)}, rules=[name])
    tolerance = NumericTolerance(absolute=2 ** -16, relative=0, source="One binary32 ULP at the P0-06 160 m reference")

    evidence = check_run_artifacts(database, "run", rules={
        name: raster_match_rule(name, "elevation", reference_role="reference_dem", tolerance=tolerance),
    })

    values, = [item for item in evidence if item.rule == name and item.field.endswith(".pixels")]
    assert values.passed is (ulps == 1)
    assert values.expected["tolerance"]["absolute"] == 1.52587890625e-5
