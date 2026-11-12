import json
from dataclasses import dataclass
from pathlib import Path
from runpy import run_path

import pyogrio
import pytest
import yaml
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.application.plan_runner import run_confirmed_plan
from geopilot.application.task_context import build_task_context
from geopilot.artifacts.service import get_artifact, list_artifacts
from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.postgis_import import PostGISSource, import_postgis_snapshot, read_postgis_snapshot_origin, refresh_postgis_snapshot
from geopilot.data.resolver import resolve_asset
from geopilot.domain.data_asset import SourceType
from geopilot.domain.task import TaskGoal
from geopilot.methods.loader import MethodCatalog
from geopilot.persistence.run_repository import get_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, load_plan, save_draft
from geopilot.plans.versioning import revise_plan
from geopilot.scripts.models import ScriptSnapshot
from geopilot.scripts.registration import RegistrationCandidate, RegistrationError, validate_manual_registration
from geopilot.scripts.runner import run_script_snapshot
from geopilot.scripts.snapshot import create_script_snapshot, save_script_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[3]
RULES = ("output_exists",)
ROLE = "filtered_points"
SELECTED = "outputs/selected.gpkg"
COLLIDING_RUN_ID = "extension-same-id"


@pytest.fixture(scope="module")
def script_and_template():
    sample = run_path(str(ROOT / "tests/scripts/test_manual_registration.py"))
    return sample["SCRIPT"], sample["TEMPLATE"]


@pytest.fixture
def project(tmp_path):
    database = tmp_path / "catalog.sqlite"
    initialize_database(database)
    return database, tmp_path / "workspace"


@dataclass(frozen=True)
class Trial:
    snapshot: ScriptSnapshot
    candidate: RegistrationCandidate
    workdir: Path


def _prepare_trial(project, asset, script, template, run_id, *, feature="building-west", crs=32650, xy=(500750, 4401500)):
    database, workspace = project
    draft = save_script_draft(
        workspace, "extension-point-filter", script_text=script,
        inputs={"source": asset.ref}, parameters={"feature_id": feature},
    )
    snapshot = create_script_snapshot(database, workspace, draft.id, draft.version, script_run_id=run_id)
    document = yaml.safe_load(template.read_text(encoding="utf-8"))
    document["reference"] = {
        "script_run_id": run_id, "script_sha256": snapshot.script_sha256,
        "inputs": {"source": asset.ref.model_dump(mode="json")}, "parameters": {"feature_id": feature},
        "basis": "P0-05 source.json coordinates" if crs == 32650 else "Independent PostGIS fixture coordinates",
        "outputs": {"selected": {
            "path": SELECTED, "layer": "selected", "crs": f"EPSG:{crs}",
            "features": {feature: f"POINT ({xy[0]} {xy[1]})"},
        }},
    }
    return Trial(snapshot, RegistrationCandidate.model_validate(document), workspace / "scripts" / run_id)


def _assert_point(path, layer, crs, feature, xy):
    frame = pyogrio.read_dataframe(path, layer=layer)
    assert frame.crs.to_epsg() == crs
    assert frame.source_feature_id.tolist() == [feature]
    point = frame.geometry.item()
    assert (point.x, point.y) == xy


def _verify_successful_trial(project, trial):
    database, workspace = project
    result = run_script_snapshot(workspace, trial.snapshot.script_run_id)
    assert (result.status, result.exit_code) == ("SUCCEEDED", 0)
    assert [item.path for item in result.outputs] == [SELECTED]
    evidence = validate_manual_registration(database, workspace, trial.candidate, registered_rules=RULES)
    assert (evidence.script_run_id, evidence.script_version, evidence.script_sha256) == (
        trial.snapshot.script_run_id, trial.snapshot.version, trial.snapshot.script_sha256,
    )
    assert evidence.tool == trial.candidate.tool.ref and evidence.method == trial.candidate.method.ref
    assert evidence.output_fingerprints == {"selected": fingerprint_file(trial.workdir / SELECTED).digest}
    return evidence


def _registry(candidate):
    registry = ToolRegistry(python_implementations=[candidate.tool.implementation])
    registry.register(candidate.tool)
    return registry


def _confirm(project, trial, asset, registry, *, previous=None):
    database, _ = project
    method = trial.candidate.method
    context = build_task_context(
        database, task_id="extension-task", goal=TaskGoal(summary="Manually reviewed point selection"),
        method=method, selected={"points": asset.ref}, registry=registry,
    )
    if previous is None:
        plan = build_draft(context, method, plan_id="extension-plan", options=trial.snapshot.parameters)
        save_draft(database, plan, context=context)
    else:
        plan = revise_plan(database, previous.id, previous.version, context=context, method=method)
    assert not plan.issues, plan.issues
    return confirm_plan(
        database, plan.id, plan.version, catalog=MethodCatalog([method], ()), registry=registry, registered_rules=RULES,
    )


