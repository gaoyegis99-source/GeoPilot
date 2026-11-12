import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import rasterio

from geopilot.data.assets import register_asset
from geopilot.persistence.schema import initialize_database, open_database


RASTER = Path(__file__).resolve().parents[1] / "fixtures" / "raster" / "two-band.tif"
CREATED_AT = "2026-11-11T00:00:00Z"


def make_source(tmp_path):
    database = tmp_path / "metadata.sqlite3"
    initialize_database(database)
    source = tmp_path / "input.tif"
    shutil.copyfile(RASTER, source)
    return database, source


def change_pixel(path):
    with rasterio.open(path, "r+") as dataset:
        band = dataset.read(1)
        band[0, 0] = 999
        dataset.write(band, 1)


def test_change_appends_version_and_preserves_confirmed_plan_reference(tmp_path):
    database, source = make_source(tmp_path)
    initial = register_asset(database, source)
    plan_snapshot = json.dumps({"inputs": {"source": initial.ref.model_dump(mode="json")}})
    with open_database(database) as connection, connection:
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES ('task-1', '{}', ?)", (CREATED_AT,))
        connection.execute(
            "INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at) "
            "VALUES ('plan-1', 1, 'task-1', 'confirmed', ?, ?)",
            (plan_snapshot, CREATED_AT),
        )

    change_pixel(source)
    updated = register_asset(database, source)
    assert (updated.id, updated.version) == (initial.id, 2)
    assert updated.fingerprint != initial.fingerprint
    assert updated.metadata == initial.metadata

    source_stat = source.stat()
    os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 2_000_000_000))
    assert register_asset(database, source) == updated

    shutil.copyfile(RASTER, source)
    restored = register_asset(database, source)
    assert (restored.id, restored.version, restored.fingerprint) == (initial.id, 3, initial.fingerprint)

    with open_database(database) as connection:
        rows = connection.execute("SELECT version, fingerprint FROM data_asset ORDER BY version").fetchall()
        assert [tuple(row) for row in rows] == [
            (1, initial.fingerprint), (2, updated.fingerprint), (3, restored.fingerprint),
        ]
        assert connection.execute("SELECT plan_json FROM analysis_plan").fetchone()[0] == plan_snapshot


def test_concurrent_registration_of_a_new_version_inserts_once(tmp_path):
    database, source = make_source(tmp_path)
    initial = register_asset(database, source)
    change_pixel(source)

    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(register_asset, database, source) for _ in range(4)]
        versions = [future.result() for future in futures]

    assert {asset.id for asset in versions} == {initial.id}
    assert {asset.version for asset in versions} == {2}
    assert len({asset.fingerprint for asset in versions}) == 1
    with open_database(database) as connection:
        assert [row[0] for row in connection.execute("SELECT version FROM data_asset ORDER BY version")] == [1, 2]
