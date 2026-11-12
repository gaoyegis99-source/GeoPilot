import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import geopandas as gpd
import pyogrio
import pytest
import yaml
from shapely.geometry import box

from geopilot.application.plan_runner import run_confirmed_plan
from geopilot.application.task_context import build_task_context
from geopilot.artifacts.service import get_artifact
from geopilot.data.assets import register_asset
from geopilot.data.errors import SourceReadError
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.postgis_import import import_postgis_snapshot, read_postgis_snapshot_origin, refresh_postgis_snapshot
from geopilot.data.resolver import resolve_asset
from geopilot.domain.data_asset import SourceType
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, load_plan, save_draft
from geopilot.plans.versioning import revise_plan
from geopilot.tools.registry import ToolRegistry

from test_postgis_snapshot import _source, isolated_postgis, postgis


ROOT = Path(__file__).resolve().parents[2]
GOAL = TaskGoal(summary="Select and clip points from the chosen PostGIS snapshot")
WORKERS = {
    "geopilot.select_spatial_relation": ROOT / "src/geopilot/gis/tools/select_spatial_relation.py",
    "geopilot.clip_vector_to_extent": ROOT / "src/geopilot/gis/tools/clip_vector_to_extent.py",
}


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "catalog.sqlite"
    initialize_database(path)
    return path


def _admin_connection(socket):
    import psycopg
    from psycopg.conninfo import make_conninfo

    # The isolated fixture owns this one-use local initialization secret.
    secret = (socket.parent / "password").read_text(encoding="utf-8")
    conninfo = make_conninfo(host=str(socket), dbname="postgres", user="postgres", password=secret)
    return psycopg.connect(conninfo)


def _catalog_runtime():
    catalog = load_method_catalog(ROOT / "config/methods")
    method = catalog.get("spatial-select-and-clip", 1)
    registry = ToolRegistry(python_implementations=WORKERS)
    for step in method.steps:
        path = ROOT / "config/tools" / f"{step.tool.id}.yaml"
        registry.register(ToolContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))))
    return catalog, method, registry


def _version_rows(database):
    with open_database(database, read_only=True) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM data_asset ORDER BY id, version")]


def _snapshot_rows(path, layer):
    frame = pyogrio.read_dataframe(path, layer=layer).sort_values("feature_id")
    assert frame.crs.to_epsg() == 4326
    return [(feature_id, score, geometry.coords[0]) for feature_id, score, geometry in zip(
        frame["feature_id"], frame["score"], frame.geometry, strict=True,
    )]


