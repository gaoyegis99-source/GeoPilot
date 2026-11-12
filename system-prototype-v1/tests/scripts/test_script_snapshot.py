import os
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from shutil import copyfile
from stat import S_IMODE
from threading import Barrier

import pyogrio
import pytest

from geopilot.data.assets import InputChangedError, register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.scripts import snapshot
from geopilot.scripts.models import ScriptDraft, ScriptSnapshot
from geopilot.scripts.snapshot import create_script_snapshot, load_script_draft, load_script_snapshot, save_script_draft


SOURCE = Path(__file__).resolve().parents[1] / "fixtures/vector/road-buffer.gpkg"
SCRIPT_ID = "filter-buildings"


@pytest.fixture
def project(tmp_path):
    database = tmp_path / "project.sqlite"
    root = tmp_path / "workspace"
    source = tmp_path / "buildings.gpkg"
    copyfile(SOURCE, source)
    initialize_database(database)
    asset = register_asset(database, source, layer="targets")
    return database, root, source, asset


def test_exact_draft_versions_and_real_readonly_snapshots_preserve_formal_output(project):
    database, root, source, asset = project
    formal = create_run_workspace(root, "formal-task", "formal-run")
    formal_output = formal.path / "outputs/buildings.gpkg"
    copyfile(source, formal_output)
    original_digest = fingerprint_file(formal_output).digest

    original_text = '# 建筑物筛选\r\nprint("草稿")\r\n'
    inputs = {"targets": asset.ref, "same_layer": asset.ref}
    parameters = {"predicate": "within", "limit": 3, "distance": 0.0, "options": [False, None, {"unit": "meter"}]}
    draft = save_script_draft(root, SCRIPT_ID, script_text=original_text, inputs=inputs, parameters=parameters)
    first = create_script_snapshot(database, root, SCRIPT_ID, draft.version, script_run_id="script-run-first")
    edited = save_script_draft(root, SCRIPT_ID, script_text="if ", inputs={"targets": asset.ref}, parameters={"limit": 5})
    second = create_script_snapshot(database, root, SCRIPT_ID, edited.version)

    assert isinstance(draft, ScriptDraft) and isinstance(first, ScriptSnapshot)
    assert (draft.id, draft.version, edited.version) == (SCRIPT_ID, 1, 2)
    assert load_script_draft(root, SCRIPT_ID, 1) == draft
    assert load_script_draft(root, SCRIPT_ID, 2) == edited
    assert load_script_snapshot(root, first.script_run_id) == first
    assert load_script_snapshot(root, second.script_run_id) == second
    assert (first.script_id, first.version, first.inputs, first.parameters) == (SCRIPT_ID, 1, inputs, parameters)
    assert type(first.parameters["distance"]) is float
    assert (second.script_id, second.version, second.parameters) == (SCRIPT_ID, 2, {"limit": 5})
    assert first.script_run_id != second.script_run_id

    workdir = root / "scripts" / first.script_run_id
    assert first.script_path == "snapshot/script.py"
    script = workdir / first.script_path
    assert script.read_bytes() == original_text.encode("utf-8")
    assert first.script_sha256 == sha256(script.read_bytes()).hexdigest()
    assert S_IMODE(script.stat().st_mode) == S_IMODE((workdir / "snapshot/request.json").stat().st_mode) == 0o400
    assert S_IMODE(script.parent.stat().st_mode) == 0o500
    assert S_IMODE(workdir.stat().st_mode) == 0o700
    assert (workdir / "outputs").is_dir() and not list((workdir / "outputs").iterdir())
    staged = first.asset_files["targets"]
    assert staged == first.asset_files["same_layer"] and staged.layer == "targets"
    input_file = workdir / staged.path
    assert input_file != source and fingerprint_file(input_file).digest == asset.fingerprint
    points = pyogrio.read_dataframe(input_file, layer=staged.layer)
    reference = pyogrio.read_dataframe(source, layer="targets")
    assert points.crs.to_epsg() == 32650 and points.equals(reference)
    source.unlink()
    assert load_script_snapshot(root, first.script_run_id) == first
    assert pyogrio.read_dataframe(input_file, layer=staged.layer).equals(reference)
    assert fingerprint_file(formal_output).digest == original_digest
    with open_database(database, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM data_asset").fetchone()[0] == 1


def test_concurrent_draft_saves_publish_complete_distinct_versions(tmp_path):
    root = tmp_path / "workspace"
    gate = Barrier(6, timeout=10)

    def save(index):
        gate.wait()
        return save_script_draft(root, SCRIPT_ID, script_text=f"print({index})\n", inputs={}, parameters={"index": index})

    with ThreadPoolExecutor(max_workers=6) as pool:
        drafts = list(pool.map(save, range(6)))
    assert sorted(draft.version for draft in drafts) == list(range(1, 7))
    for index, draft in enumerate(drafts):
        assert draft.parameters == {"index": index}
        assert load_script_draft(root, SCRIPT_ID, draft.version) == draft
    directory = root / "scripts/drafts" / SCRIPT_ID
    assert {path.name for path in directory.iterdir()} == {f"{version}.json" for version in range(1, 7)}


def test_concurrent_snapshots_with_same_run_id_create_only_one(project):
    database, root, _, asset = project
    for script_text in ("# first\n", "# second\n"):
        save_script_draft(root, SCRIPT_ID, script_text=script_text, inputs={"targets": asset.ref}, parameters={})
    gate = Barrier(2, timeout=10)

    def create(version):
        gate.wait()
        try:
            return create_script_snapshot(database, root, SCRIPT_ID, version, script_run_id="same-id")
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (1, 2)))
    committed, = [result for result in results if result is not None]
    assert load_script_snapshot(root, "same-id") == committed
    saved = load_script_draft(root, SCRIPT_ID, committed.version)
    assert (root / "scripts/same-id" / committed.script_path).read_bytes() == saved.script_text.encode("utf-8")
    assert {path.name for path in (root / "scripts").iterdir()} == {"drafts", "same-id"}