def _formal_run(project, trial, plan, registry, run_id):
    database, workspace = project
    result = run_confirmed_plan(
        database, plan.id, plan.version, workspace_root=workspace, run_id=run_id,
        registry=registry, input_preflight=preflight_step_inputs,
        python_implementations={trial.candidate.tool.implementation: trial.workdir / trial.snapshot.script_path},
    )
    assert (result.execution_status, result.check_status) == ("SUCCEEDED", "PASSED"), result
    assert (result.steps["filter"].status, result.steps["filter"].exit_code) == ("SUCCEEDED", 0)
    with open_database(database, read_only=True) as connection:
        artifact = get_artifact(connection, run_id, ROLE)
    assert (artifact.source.plan_id, artifact.source.plan_version) == (plan.id, plan.version)
    assert artifact.source.inputs == plan.inputs
    assert artifact.fingerprint == fingerprint_file(result.workdir / artifact.uri).digest
    return result, artifact


def test_failed_trial_cannot_replace_a_formal_run_or_supply_registration_evidence(project, script_and_template):
    database, workspace = project
    script, template = script_and_template
    asset = register_asset(database, ROOT / "tests/fixtures/vector/road-buffer.gpkg", layer="targets")
    reviewed = _prepare_trial(project, asset, script, template, "reviewed-trial")
    _verify_successful_trial(project, reviewed)
    registry = _registry(reviewed.candidate)
    plan = _confirm(project, reviewed, asset, registry)
    formal, artifact = _formal_run(project, reviewed, plan, registry, COLLIDING_RUN_ID)
    _assert_point(formal.workdir / artifact.uri, artifact.layer, 32650, "building-west", (500750, 4401500))
    original_files = {path: path.read_bytes() for path in formal.workdir.rglob("*") if path.is_file()}
    with open_database(database, read_only=True) as connection:
        original_run = get_run(connection, COLLIDING_RUN_ID)
        original_artifacts = list_artifacts(connection, COLLIDING_RUN_ID)

    failed = _prepare_trial(project, asset, script + "\nraise SystemExit(7)\n", template, COLLIDING_RUN_ID)
    failure = run_script_snapshot(workspace, COLLIDING_RUN_ID)
    assert (failure.status, failure.exit_code, failure.outputs) == ("FAILED", 7, ())
    _assert_point(failed.workdir / SELECTED, "selected", 32650, "building-west", (500750, 4401500))
    assert failed.workdir == workspace / "scripts" / COLLIDING_RUN_ID
    assert formal.workdir == workspace / "tasks" / plan.task_id / "runs" / COLLIDING_RUN_ID
    with pytest.raises(RegistrationError) as error:
        validate_manual_registration(database, workspace, failed.candidate, registered_rules=RULES)
    assert (error.value.code, error.value.field) == ("SCRIPT_RUN_INVALID", "reference.script_run_id")

    with TestClient(create_app(database, workspace_root=workspace)) as client:
        endpoint = f"/api/script-runs/{COLLIDING_RUN_ID}"
        detail = client.get(endpoint)
        assert detail.status_code == 200
        assert (detail.json()["status"], detail.json()["exit_code"], detail.json()["outputs"]) == ("FAILED", 7, [])
        log = client.get(f"{endpoint}/log")
        assert log.status_code == 200 and "exit_code: 7" in log.text
        assert client.get(f"{endpoint}/outputs/0/file").status_code == 404
        state = client.get(f"/api/runs/{COLLIDING_RUN_ID}")
        assert state.status_code == 200
        assert (state.json()["execution_status"], state.json()["check_status"]) == ("SUCCEEDED", "PASSED")
        download = client.get(f"/api/artifacts/{artifact.id}/file")
        assert download.status_code == 200 and download.content == original_files[formal.workdir / artifact.uri]

    assert original_files == {path: path.read_bytes() for path in formal.workdir.rglob("*") if path.is_file()}
    with open_database(database, read_only=True) as connection:
        assert get_run(connection, COLLIDING_RUN_ID) == original_run
        assert list_artifacts(connection, COLLIDING_RUN_ID) == original_artifacts
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM artifact").fetchone()[0] == 1


