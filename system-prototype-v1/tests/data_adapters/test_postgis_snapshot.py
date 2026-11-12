import json
import shutil
import sqlite3
import subprocess
import traceback
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

import pyogrio
import pytest
from shapely.geometry import box

from geopilot.application.run_inputs import stage_run_inputs
from geopilot.data.errors import SourceReadError
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.postgis_import import (
    PostGISSource,
    export_postgis_snapshot,
    import_postgis_snapshot,
    read_postgis_snapshot_origin,
)
from geopilot.data.resolver import resolve_asset
from geopilot.domain.data_asset import SourceType
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.persistence.workspace import create_run_workspace


IMAGE = "postgis/postgis:16-3.5"
SCHEMA = "Survey 2024"
TABLE = "Plot records"
ALIAS = "本机样本"
FIELDS = ("feature_id", "land use", "label", "score", "flag", "measure", "import_mode", "import_isolation")


@contextmanager
def isolated_postgis():
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    if shutil.which("docker") is None:
        pytest.skip("An isolated local PostGIS fixture requires Docker")
    available = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=10)
    if available.returncode:
        pytest.skip(f"The local Docker image {IMAGE} is unavailable")
    with TemporaryDirectory(prefix="gp-pg-", dir="/tmp") as directory:
        root = Path(directory)
        socket = root / "socket"
        socket.mkdir(mode=0o777)
        admin_password = uuid4().hex
        secret_file = root / "password"
        secret_file.write_text(admin_password, encoding="utf-8")
        secret_file.chmod(0o400)
        name = f"geopilot-postgis-{uuid4().hex}"
        launch = subprocess.run([
            "docker", "run", "--rm", "--detach", "--pull=never", "--network", "none", "--name", name,
            "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
            "--mount", f"type=bind,src={socket},dst=/var/run/postgresql",
            "--mount", f"type=bind,src={secret_file},dst=/run/postgis-fixture-password,readonly",
            "--env", "POSTGRES_PASSWORD_FILE=/run/postgis-fixture-password",
            "--env", "POSTGRES_INITDB_ARGS=--auth-local=scram-sha-256 --auth-host=scram-sha-256",
            IMAGE, "postgres", "-c", "listen_addresses=",
        ], capture_output=True, timeout=20)
        assert launch.returncode == 0, "The isolated local PostGIS container could not start"
        admin = make_conninfo(host=str(socket), dbname="postgres", user="postgres", password=admin_password)
        reader_password = f"geopilot-fixture-{uuid4().hex}"
        reader = make_conninfo(host=str(socket), dbname="postgres", user="snapshot_reader", password=reader_password)
        try:
            deadline = monotonic() + 35
            while True:
                try:
                    with psycopg.connect(admin, connect_timeout=2) as connection:
                        engine = connection.execute(
                            "SELECT current_setting('server_version'), PostGIS_Lib_Version(), "
                            "split_part(pg_read_file('postmaster.pid'), chr(10), 1)"
                        ).fetchone()
                        if engine[2] == "1":
                            break
                except psycopg.Error:
                    pass
                if monotonic() >= deadline:
                    pytest.fail("The isolated PostGIS fixture did not become ready")
                sleep(0.1)
            with psycopg.connect(admin) as connection:
                connection.execute('CREATE SCHEMA "Survey 2024"')
                connection.execute('''
                    CREATE TABLE "Survey 2024".plots (
                        feature_id bigint PRIMARY KEY, "land use" text, label text, score double precision,
                        flag boolean, measure bigint, geom geometry(Point, 4326)
                    )
                ''')
                with connection.cursor() as cursor:
                    cursor.executemany(
                        'INSERT INTO "Survey 2024".plots VALUES (%s,%s,%s,%s,%s,%s,ST_SetSRID(ST_Point(%s,%s),4326))',
                        [
                            (1, "O'Brien", None, 3.25, True, 9_007_199_254_740_993, 10.0, 30.0),
                            (2, "O'Brien", None, 4.5, True, None, 12.0, 31.0),
                            (3, "O'Brien", "tag", 1.0, True, 3, 15.0, 32.0),
                            (4, "grass", None, 2.0, True, 4, 16.0, 33.0),
                        ],
                    )
                connection.execute('''
                    CREATE VIEW "Survey 2024"."Plot records" AS
                    SELECT plots.*, current_setting('transaction_read_only') AS import_mode,
                           current_setting('transaction_isolation') AS import_isolation
                    FROM "Survey 2024".plots
                ''')
                connection.execute('CREATE TABLE "Survey 2024".mixed (id integer, geom geometry)')
                connection.execute('''
                    INSERT INTO "Survey 2024".mixed VALUES
                    (1, ST_SetSRID(ST_Point(1, 2), 4326)), (2, ST_SetSRID(ST_Point(3, 4), 3857))
                ''')
                connection.execute('CREATE TABLE "Survey 2024".unknown (id integer, geom geometry(Point))')
                connection.execute('INSERT INTO "Survey 2024".unknown VALUES (1, ST_Point(1, 2))')
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier("snapshot_reader"), sql.Literal(reader_password),
                ))
                connection.execute('GRANT USAGE ON SCHEMA "Survey 2024" TO snapshot_reader')
                connection.execute('GRANT SELECT ON ALL TABLES IN SCHEMA "Survey 2024" TO snapshot_reader')
                assert connection.execute(
                    "SELECT has_table_privilege('snapshot_reader', '\"Survey 2024\".plots', 'UPDATE')"
                ).fetchone() == (False,)
                assert connection.execute("SHOW listen_addresses").fetchone() == ("",)
            yield reader, reader_password, socket, engine[:2]
        finally:
            stopped = subprocess.run(["docker", "stop", "--time", "5", name], capture_output=True, timeout=15)
            if stopped.returncode:
                subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=15)


