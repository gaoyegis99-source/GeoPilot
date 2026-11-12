import csv
import json
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
import yaml
from shapely.geometry import Point

from geopilot.application.run_checks import RUN_CHECKS, RunCheckContext, RunCheckResult
from geopilot.domain.step_io import StepFileRef, StepOutput, StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.methods.loader import load_method_catalog
from geopilot.methods.models import OptionBinding
from geopilot.plans.models import AnalysisPlan, PlanOutput, PlanStep
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
GOLDEN = ROOT / "tests/fixtures/vector"
REFERENCE = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))
POINT_REFERENCE = REFERENCE["relations"]["intersects"]
CSV_FIELDS = ("zone_id", "assignment_status", "count", "source_feature_ids")
EXPAND = "expand_analysis_extent"
SELECT = "select_roads_from_expanded_extent"
BUFFER = "create_buffer"


@pytest.fixture(scope="module")
def plan():
    method = load_method_catalog(CONFIG / "methods").get("road-buffer-statistics", 2)
    options = {
        "distance_m": 500, "distance_method": "local_projected", "distance_crs_ref": REFERENCE["crs"],
        "spatial_relation": "intersects", "boundary_assignment": "centroid", "boundary_tie": "unassigned",
        "statistic": "count", "output_scope": "clipped_to_analysis_extent",
    }
    steps = []
    for step in method.steps:
        outputs = {}
        for name, kind in step.outputs.items():
            suffix = "csv" if kind == "table_csv" else "gpkg"
            outputs[name] = PlanOutput(
                type=kind, target=f"intermediate/{step.key}-{name}.{suffix}", layer=name if suffix == "gpkg" else None,
            )
        steps.append(PlanStep(
            key=step.key, tool=step.tool, inputs=step.inputs,
            parameters={name: options[binding.option] if isinstance(binding, OptionBinding) else binding.literal
                        for name, binding in step.parameters.items()}, outputs=outputs,
        ))
    return AnalysisPlan(
        id="road-checks", task_id="road-checks", version=1, status="confirmed", goal={"summary": "Count road targets"},
        inputs={role: {"asset_id": role, "asset_version": 1} for role in method.inputs},
        critical_options=options, method=method.ref, steps=steps,
        expected_artifacts=method.expected_artifacts, check_rules=method.check_rules, issues=[],
    )


def _stage_assets(workdir, plan):
    (workdir / "inputs").mkdir()
    (workdir / "intermediate").mkdir()
    shutil.copy2(GOLDEN / "road-buffer.gpkg", workdir / "inputs/road-buffer.gpkg")
    return {role: StepFileRef(path="inputs/road-buffer.gpkg", layer=role) for role in plan.inputs}


def _output(plan, step_key, role):
    step = next(step for step in plan.steps if step.key == step_key)
    output = step.outputs[role]
    return StepOutput(path=output.target, layer=output.layer, type=output.type)


def _record(step_key, role, output):
    now = datetime.now(UTC)
    return StepResult(
        run_id="road-checks", step_key=step_key, status="SUCCEEDED", exit_code=0, outputs={role: output},
        log_path=f"steps/{step_key}/process.log", warnings=[], error=None, started_at=now, finished_at=now,
        environment={},
    )


def _write_layer(workdir, output, frame):
    pyogrio.write_dataframe(frame, workdir / output.path, layer=output.layer, driver="GPKG")


def _summary_rows():
    members = POINT_REFERENCE["zone_target_ids"]
    rows = [(zone, "assigned", len(ids), ids.copy()) for zone, ids in sorted(members.items())]
    unassigned = POINT_REFERENCE["unassigned_target_ids"]
    return rows + [("", "unassigned", len(unassigned), unassigned.copy())]