def test_postgis_snapshot_versions_replay_after_disconnect_through_trial_and_reviewed_method(project, script_and_template, tmp_path):
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import make_conninfo

    database, workspace = project
    script, template = script_and_template
    isolated_postgis = run_path(str(ROOT / "tests/data_adapters/test_postgis_snapshot.py"))["isolated_postgis"]
    source = PostGISSource(
        connection_alias="extension-fixture", schema="Survey 2024", table="Extension points",
        fields=("source_feature_id",),
    )
    old_xy, new_xy = (10, 30), (11.25, 30.75)
    with isolated_postgis() as (reader, password, socket, engine):
        print(f"PostgreSQL {engine[0]}, PostGIS {engine[1]}")
        admin = make_conninfo(
            host=str(socket), dbname="postgres", user="postgres",
            password=(socket.parent / "password").read_text(encoding="utf-8"),
        )
        with psycopg.connect(admin) as connection:
            connection.execute('''
                CREATE VIEW "Survey 2024"."Extension points" AS
                SELECT feature_id::text AS source_feature_id, geom FROM "Survey 2024".plots WHERE feature_id IN (1, 2)
            ''')
            connection.execute('GRANT SELECT ON "Survey 2024"."Extension points" TO snapshot_reader')
        old = import_postgis_snapshot(database, tmp_path / "postgis-v1.gpkg", conninfo=reader, source=source, layer="points")
        old_trial = _prepare_trial(project, old, script, template, "postgis-trial-v1", feature="1", crs=4326, xy=old_xy)
        original = old.source.path.read_bytes()
        with psycopg.connect(admin) as connection:
            moved = connection.execute('''
                UPDATE "Survey 2024".plots SET geom = ST_SetSRID(ST_Point(%s,%s),4326) WHERE feature_id = 1
            ''', new_xy)
            assert moved.rowcount == 1
        new = refresh_postgis_snapshot(database, tmp_path / "postgis-v2.gpkg", conninfo=reader, source=source, layer="points")
        assert old.id == new.id and (old.version, new.version) == (1, 2)
        assert old.source_type is new.source_type is SourceType.POSTGIS_SNAPSHOT
        assert old.fingerprint != new.fingerprint and old.source.path.read_bytes() == original
        old_origin = read_postgis_snapshot_origin(old.source.path)
        new_origin = read_postgis_snapshot_origin(new.source.path)
        assert old_origin.source == new_origin.source == source
        assert old_origin.srid == new_origin.srid == 4326
        assert old_origin.exported_at < new_origin.exported_at

    assert not socket.exists()
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(reader, connect_timeout=2)
    new_trial = _prepare_trial(project, new, script, template, "postgis-trial-v2", feature="1", crs=4326, xy=new_xy)
    assert (old_trial.snapshot.version, new_trial.snapshot.version) == (1, 2)
    assert old_trial.snapshot.script_sha256 == new_trial.snapshot.script_sha256
    registry = _registry(old_trial.candidate)
    previous = None
    for trial, asset, expected in ((old_trial, old, old_xy), (new_trial, new, new_xy)):
        assert resolve_asset(database, asset.ref) == asset
        assert trial.snapshot.inputs == {"source": asset.ref}
        staged = trial.workdir / trial.snapshot.asset_files["source"].path
        assert fingerprint_file(staged).digest == asset.fingerprint
        _verify_successful_trial(project, trial)
        _assert_point(trial.workdir / SELECTED, "selected", 4326, "1", expected)

        plan = _confirm(project, trial, asset, registry, previous=previous)
        assert (plan.version, plan.inputs["points"]) == (asset.version, asset.ref)
        formal, artifact = _formal_run(project, old_trial, plan, registry, f"postgis-offline-v{asset.version}")
        _assert_point(formal.workdir / artifact.uri, artifact.layer, 4326, "1", expected)
        request_file = formal.workdir / "request.json"
        request = json.loads(request_file.read_text(encoding="utf-8"))
        formal_input = formal.workdir / request["asset_files"]["points"]["path"]
        assert fingerprint_file(formal_input).digest == asset.fingerprint
        assert artifact.source.inputs["points"] == asset.ref
        for persisted in (asset.source.path, staged, trial.workdir / "snapshot/request.json", request_file):
            content = persisted.read_bytes()
            if any(secret.encode() in content for secret in (reader, password, str(socket))):
                pytest.fail("PostGIS connection details leaked into local snapshot or run evidence")
        previous = plan

    assert load_plan(database, previous.id, 1).inputs["points"] == old.ref
    assert load_plan(database, previous.id, 2).inputs["points"] == new.ref
    assert old.source.path.read_bytes() == original
    catalog_bytes = database.read_bytes()
    if any(secret.encode() in catalog_bytes for secret in (reader, password, str(socket))):
        pytest.fail("PostGIS connection details leaked into the local catalogue")
    with open_database(database, read_only=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM artifact").fetchone()[0] == 2
