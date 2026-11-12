import csv
import json
import sqlite3
from pathlib import Path
from shutil import copyfile

import pyogrio
import pytest
import rasterio
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.artifacts.service import (
    ArtifactRegistrationError,
    get_artifact,
    insert_artifacts,
    list_artifacts,
    prepare_artifacts,
)
from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepResult
from geopilot.persistence.run_repository import get_run, insert_run, save_run_step, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.plans.models import AnalysisPlan
from geopilot.runs.models import Run, RunStep


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
VECTOR = FIXTURES / "vector/road-buffer.gpkg"
RASTER = FIXTURES / "raster/dem-plane.tif"
STAMP = "2026-11-11T00:00:00Z"


@pytest.fixture
def completed(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    workdir = create_run_workspace(tmp_path / "workspace", "task-1", "run-1").path
    inputs = {
        "roads": register_asset(database, VECTOR, layer="roads").ref,
        "dem": register_asset(database, RASTER).ref,
        "unused": register_asset(database, VECTOR, layer="targets").ref,
    }
    steps = [
        {"key": "prepare", "tool": {"id": "prepare", "version": 3}, "inputs": {"source": {"asset": "roads"}},
         "parameters": {}, "outputs": {"lines": {"type": "vector_line", "target": "intermediate/roads.gpkg", "layer": "roads"}}},
        {"key": "terrain", "tool": {"id": "terrain", "version": 1}, "inputs": {"source": {"asset": "dem"}},
         "parameters": {}, "outputs": {"grid": {"type": "raster", "target": "intermediate/grid.tif"}}},
        {"key": "summarize", "tool": {"id": "summarize", "version": 2}, "parameters": {},
         "inputs": {"lines": {"step": "prepare", "output": "lines"}, "grid": {"step": "terrain", "output": "grid"}},
         "outputs": {"scores": {"type": "table_csv", "target": "intermediate/scores.csv"}}},
    ]
    plan = AnalysisPlan.model_validate({
        "id": "plan-1", "version": 2, "task_id": "task-1", "status": "confirmed",
        "goal": {"summary": "Register actual GIS outputs"}, "inputs": inputs,
        "critical_options": {"audit": 1}, "method": {"id": "test-artifacts", "version": 1}, "steps": steps,
        "expected_artifacts": [
            {"role": role, "step": step, "output": output, "type": kind}
            for role, step, output, kind in (
                ("roads", "prepare", "lines", "vector_line"), ("summary", "summarize", "scores", "table_csv"),
                ("elevation", "terrain", "grid", "raster"),
            )
        ],
        "check_rules": ["output_exists"], "issues": [],
    })
    copyfile(VECTOR, workdir / "intermediate/roads.gpkg")
    copyfile(RASTER, workdir / "intermediate/grid.tif")
    (workdir / "intermediate/scores.csv").write_text("road,count\nroad-main,1\n", encoding="utf-8")

    results = {}
    with open_database(database) as connection, connection:
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES ('task-1', '{}', ?)", (STAMP,))
        connection.execute(
            """INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at)
               VALUES (?, ?, ?, 'confirmed', ?, ?)""", (plan.id, plan.version, plan.task_id, plan.model_dump_json(), STAMP),
        )
        insert_run(connection, Run(id="run-1", plan_id=plan.id, plan_version=plan.version, workdir=str(workdir),
                                   created_at=STAMP, execution_status="RUNNING"))
        for position, step in enumerate(plan.steps):
            log = f"steps/{step.key}/process.log"
            result = StepResult(
                run_id="run-1", step_key=step.key, status="SUCCEEDED", exit_code=0,
                outputs={port: {"path": output.target, "layer": output.layer, "type": output.type}
                         for port, output in step.outputs.items()},
                log_path=log, warnings=[], error=None, started_at=STAMP, finished_at=STAMP, environment={},
            )
            save_run_step(connection, RunStep(run_id="run-1", step_key=step.key, position=position,
                                             status=result.status, result=result, log_path=log))
            results[step.key] = result
    return database, workdir, plan, results


def _prepare(completed):
    _, workdir, plan, results = completed
    return prepare_artifacts("run-1", plan, workdir, results)


def test_reopens_actual_outputs_and_atomically_records_lineage(completed):
    database, workdir, plan, _ = completed
    prepared = _prepare(completed)
    assert list(prepared.outputs) == ["roads", "summary", "elevation"]
    assert not (workdir / "outputs/manifest.json").exists()

    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        insert_artifacts(connection, prepared)
        run = get_run(connection, "run-1")
        update_run(connection, run.model_copy(update={"execution_status": "SUCCEEDED", "check_status": "CHECKING"}))
        with open_database(database) as observer:
            assert list_artifacts(observer, "run-1") == ()
            assert get_run(observer, "run-1").execution_status == "RUNNING"

    with open_database(database) as connection:
        records = {record.role: record for record in list_artifacts(connection, "run-1")}
        assert get_artifact(connection, "run-1", "roads") == records["roads"]
        assert get_artifact(connection, "run-1", "unreported") is None
        assert get_run(connection, "run-1").check_status == "CHECKING"
    for role, record in records.items():
        assert record.run_id == "run-1" and record.source.plan_id == plan.id and record.source.plan_version == 2
        assert record.fingerprint == fingerprint_file(workdir / record.uri).digest
        assert prepared.manifest["artifacts"][role] == {
            "path": record.uri, "layer": record.layer, "type": record.type, "fingerprint": record.fingerprint,
        }
    assert (records["roads"].kind, records["roads"].format, records["roads"].layer) == ("vector", "GPKG", "roads")
    assert records["roads"].metadata["feature_count"] == 3
    assert records["roads"].source.inputs == {"roads": plan.inputs["roads"]}
    assert records["summary"].source.inputs == {key: plan.inputs[key] for key in ("roads", "dem")}
    assert records["summary"].source.step_key == "summarize" and records["summary"].source.output_name == "scores"
    assert records["summary"].source.tool.version == 2 and records["summary"].metadata["row_count"] == 1
    assert (records["elevation"].kind, records["elevation"].format) == ("raster", "GTiff")
    with rasterio.open(workdir / records["elevation"].uri) as raster:
        assert records["elevation"].metadata["width"] == raster.width
        assert records["elevation"].metadata["height"] == raster.height


def test_registers_previews_and_downloads_a_large_source_id_list(completed, monkeypatch):
    database, workdir, _, _ = completed
    ids = [f"r{row:03}c{column:03}/building-zone-boundary" for row in range(128) for column in range(128)]
    field = json.dumps(ids, ensure_ascii=False)
    assert len(field) == 573_440
    path = workdir / "intermediate/scores.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("zone_id", "assignment_status", "count", "source_feature_ids"))
        writer.writerow(("", "unassigned", len(ids), field))
    original = path.read_bytes()

    prepared = _prepare(completed)
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        summary = next(record for record in insert_artifacts(connection, prepared) if record.role == "summary")
    assert summary.metadata["row_count"] == 1
    assert summary.fingerprint == fingerprint_file(path).digest
    with TestClient(create_app(database)) as client:
        url = f"/api/artifacts/{summary.id}"
        preview = client.get(f"{url}/table")
        assert preview.status_code == 200, preview.text
        assert preview.json()["rows"] == [["", "unassigned", str(len(ids)), field]]
        assert preview.json()["truncated"] is False
        monkeypatch.setattr("geopilot.api.artifact_routes._MAX_TABLE_PREVIEW_CELL_BYTES", 128)
        limited = client.get(f"{url}/table")
        assert limited.status_code == 200, limited.text
        assert limited.json()["rows"] == [] and limited.json()["truncated"] is True
        download = client.get(f"{url}/file")
        assert download.status_code == 200
        assert download.content == original


@pytest.mark.parametrize("fault", [
    "missing", "invalid_csv", "oversized_csv", "broken_raster", "wrong_layer", "wrong_geometry", "symlink",
])
def test_invalid_files_block_the_whole_batch(completed, fault):
    database, workdir, plan, results = completed
    code, role = "OUTPUT_INVALID", "summary"
    if fault == "missing":
        (workdir / "intermediate/scores.csv").unlink()
        code = "OUTPUT_MISSING"
    elif fault == "invalid_csv":
        (workdir / "intermediate/scores.csv").write_text('road,count\n"unclosed\n', encoding="utf-8")
    elif fault == "oversized_csv":
        field = "x" * (csv.field_size_limit() + 1)
        (workdir / "intermediate/scores.csv").write_text(f"road,count\n{field},1\n", encoding="utf-8")
    elif fault == "broken_raster":
        (workdir / "intermediate/grid.tif").write_bytes(b"not a TIFF")
        role = "elevation"
    elif fault == "wrong_layer":
        role = "roads"
        output = results["prepare"].outputs["lines"].model_copy(update={"layer": "targets"})
        results["prepare"] = results["prepare"].model_copy(update={"outputs": {"lines": output}})
    elif fault == "wrong_geometry":
        role = "roads"
        path = workdir / "intermediate/roads.gpkg"
        path.unlink()
        pyogrio.write_dataframe(pyogrio.read_dataframe(VECTOR, layer="targets"), path, layer="roads")
    else:
        path = workdir / "intermediate/scores.csv"
        path.unlink()
        path.symlink_to(VECTOR)

    with pytest.raises(ArtifactRegistrationError) as error:
        prepare_artifacts("run-1", plan, workdir, results)

    assert error.value.code == code and error.value.field.startswith(f"artifacts.{role}")
    assert not (workdir / "outputs/manifest.json").exists()
    with open_database(database) as connection:
        assert list_artifacts(connection, "run-1") == ()
        assert get_run(connection, "run-1").execution_status == "RUNNING"


def test_state_change_between_prepare_and_insert_is_rejected(completed):
    database, workdir, _, _ = completed
    prepared = _prepare(completed)
    (workdir / "intermediate/scores.csv").write_text("road,count\nroad-main,2\n", encoding="utf-8")
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ArtifactRegistrationError) as error:
            insert_artifacts(connection, prepared)
        assert error.value.code == "OUTPUT_INVALID"
        assert list_artifacts(connection, "run-1") == ()


