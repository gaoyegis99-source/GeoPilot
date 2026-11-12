import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pyogrio
import pytest

from geopilot.data.assets import InputChangedError
from geopilot.data.fingerprint import fingerprint_file
from geopilot.persistence.schema import open_database
from geopilot.plans.compiler import PlanCompilationError
from geopilot.plans.service import confirm_plan
from geopilot.runs.service import create_run


def _confirm(project):
    db, _, _, plan, catalog, registry = project
    return confirm_plan(db, plan.id, plan.version, catalog=catalog, registry=registry, registered_rules={"output_exists"})


def _submit(project):
    db, _, workspace, plan, _, _ = project
    return create_run(db, plan.id, plan.version, workspace_root=workspace)


def _runs(database):
    with open_database(database) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM run ORDER BY created_at, id")]


def test_draft_and_unknown_version_do_not_prepare_run(project):
    db, _, workspace, plan, _, _ = project
    with pytest.raises(PlanCompilationError) as error:
        _submit(project)
    assert error.value.code == "PLAN_NOT_CONFIRMED"
    with pytest.raises(LookupError):
        create_run(db, plan.id, 2, workspace_root=workspace)
    assert not workspace.exists() and not _runs(db)


def test_create_run_publishes_exact_snapshot_and_fresh_directory(project):
    db, source, _, _, _, _ = project
    confirmed = _confirm(project)
    source_digest = fingerprint_file(source).digest
    run_id = _submit(project)
    row, = _runs(db)
    assert (row["id"], row["plan_id"], row["plan_version"]) == (run_id, confirmed.id, confirmed.version)
    assert (row["execution_status"], row["check_status"]) == ("NOT_STARTED", "NOT_CHECKED")
    workdir = Path(row["workdir"])
    document = json.loads((workdir / "request.json").read_text(encoding="utf-8"))
    assert document["run_id"] == run_id and document["plan"] == confirmed.model_dump(mode="json")
    reference = document["asset_files"]["lines"]
    assert reference["layer"] == "roads"
    assert fingerprint_file(workdir / reference["path"]).digest == source_digest
    assert not list((workdir / "steps").iterdir())
    assert not list((workdir / "outputs").iterdir())
    assert _submit(project) == run_id
    with open_database(db) as connection, connection:
        connection.execute("UPDATE run SET execution_status='SUCCEEDED', check_status='CHECKING' WHERE id=?", (run_id,))
    assert _submit(project) == run_id
    with open_database(db) as connection, connection:
        connection.execute("UPDATE run SET check_status='PASSED' WHERE id=?", (run_id,))
    second = _submit(project)
    assert second != run_id
    assert len({entry["workdir"] for entry in _runs(db)}) == 2
    assert fingerprint_file(source).digest == source_digest


def test_source_changed_after_confirmation_is_rejected(project):
    db, source, workspace, _, _, _ = project
    _confirm(project)
    row = pyogrio.read_dataframe(source, layer="roads", max_features=1)
    pyogrio.write_dataframe(row, source, layer="roads", append=True)
    with pytest.raises(InputChangedError):
        _submit(project)
    assert not _runs(db) and not list(workspace.glob("tasks/*/runs/*"))


def test_concurrent_submissions_publish_only_one_run(project):
    db, _, workspace, _, _, _ = project
    _confirm(project)
    start = Barrier(2, timeout=10)

    def submit():
        start.wait()
        return _submit(project)

    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(submit) for _ in range(2)]
        ids = [call.result(timeout=20) for call in calls]
    assert ids[0] == ids[1]
    assert len(_runs(db)) == len(list(workspace.glob("tasks/*/runs/*"))) == 1


def test_database_failure_cleans_unpublished_directory(project):
    db, _, workspace, _, _, _ = project
    _confirm(project)
    with open_database(db) as connection, connection:
        connection.execute("""
            CREATE TRIGGER reject_run BEFORE INSERT ON run
            BEGIN SELECT RAISE(ABORT, 'test run insert rejection'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="test run insert rejection"):
        _submit(project)
    assert not _runs(db) and not list(workspace.glob("tasks/*/runs/*"))
