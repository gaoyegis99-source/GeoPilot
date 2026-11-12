"""Store append-only script drafts and prepare isolated script run snapshots."""

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from shutil import rmtree
from stat import S_ISREG
from uuid import uuid4

from pydantic import JsonValue

from geopilot.application.run_inputs import stage_run_inputs
from geopilot.domain.data_asset import DataAssetRef
from geopilot.persistence.workspace import _open_directory, _open_path, _validate_component
from geopilot.scripts.models import ScriptDraft, ScriptSnapshot


_SCRIPT_FILE = "script.py"
_REQUEST_FILE = "request.json"
_SNAPSHOT = "snapshot"
_STAGED_SNAPSHOT = ".snapshot"
SCRIPT_REQUEST_PATH = f"{_SNAPSHOT}/{_REQUEST_FILE}"


def _remove_file(directory: int, name: str, failure: BaseException) -> None:
    try:
        os.unlink(name, dir_fd=directory)
    except OSError as cleanup:
        failure.add_note(f"Could not remove incomplete file {name}: {cleanup}")


def _write_readonly(directory: int, name: str, payload: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), 0o400)
            os.fsync(stream.fileno())
    except BaseException as failure:
        _remove_file(directory, name, failure)
        raise


def _read_file(directory: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        if not S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Expected a regular local file: {name}")
        return stream.read()


def _json_bytes(model: ScriptDraft | ScriptSnapshot) -> bytes:
    return (model.model_dump_json(indent=2) + "\n").encode("utf-8")


def _publish_draft(directory: int, draft: ScriptDraft) -> None:
    temporary = f".draft-{uuid4().hex}.tmp"
    _write_readonly(directory, temporary, _json_bytes(draft))
    try:
        os.link(temporary, f"{draft.version}.json", src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    except BaseException as failure:
        _remove_file(directory, temporary, failure)
        raise
    os.unlink(temporary, dir_fd=directory)


def save_script_draft(
    workspace_root: str | Path, script_id: str, *, script_text: str,
    inputs: Mapping[str, DataAssetRef], parameters: Mapping[str, JsonValue],
) -> ScriptDraft:
    """Append a version while preserving all existing draft files."""
    _validate_component(script_id, "script_id")
    initial = ScriptDraft(
        id=script_id, version=1, script_text=script_text, inputs=dict(inputs),
        parameters=dict(parameters), created_at=datetime.now(UTC),
    )
    root = Path(workspace_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with _open_path(root, ("scripts", "drafts", script_id), create=True) as directory:
        versions = (
            int(name[:-5]) for name in os.listdir(directory)
            if name.endswith(".json") and name[:-5].isascii() and name[:-5].isdecimal()
        )
        version = max(versions, default=0) + 1
        while True:
            draft = initial.model_copy(update={"version": version})
            try:
                _publish_draft(directory, draft)
            except FileExistsError:
                version += 1
            else:
                return draft


def load_script_draft(workspace_root: str | Path, script_id: str, version: int) -> ScriptDraft:
    """Read the exact saved draft version."""
    _validate_component(script_id, "script_id")
    if type(version) is not int or version <= 0:
        raise ValueError("version must be a positive integer")
    root = Path(workspace_root).resolve()
    with _open_path(root, ("scripts", "drafts", script_id)) as directory:
        draft = ScriptDraft.model_validate_json(_read_file(directory, f"{version}.json"))
    if (draft.id, draft.version) != (script_id, version):
        raise ValueError("Draft identity differs from its saved version path")
    return draft


def _validate_run_id(script_run_id: str) -> None:
    _validate_component(script_run_id, "script_run_id")
    if script_run_id == "drafts":
        raise ValueError("script_run_id cannot use the drafts directory name")


def _rollback_snapshot(scripts: int, workdir: Path, failure: BaseException) -> None:
    try:
        with _open_directory(workdir, scripts) as run:
            try:
                os.stat(_SNAPSHOT, dir_fd=run, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                # Preserve a complete snapshot if interrupted after publication.
                return
            try:
                with _open_directory(workdir / _STAGED_SNAPSHOT, run) as staged:
                    os.fchmod(staged, 0o700)
            except FileNotFoundError:
                pass
        rmtree(workdir.name, dir_fd=scripts)
    except OSError as cleanup:
        failure.add_note(f"Could not remove incomplete script run {workdir}: {cleanup}")


def create_script_snapshot(
    database_path: str | Path, workspace_root: str | Path, script_id: str, version: int, *, script_run_id: str | None = None,
) -> ScriptSnapshot:
    """Stage exact registered inputs and publish a read-only request in a new script run."""
    run_id = uuid4().hex if script_run_id is None else script_run_id
    _validate_run_id(run_id)
    root = Path(workspace_root).resolve()
    draft = load_script_draft(root, script_id, version)
    script = draft.script_text.encode("utf-8")
    workdir = root / "scripts" / run_id
    with _open_path(root, ("scripts",)) as scripts:
        os.mkdir(run_id, mode=0o700, dir_fd=scripts)
        try:
            with _open_directory(workdir, scripts) as run:
                for name in ("inputs", "outputs", _STAGED_SNAPSHOT):
                    os.mkdir(name, mode=0o700, dir_fd=run)
                asset_files = stage_run_inputs(database_path, draft.inputs, workdir)
                snapshot = ScriptSnapshot(
                    script_run_id=run_id, script_id=draft.id, version=draft.version, script_sha256=sha256(script).hexdigest(),
                    inputs=draft.inputs, asset_files=asset_files, parameters=draft.parameters, created_at=datetime.now(UTC),
                )
                with _open_directory(workdir / _STAGED_SNAPSHOT, run) as staged:
                    _write_readonly(staged, _SCRIPT_FILE, script)
                    _write_readonly(staged, _REQUEST_FILE, _json_bytes(snapshot))
                    os.fchmod(staged, 0o500)
                os.rename(_STAGED_SNAPSHOT, _SNAPSHOT, src_dir_fd=run, dst_dir_fd=run)
        except BaseException as failure:
            _rollback_snapshot(scripts, workdir, failure)
            raise
    return snapshot


def load_script_snapshot(workspace_root: str | Path, script_run_id: str) -> ScriptSnapshot:
    """Read a published script request and verify the exact source bytes."""
    _validate_run_id(script_run_id)
    root = Path(workspace_root).resolve()
    with _open_path(root, ("scripts", script_run_id, _SNAPSHOT)) as directory:
        snapshot = ScriptSnapshot.model_validate_json(_read_file(directory, _REQUEST_FILE))
        if snapshot.script_run_id != script_run_id:
            raise ValueError("Snapshot identity differs from the script run directory")
        if sha256(_read_file(directory, _SCRIPT_FILE)).hexdigest() != snapshot.script_sha256:
            raise ValueError("Script bytes differ from the saved SHA-256 fingerprint")
    return snapshot