@pytest.mark.parametrize("version", [0, -1, True, 1.0, "1"])
def test_exact_version_requires_strict_positive_integer(tmp_path, version):
    with pytest.raises(ValueError, match="version"):
        load_script_draft(tmp_path, SCRIPT_ID, version)


@pytest.mark.parametrize("parameters", [{"bad": float("nan")}, {"nested": [float("inf")]}, {"bad": (1, 2)}, {"nested": {1: "x"}}])
def test_non_json_parameters_are_rejected_before_creating_workspace(tmp_path, parameters):
    root = tmp_path / "workspace"
    with pytest.raises(ValueError):
        save_script_draft(root, SCRIPT_ID, script_text="", inputs={}, parameters=parameters)
    assert not root.exists()


@pytest.mark.parametrize("invalid", ["", ".", "..", "../formal-run", "a/b", "a\\b", "name with space"])
def test_path_like_ids_are_rejected_before_writes(tmp_path, invalid):
    root = tmp_path / "workspace"
    with pytest.raises(ValueError, match="script_id"):
        save_script_draft(root, invalid, script_text="", inputs={}, parameters={})
    assert not root.exists()


def test_internal_symbolic_links_are_rejected(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "scripts").symlink_to(external, target_is_directory=True)
    with pytest.raises(OSError):
        save_script_draft(root, SCRIPT_ID, script_text="", inputs={}, parameters={})
    assert not list(external.iterdir())


@pytest.mark.parametrize("fault", ["missing_version", "changed_source"])
def test_failed_input_validation_removes_candidate_directory(project, fault):
    database, root, source, asset = project
    reference = asset.ref
    if fault == "changed_source":
        frame = pyogrio.read_dataframe(source, layer="targets", max_features=1)
        pyogrio.write_dataframe(frame, source, layer="targets", append=True)
    else:
        reference = reference.model_copy(update={"asset_version": 2})
    save_script_draft(root, SCRIPT_ID, script_text="", inputs={"targets": reference}, parameters={})
    with pytest.raises(InputChangedError if fault == "changed_source" else LookupError):
        create_script_snapshot(database, root, SCRIPT_ID, 1, script_run_id="unpublished")
    assert not (root / "scripts/unpublished").exists()
    assert load_script_draft(root, SCRIPT_ID, 1).inputs == {"targets": reference}


def test_snapshot_is_unreadable_before_atomic_publication_and_rolls_back_on_failure(project, monkeypatch):
    database, root, _, asset = project
    draft = save_script_draft(root, SCRIPT_ID, script_text="print('draft')\n", inputs={"targets": asset.ref}, parameters={})
    original_rename = os.rename

    def fail_publication(source, destination, *args, **kwargs):
        if destination == "snapshot" and source.startswith(".snapshot"):
            workdir = root / "scripts/unpublished"
            assert pyogrio.read_dataframe(workdir / "inputs/0001/source.gpkg", layer="targets").crs.to_epsg() == 32650
            assert S_IMODE((workdir / source).stat().st_mode) == 0o500
            with pytest.raises(FileNotFoundError):
                load_script_snapshot(root, "unpublished")
            raise OSError("test snapshot publication failed")
        return original_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(snapshot.os, "rename", fail_publication)
    with pytest.raises(OSError, match="test snapshot publication failed"):
        create_script_snapshot(database, root, SCRIPT_ID, draft.version, script_run_id="unpublished")
    assert not (root / "scripts/unpublished").exists()
    assert load_script_draft(root, SCRIPT_ID, 1) == draft


def test_snapshot_reopen_detects_script_content_change_and_symbolic_link(project):
    database, root, _, _ = project
    save_script_draft(root, SCRIPT_ID, script_text="print('original')\n", inputs={}, parameters={})
    result = create_script_snapshot(database, root, SCRIPT_ID, 1, script_run_id="script-run")
    path = root / "scripts/script-run" / result.script_path
    path.chmod(0o600)
    path.write_text("print('edited externally')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        load_script_snapshot(root, result.script_run_id)
    path.parent.chmod(0o700)
    external = root / "outside.py"
    external.write_text("print('original')\n", encoding="utf-8")
    path.unlink()
    path.symlink_to(external)
    with pytest.raises(OSError):
        load_script_snapshot(root, result.script_run_id)