def _write_summary(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(CSV_FIELDS)
        writer.writerows((zone, status, count, json.dumps(ids)) for zone, status, count, ids in rows)


@pytest.fixture
def run_files(tmp_path, plan):
    assets = _stage_assets(tmp_path, plan)
    source = tmp_path / assets["roads"].path
    roads = pyogrio.read_dataframe(source, layer="roads")
    extent = pyogrio.read_dataframe(source, layer="analysis_extent")
    expanded = extent.set_geometry(extent.geometry.buffer(REFERENCE["distance_m"], resolution=16))
    candidates = roads[roads.source_feature_id.isin(REFERENCE["candidate_road_ids"])]
    targets = pyogrio.read_dataframe(source, layer="targets")
    selected = targets[targets.source_feature_id.isin(POINT_REFERENCE["clipped_target_ids"])]

    expanded_output = _output(plan, EXPAND, "expanded_extent")
    candidate_output = _output(plan, SELECT, "roads")
    artifacts = {artifact.role: _output(plan, artifact.step, artifact.output) for artifact in plan.expected_artifacts}
    _write_layer(tmp_path, expanded_output, expanded)
    _write_layer(tmp_path, candidate_output, candidates)
    _write_layer(tmp_path, artifacts["selected_targets"], selected)
    buffer = gpd.GeoDataFrame(geometry=candidates.geometry.buffer(REFERENCE["distance_m"], resolution=16), crs=roads.crs)
    _write_layer(tmp_path, artifacts["buffer_extent"], buffer)
    _write_summary(tmp_path / artifacts["zone_statistics"].path, _summary_rows())
    return RunCheckContext(
        plan=plan, workdir=tmp_path, asset_files=assets, artifacts=artifacts,
        steps={EXPAND: _record(EXPAND, "expanded_extent", expanded_output), SELECT: _record(SELECT, "roads", candidate_output)},
    )


def test_reopens_expected_artifacts_and_reports_each_missing_or_unreadable_role(run_files):
    assert RUN_CHECKS["output_exists"](run_files).passed
    artifacts = dict(run_files.artifacts)
    del artifacts["selected_targets"]
    (run_files.workdir / artifacts["zone_statistics"].path).write_bytes(b"")
    artifacts["buffer_extent"] = artifacts["buffer_extent"].model_copy(update={"layer": "unknown_layer"})

    result = RUN_CHECKS["output_exists"](replace(run_files, artifacts=artifacts))

    assert isinstance(result, RunCheckResult) and not result.passed
    assert set(result.observed["problems"]) == {"selected_targets", "zone_statistics", "buffer_extent"}


def test_artifact_reference_cannot_follow_a_link_out_of_workspace(run_files, tmp_path):
    reference = run_files.artifacts["selected_targets"]
    original = run_files.workdir / reference.path
    outside = tmp_path.parent / f"{tmp_path.name}-outside.gpkg"
    original.rename(outside)
    original.symlink_to(outside)

    result = RUN_CHECKS["output_exists"](run_files)

    assert not result.passed
    assert "Symbolic links" in result.observed["problems"]["selected_targets"]
    assert outside.is_file()


def test_candidate_reference_detects_missing_influence_road_and_excess_road(run_files):
    check = RUN_CHECKS["outside_roads_within_influence_included"]
    initial = check(run_files)
    assert initial.passed and initial.observed["expected_ids"] == REFERENCE["candidate_road_ids"]
    output = run_files.steps[SELECT].outputs["roads"]
    roads = pyogrio.read_dataframe(run_files.workdir / run_files.asset_files["roads"].path, layer="roads")
    _write_layer(run_files.workdir, output, roads[roads.source_feature_id.isin({"road-main", "road-far"})])

    failed = check(run_files)

    assert not failed.passed
    assert failed.observed["missing_ids"] == ["road-edge"]
    assert failed.observed["unexpected_ids"] == ["road-far"]


def test_clipped_scope_includes_boundary_and_checks_only_selected_geometry(run_files):
    check = RUN_CHECKS["output_clipped_to_analysis_extent"]
    assert check(run_files).passed
    output = run_files.artifacts["selected_targets"]
    targets = pyogrio.read_dataframe(run_files.workdir / output.path, layer=output.layer)
    edge = gpd.GeoDataFrame({"source_feature_id": ["extent-edge"]}, geometry=[Point(500000, 4400000)], crs=targets.crs)
    _write_layer(run_files.workdir, output, pd.concat([targets, edge], ignore_index=True))
    assert check(run_files).passed
    outside = gpd.GeoDataFrame(
        {"source_feature_id": ["outside"]}, geometry=[Point(500000, 4402001)], crs=targets.crs,
    )
    _write_layer(run_files.workdir, output, pd.concat([targets, edge, outside], ignore_index=True))

    failed = check(run_files)

    assert not failed.passed and failed.observed["outside_ids"] == ["outside"]


def test_csv_counts_are_reconciled_to_unique_real_selected_ids(run_files):
    check = RUN_CHECKS["detail_count_matches_summary"]
    target = run_files.artifacts["selected_targets"]
    selected = pyogrio.read_dataframe(run_files.workdir / target.path, layer=target.layer)
    _write_layer(run_files.workdir, target, pd.concat([selected, selected.iloc[:1]], ignore_index=True))

    result = check(run_files)

    assert result.passed
    assert result.observed["selected_rows"] == 7
    assert result.observed["selected_unique_count"] == 6
    assert (result.observed["assigned_count"], result.observed["unassigned_count"]) == (5, 1)


@pytest.mark.parametrize("tamper", ["count", "duplicate", "same_count_wrong_id"])
def test_csv_detects_row_count_mismatch_duplicates_and_wrong_ids(run_files, tamper):
    rows = _summary_rows()
    zone, status, count, ids = rows[0]
    if tamper == "count":
        count += 1
    elif tamper == "duplicate":
        ids[-1] = rows[1][3][0]
    else:
        ids[-1] = "unknown-building"
    rows[0] = (zone, status, count, ids)
    _write_summary(run_files.workdir / run_files.artifacts["zone_statistics"].path, rows)

    result = RUN_CHECKS["detail_count_matches_summary"](run_files)

    assert not result.passed
    if tamper == "count":
        assert result.observed["count_mismatch_rows"] == [2]
    elif tamper == "duplicate":
        assert result.observed["duplicate_ids"] == [rows[1][3][0]]
    else:
        assert result.observed["unexpected_ids"] == ["unknown-building"]
        assert result.observed["missing_ids"] == ["building-overlap"]


@pytest.fixture(scope="module")
def real_buffer_audits(tmp_path_factory, plan):
    executable = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if not executable:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS for real buffer process evidence")
    workdir = tmp_path_factory.mktemp("run-check-buffers")
    assets = _stage_assets(workdir, plan)
    roads = pyogrio.read_dataframe(workdir / assets["roads"].path, layer="roads")
    prepared = _output(plan, "choose_measurement_method", "roads")
    _write_layer(workdir, prepared, roads[roads.source_feature_id.isin(REFERENCE["candidate_road_ids"])])
    registry = ToolRegistry(qgis_process=executable)
    bindings = {}
    for name in ("qgis-buffer.yaml", "qgis-road-workflow.yaml"):
        bindings.update(yaml.safe_load((CONFIG / "runner_bindings" / name).read_text(encoding="utf-8")))
    steps = {}
    for key, source in ((EXPAND, assets["analysis_extent"]), (BUFFER, prepared)):
        step = next(step for step in plan.steps if step.key == key)
        contract_path = CONFIG / "tools" / f"{step.tool.id}.yaml"
        registry.register(ToolContract.model_validate(yaml.safe_load(contract_path.read_text(encoding="utf-8"))))
        (workdir / f"steps/{key}").mkdir(parents=True)
        request = StepRequest(
            run_id="road-checks", step_key=key, tool=step.tool, workdir_ref="road-checks",
            inputs={"source": StepFileRef(path=source.path, layer=source.layer)}, parameters=step.parameters,
            output_targets={name: StepFileRef(path=target.target, layer=target.layer) for name, target in step.outputs.items()},
            log_path=f"steps/{key}/process.log", cancel_path="cancel.flag",
        )
        result = run_step(request, registry=registry, workdir=workdir, qgis_bindings=bindings, qgis_process=executable)
        assert result.status == "SUCCEEDED", result
        steps[key] = result
    return RunCheckContext(plan=plan, workdir=workdir, asset_files=assets, steps=steps, artifacts={})


@pytest.mark.parametrize("distance", [500, {"value": 500, "unit": "meter"}, {"value": 0.5, "unit": "kilometer"}])
def test_both_real_buffer_audits_match_confirmed_distance_policy(real_buffer_audits, distance):
    options = {**real_buffer_audits.plan.critical_options, "distance_m": distance}
    context = replace(real_buffer_audits, plan=real_buffer_audits.plan.model_copy(update={"critical_options": options}))
    result = RUN_CHECKS["measurement_policy_satisfied"](context)

    assert result.passed, result
    assert set(result.observed["steps"]) == {EXPAND, BUFFER}
    for evidence in result.observed["steps"].values():
        assert evidence["request_distance_m"] == evidence["qgis_distance_m"] == 500
        assert evidence["crs"] == REFERENCE["crs"] and evidence["unit"] == "meter"


@pytest.mark.parametrize("tamper", ["request_distance", "audit_unit", "engine_distance", "source_crs"])
def test_disagreeing_buffer_evidence_fails_rule_and_preserves_execution(real_buffer_audits, tmp_path, tamper):
    workdir = tmp_path / "run"
    shutil.copytree(real_buffer_audits.workdir, workdir)
    context = replace(real_buffer_audits, workdir=workdir)
    log = workdir / context.steps[BUFFER].log_path
    if tamper == "request_distance":
        request = log.with_name("request.json")
        record = json.loads(request.read_text(encoding="utf-8"))
        record["parameters"]["distance"] = 499
        request.write_text(json.dumps(record), encoding="utf-8")
    elif tamper == "source_crs":
        source = _output(context.plan, "choose_measurement_method", "roads")
        frame = pyogrio.read_dataframe(workdir / source.path, layer=source.layer)
        _write_layer(workdir, source, frame.set_crs("EPSG:32651", allow_override=True))
    else:
        old, new = ('"unit": "meter"', '"unit": "foot"') if tamper == "audit_unit" else ('"DISTANCE": 500,', '"DISTANCE": 10,')
        text = log.read_text(encoding="utf-8")
        assert old in text
        log.write_text(text.replace(old, new, 1), encoding="utf-8")

    result = RUN_CHECKS["measurement_policy_satisfied"](context)

    assert not result.passed and BUFFER in result.observed["problems"]
    assert all(record.status == "SUCCEEDED" for record in context.steps.values())