@pytest.fixture(scope="module")
def postgis():
    with isolated_postgis() as environment:
        yield environment


def _source(**changes):
    values = {
        "connection_alias": ALIAS, "schema": SCHEMA, "table": TABLE,
        "geometry_column": "geom", "fields": FIELDS,
        "filters": {"land use": "O'Brien", "flag": True, "label": None},
    }
    return PostGISSource(**{**values, **changes})


def test_import_real_filtered_named_layer_and_analyze_after_postgis_stops(tmp_path, capsys):
    database = tmp_path / "catalog.sqlite"
    destination = tmp_path / "filtered.gpkg"
    initialize_database(database)
    before = datetime.now(UTC)
    source = _source()
    with isolated_postgis() as (conninfo, password, socket, engine):
        assert engine[0].startswith("16.") and engine[1].startswith("3.")
        asset = import_postgis_snapshot(database, destination, conninfo=conninfo, source=source, layer="selected")
        assert asset.source_type is SourceType.POSTGIS_SNAPSHOT and asset.version == 1
        assert asset.source.path == destination and asset.source.layer == "selected"
        assert asset.fingerprint == fingerprint_file(destination).digest
        assert asset.metadata.crs == "EPSG:4326"
        assert asset.metadata.feature_count == 2 and asset.metadata.geometry_type == "Point"
        origin = read_postgis_snapshot_origin(destination)
        assert origin.source == source and origin.srid == 4326
        assert before <= origin.exported_at <= datetime.now(UTC) and origin.exported_at.tzinfo is not None
        with open_database(database, read_only=True) as catalog:
            row = catalog.execute("SELECT source_key, source_json, source_type FROM data_asset").fetchone()
        assert row["source_key"] == source.source_key and row["source_type"] == "postgis_snapshot"
        assert json.loads(row["source_json"])["layer"] == "selected"
        for evidence in (database.read_bytes(), destination.read_bytes(), asset.model_dump_json().encode()):
            assert password.encode() not in evidence and str(socket).encode() not in evidence
    captured = capsys.readouterr()
    assert password not in captured.out + captured.err
    assert not socket.exists()

    reopened = resolve_asset(database, asset.ref)
    assert reopened == asset
    run = create_run_workspace(tmp_path / "workspace", "snapshot-task", "offline-run")
    staged = stage_run_inputs(database, {"source": reopened.ref}, run.path)["source"]
    assert staged.layer == "selected"
    assert fingerprint_file(run.path / staged.path).digest == asset.fingerprint
    frame = pyogrio.read_dataframe(run.path / staged.path, layer=staged.layer).sort_values("feature_id")
    assert frame["feature_id"].tolist() == [1, 2]
    assert frame["land use"].tolist() == ["O'Brien", "O'Brien"]
    assert frame["label"].isna().all()
    assert frame["flag"].tolist() == [True, True]
    assert frame["score"].tolist() == [3.25, 4.5]
    assert frame["import_mode"].tolist() == ["on", "on"]
    assert frame["import_isolation"].tolist() == ["repeatable read", "repeatable read"]
    assert [point.coords[0] for point in frame.geometry] == [(10.0, 30.0), (12.0, 31.0)]
    assert frame.loc[frame.geometry.within(box(9, 29, 11, 31)), "feature_id"].tolist() == [1]
    with sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True) as snapshot:
        assert snapshot.execute(
            "SELECT reference_scope, table_name FROM gpkg_metadata_reference"
        ).fetchall() == [("table", "selected")]
        assert snapshot.execute('SELECT measure FROM selected ORDER BY feature_id').fetchall() == [
            (9_007_199_254_740_993,), (None,),
        ]
    assert not destination.with_suffix(".gpkg.aux.xml").exists()