def test_forged_result_and_different_snapshot_are_rejected(completed):
    database, workdir, plan, results = completed
    altered = {**results, "prepare": results["prepare"].model_copy(update={"environment": {"fake": "1"}})}
    changed_plan = plan.model_copy(update={"goal": plan.goal.model_copy(update={"summary": "Different snapshot"})})
    different_json_type = plan.model_copy(update={"critical_options": {"audit": True}})
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        for prepared in (
            prepare_artifacts("run-1", plan, workdir, altered),
            prepare_artifacts("run-1", changed_plan, workdir, results),
            prepare_artifacts("run-1", different_json_type, workdir, results),
        ):
            with pytest.raises(ArtifactRegistrationError):
                insert_artifacts(connection, prepared)
            assert list_artifacts(connection, "run-1") == ()


def test_second_insert_failure_rolls_back_batch_within_outer_transaction(completed):
    database, _, _, _ = completed
    prepared = _prepare(completed)
    with open_database(database) as connection, connection:
        connection.execute("""
            CREATE TRIGGER reject_summary BEFORE INSERT ON artifact WHEN NEW.role = 'summary'
            BEGIN SELECT RAISE(ABORT, 'second record failed'); END
        """)
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ArtifactRegistrationError, match="second record failed") as error:
            insert_artifacts(connection, prepared)
        assert error.value.code == "STORAGE_FAILED"
        assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
        assert connection.in_transaction and list_artifacts(connection, "run-1") == ()
    with open_database(database) as connection:
        assert list_artifacts(connection, "run-1") == ()


def test_registered_roles_cannot_be_overwritten(completed):
    database, _, _, _ = completed
    prepared = _prepare(completed)
    with open_database(database) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        insert_artifacts(connection, prepared)
        records = list_artifacts(connection, "run-1")
        with pytest.raises(ArtifactRegistrationError):
            insert_artifacts(connection, _prepare(completed))
        assert list_artifacts(connection, "run-1") == records