def test_changed_postgis_data_appends_a_version_and_both_confirmed_plans_run_offline(database, tmp_path):
    catalog, method, registry = _catalog_runtime()
    area_file = tmp_path / "area.gpkg"
    pyogrio.write_dataframe(gpd.GeoDataFrame({"name": ["selection"]}, geometry=[box(9, 29, 13, 32)], crs=4326),
                           area_file, layer="area")
    area = register_asset(database, area_file, layer="area")
    source = _source(table="Versioned plots", fields=("source_feature_id", "feature_id", "score"))
    first_path, second_path = (tmp_path / name for name in ("plots-v1.gpkg", "plots-v2.gpkg"))
    confirmation = {"catalog": catalog, "registry": registry, "registered_rules": {"output_exists"}}

    def context(asset):
        return build_task_context(
            database, task_id="postgis-task", goal=GOAL, method=method, registry=registry,
            selected={"targets": asset.ref, "relation_area": area.ref, "analysis_extent": area.ref},
        )

    with isolated_postgis() as (reader, reader_password, socket, engine):
        assert engine[0].startswith("16.") and engine[1].startswith("3.")
        with _admin_connection(socket) as admin:
            admin.execute('''
                CREATE VIEW "Survey 2024"."Versioned plots" AS
                SELECT plots.*, plots.feature_id::text AS source_feature_id FROM "Survey 2024".plots
            ''')
            admin.execute('GRANT SELECT ON "Survey 2024"."Versioned plots" TO snapshot_reader')
        first = import_postgis_snapshot(database, first_path, conninfo=reader, source=source, layer="selected")
        initial_context = context(first)
        draft = build_draft(initial_context, method, plan_id="postgis-plan", options={"spatial_relation": "within"})
        assert not draft.issues, draft.issues
        save_draft(database, draft, context=initial_context)
        old_plan = confirm_plan(database, draft.id, draft.version, **confirmation)
        original_rows = _snapshot_rows(first_path, first.source.layer)
        original_bytes = first_path.read_bytes()
        assert original_rows == [(1, 3.25, (10.0, 30.0)), (2, 4.5, (12.0, 31.0))]

        with _admin_connection(socket) as admin:
            moved = admin.execute('''
                UPDATE "Survey 2024".plots SET score = %s, geom = ST_SetSRID(ST_Point(%s,%s),4326)
                WHERE feature_id = 1
            ''', (7.5, 14.0, 34.0))
            assert moved.rowcount == 1
            admin.execute('''
                INSERT INTO "Survey 2024".plots VALUES (%s,%s,%s,%s,%s,%s,ST_SetSRID(ST_Point(%s,%s),4326))
            ''', (5, "O'Brien", None, 2.25, True, 5, 10.5, 30.5))
        second = refresh_postgis_snapshot(database, second_path, conninfo=reader, source=source, layer="selected")
        assert first.id == second.id and (first.version, second.version) == (1, 2)
        assert first.source_type is second.source_type is SourceType.POSTGIS_SNAPSHOT
        assert first.source.path == first_path and second.source.path == second_path
        assert first.metadata.feature_count == 2 and second.metadata.feature_count == 3
        assert first.fingerprint != second.fingerprint
        assert first_path.read_bytes() == original_bytes
        assert _snapshot_rows(second_path, second.source.layer) == [
            (1, 7.5, (14.0, 34.0)), (2, 4.5, (12.0, 31.0)), (5, 2.25, (10.5, 30.5)),
        ]
        old_origin, new_origin = map(read_postgis_snapshot_origin, (first_path, second_path))
        assert old_origin.source == new_origin.source == source and old_origin.exported_at < new_origin.exported_at
        assert old_origin.srid == new_origin.srid == 4326
        revised = revise_plan(database, old_plan.id, old_plan.version, context=context(second), method=method)
        new_plan = confirm_plan(database, revised.id, revised.version, **confirmation)
        assert old_plan.id == new_plan.id and (old_plan.version, new_plan.version) == (1, 2)
        assert load_plan(database, old_plan.id, 1) == old_plan
        assert load_plan(database, new_plan.id, 2) == new_plan
        assert old_plan.inputs["targets"] == first.ref and new_plan.inputs["targets"] == second.ref
        for evidence in (database.read_bytes(), original_bytes, second_path.read_bytes()):
            if any(secret.encode() in evidence for secret in (reader, reader_password, str(socket))):
                pytest.fail("PostGIS credentials or socket leaked into local snapshot evidence")

    assert not socket.exists()
    for plan, asset, expected_ids in ((old_plan, first, [1, 2]), (new_plan, second, [2, 5])):
        assert resolve_asset(database, plan.inputs["targets"]) == asset
        assert fingerprint_file(asset.source.path).digest == asset.fingerprint
        run = run_confirmed_plan(
            database, plan.id, plan.version, workspace_root=tmp_path / "workspace", run_id=f"offline-v{plan.version}",
            registry=registry, python_implementations=WORKERS,
        )
        assert (run.execution_status, run.check_status) == ("SUCCEEDED", "PASSED"), run
        assert [step.exit_code for step in run.steps.values()] == [0, 0]
        output = run.artifacts["clipped_targets"]
        frame = pyogrio.read_dataframe(run.workdir / output.path, layer=output.layer)
        assert frame.crs.to_epsg() == 4326 and sorted(frame["feature_id"]) == expected_ids
        snapshot = json.loads((run.workdir / "request.json").read_text(encoding="utf-8"))
        staged = run.workdir / snapshot["asset_files"]["targets"]["path"]
        assert fingerprint_file(staged).digest == asset.fingerprint
        with open_database(database, read_only=True) as connection:
            artifact = get_artifact(connection, run.run_id, "clipped_targets")
            assert (artifact.source.plan_id, artifact.source.plan_version) == (plan.id, plan.version)
            assert artifact.source.inputs["targets"] == asset.ref

    assert first_path.read_bytes() == original_bytes
    versions = [row for row in _version_rows(database) if row["source_key"] == source.source_key]
    assert [(row["id"], row["version"]) for row in versions] == [(first.id, 1), (first.id, 2)]


def test_explicit_concurrent_refreshes_each_commit_a_distinct_version(database, tmp_path, postgis):
    conninfo, _, _, _ = postgis
    source = _source()
    first = import_postgis_snapshot(database, tmp_path / "first.gpkg", conninfo=conninfo, source=source)
    gate = Barrier(3, timeout=10)

    def refresh(index):
        gate.wait()
        return refresh_postgis_snapshot(database, tmp_path / f"candidate-{index}.gpkg", conninfo=conninfo, source=source)

    with ThreadPoolExecutor(max_workers=3) as pool:
        new = list(pool.map(refresh, range(3)))
    assert sorted(asset.version for asset in new) == [2, 3, 4]
    assert {asset.id for asset in new} == {first.id}
    assert len({asset.source.path for asset in (first, *new)}) == 4
    assert len({read_postgis_snapshot_origin(asset.source.path).exported_at for asset in (first, *new)}) == 4
    for asset in (first, *new):
        assert resolve_asset(database, asset.ref) == asset
        assert _snapshot_rows(asset.source.path, asset.source.layer) == [(1, 3.25, (10.0, 30.0)), (2, 4.5, (12.0, 31.0))]
    assert [(row["id"], row["version"]) for row in _version_rows(database)] == [(first.id, n) for n in range(1, 5)]
    assert not list(tmp_path.glob(".postgis-export-*"))