def test_export_empty_selected_layer_preserves_declared_srid_and_type(tmp_path, postgis):
    conninfo, _, _, _ = postgis
    source = _source(filters={"feature_id": -1})
    destination = tmp_path / "empty.gpkg"
    snapshot = export_postgis_snapshot(destination, conninfo=conninfo, source=source, layer="empty_points")
    assert snapshot.path == destination and snapshot.layer == "empty_points"
    assert snapshot.origin == read_postgis_snapshot_origin(destination)
    assert snapshot.fingerprint == fingerprint_file(destination).digest
    info = pyogrio.read_info(destination, layer=snapshot.layer)
    assert (info["features"], info["geometry_type"], info["crs"]) == (0, "Point", "EPSG:4326")


def test_unknown_srid_is_recorded_and_mixed_srids_are_rejected(tmp_path, postgis):
    conninfo, _, _, _ = postgis
    database = tmp_path / "catalog.sqlite"
    initialize_database(database)
    asset = import_postgis_snapshot(
        database, tmp_path / "unknown.gpkg", conninfo=conninfo,
        source=_source(table="unknown", fields=None, filters={}),
    )
    assert asset.metadata.crs is None and "CRS_UNKNOWN" in asset.metadata.issues
    target = tmp_path / "mixed.gpkg"
    with pytest.raises(SourceReadError, match="SRID"):
        export_postgis_snapshot(target, conninfo=conninfo, source=_source(table="mixed", fields=None, filters={}))
    assert not target.exists()
    snapshot = export_postgis_snapshot(
        tmp_path / "projected.gpkg", conninfo=conninfo,
        source=_source(table="mixed", fields=None, filters={"id": 2}), layer="projected_points",
    )
    assert snapshot.origin.srid == 3857
    projected = pyogrio.read_dataframe(snapshot.path, layer=snapshot.layer)
    assert projected.crs.to_epsg() == 3857 and projected.geometry.iloc[0].coords[0] == (3.0, 4.0)


def test_bad_connection_and_filter_are_sanitized_and_leave_no_snapshot(tmp_path, postgis, capsys):
    from psycopg.conninfo import make_conninfo

    conninfo, password, socket, _ = postgis
    target = tmp_path / "unwritten.gpkg"
    wrong = f"synthetic-invalid-{uuid4().hex}"
    with pytest.raises(SourceReadError) as error:
        export_postgis_snapshot(target, conninfo=make_conninfo(conninfo, password=wrong), source=_source())
    assert error.value.field == "source.connection"
    trace = "".join(traceback.format_exception(error.value))
    out = capsys.readouterr()
    for forbidden in (conninfo, wrong, password, str(socket)):
        assert forbidden not in trace + out.out + out.err
    with pytest.raises(SourceReadError) as error:
        export_postgis_snapshot(target, conninfo=conninfo, source=_source(filters={"missing field": "value"}))
    assert error.value.field == "source.filters"
    assert not target.exists()


def test_output_collision_and_registration_failure_preserve_files_and_catalog(tmp_path, postgis):
    conninfo, _, _, _ = postgis
    database = tmp_path / "catalog.sqlite"
    initialize_database(database)
    used = tmp_path / "already.gpkg"
    used.write_bytes(b"independent existing file")
    with pytest.raises(FileExistsError):
        export_postgis_snapshot(used, conninfo=conninfo, source=_source())
    assert used.read_bytes() == b"independent existing file"

    first = import_postgis_snapshot(database, tmp_path / "first.gpkg", conninfo=conninfo, source=_source())
    duplicate = tmp_path / "duplicate.gpkg"
    with pytest.raises(SourceReadError) as error:
        import_postgis_snapshot(database, duplicate, conninfo=conninfo, source=_source())
    assert error.value.field == "source.selection" and not duplicate.exists()

    with open_database(database) as connection, connection:
        connection.execute('''
            CREATE TRIGGER refuse_registration BEFORE INSERT ON data_asset
            BEGIN SELECT RAISE(ABORT, 'fixture rejection'); END
        ''')
    rejected = tmp_path / "rejected.gpkg"
    with pytest.raises(sqlite3.IntegrityError):
        import_postgis_snapshot(database, rejected, conninfo=conninfo, source=_source(filters={"flag": True}))
    assert not rejected.exists()
    with open_database(database, read_only=True) as connection:
        assert tuple(connection.execute("SELECT id, version FROM data_asset").fetchone()) == (first.id, 1)
        assert connection.execute("SELECT count(*) FROM data_asset").fetchone()[0] == 1