def test_concurrent_same_destination_never_replaces_a_snapshot(database, tmp_path, postgis):
    conninfo, _, _, _ = postgis
    source = _source()
    first = import_postgis_snapshot(database, tmp_path / "first.gpkg", conninfo=conninfo, source=source)
    destination = tmp_path / "new.gpkg"
    gate = Barrier(2, timeout=10)

    def refresh(_):
        gate.wait()
        try:
            return refresh_postgis_snapshot(database, destination, conninfo=conninfo, source=source)
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(refresh, range(2)))
    second, = [asset for asset in outcomes if asset is not None]
    assert (second.id, second.version) == (first.id, 2)
    assert resolve_asset(database, second.ref) == second
    assert resolve_asset(database, first.ref) == first
    assert not list(tmp_path.glob(".postgis-export-*"))


def test_refresh_rejects_registered_destination_even_when_old_file_is_moved(database, tmp_path, postgis):
    conninfo, _, _, _ = postgis
    source = _source()
    first_path = tmp_path / "first.gpkg"
    first = import_postgis_snapshot(database, first_path, conninfo=conninfo, source=source)
    before = _version_rows(database)
    with pytest.raises(FileExistsError):
        refresh_postgis_snapshot(database, first_path, conninfo=conninfo, source=source)
    moved = first_path.rename(tmp_path / "preserved-v1.gpkg")
    try:
        with pytest.raises(FileExistsError):
            refresh_postgis_snapshot(database, first_path, conninfo=conninfo, source=source)
        assert not first_path.exists() and fingerprint_file(moved).digest == first.fingerprint
        assert _version_rows(database) == before
    finally:
        moved.rename(first_path)
    assert resolve_asset(database, first.ref) == first


def test_refresh_requires_existing_exact_logical_selection(database, tmp_path, postgis):
    conninfo, _, _, _ = postgis
    original = _source()
    first = import_postgis_snapshot(database, tmp_path / "first.gpkg", conninfo=conninfo, source=original)
    changed_selection = _source(filters={"flag": True})
    with pytest.raises(SourceReadError) as failure:
        refresh_postgis_snapshot(database, tmp_path / "new-selection.gpkg", conninfo=conninfo, source=changed_selection)
    assert failure.value.field == "source.selection"
    assert not (tmp_path / "new-selection.gpkg").exists()
    assert [row["id"] for row in _version_rows(database)] == [first.id]


@pytest.mark.parametrize("action", ["ABORT, 'fixture refusal'", "IGNORE"])
def test_sqlite_rejection_rolls_back_candidate_and_keeps_prior_snapshot(database, tmp_path, postgis, action):
    conninfo, _, _, _ = postgis
    source = _source()
    first = import_postgis_snapshot(database, tmp_path / "first.gpkg", conninfo=conninfo, source=source)
    before = _version_rows(database)
    with open_database(database) as connection, connection:
        connection.execute(f"""
            CREATE TRIGGER refuse_refresh BEFORE INSERT ON data_asset
            WHEN NEW.version > 1 BEGIN SELECT RAISE({action}); END
        """)
    candidate = tmp_path / "unpublished.gpkg"
    with pytest.raises(sqlite3.IntegrityError):
        refresh_postgis_snapshot(database, candidate, conninfo=conninfo, source=source)
    assert not candidate.exists() and not list(tmp_path.glob(".postgis-export-*"))
    assert _version_rows(database) == before
    assert resolve_asset(database, first.ref) == first


def test_sqlite_commit_failure_withdraws_candidate_after_successful_insert(database, tmp_path, postgis):
    conninfo, _, _, _ = postgis
    source = _source()
    first = import_postgis_snapshot(database, tmp_path / "first.gpkg", conninfo=conninfo, source=source)
    before = _version_rows(database)
    with open_database(database) as connection, connection:
        connection.execute("CREATE TABLE fixture_parent (id INTEGER PRIMARY KEY)")
        connection.execute("""
            CREATE TABLE fixture_reference (
                parent INTEGER REFERENCES fixture_parent(id) DEFERRABLE INITIALLY DEFERRED
            )
        """)
        connection.execute("""
            CREATE TRIGGER fail_at_commit AFTER INSERT ON data_asset WHEN NEW.version > 1
            BEGIN INSERT INTO fixture_reference VALUES (42); END
        """)
    candidate = tmp_path / "uncommitted.gpkg"
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        refresh_postgis_snapshot(database, candidate, conninfo=conninfo, source=source)
    assert not candidate.exists() and not list(tmp_path.glob(".postgis-export-*"))
    assert _version_rows(database) == before
    assert resolve_asset(database, first.ref) == first
    with open_database(database, read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM fixture_reference").fetchone()[0] == 0
