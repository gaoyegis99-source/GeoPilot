import errno
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from geopilot.persistence import workspace


def test_run_and_step_paths_follow_v2_layout_and_preserve_inputs(tmp_path):
    root = tmp_path / "workspace"
    source = root / "assets" / "original.gpkg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original source")

    run = workspace.create_run_workspace(root, "task-001", "run-001")
    assert run.path == root / "tasks" / "task-001" / "runs" / "run-001"
    assert run.request_path == run.path / "request.json"
    assert run.cancel_path == run.path / "cancel.flag"
    assert {item.name for item in run.path.iterdir() if item.is_dir()} == {
        "inputs", "steps", "intermediate", "outputs", "previews", "checks",
    }
    step = run.create_step("buffer_roads")
    assert step == run.path / "steps" / "buffer_roads"
    assert step.is_dir() and not list(step.iterdir())
    assert not run.request_path.exists()
    assert not run.cancel_path.exists()

    run.request_path.write_text("{}", encoding="utf-8")
    log = step / "process.log"
    log.write_text("finished", encoding="utf-8")
    retry = workspace.create_run_workspace(root, "task-001", "run-002")
    same_run_other_task = workspace.create_run_workspace(root, "task-002", "run-001")
    assert len({run.path, retry.path, same_run_other_task.path}) == 3
    assert not retry.request_path.exists()
    assert run.request_path.read_text(encoding="utf-8") == "{}"
    assert log.read_text(encoding="utf-8") == "finished"
    assert source.read_bytes() == b"original source"


def test_existing_run_and_step_are_rejected_without_modifying_contents(tmp_path):
    root = tmp_path / "workspace"
    run = workspace.create_run_workspace(root, "task", "run")
    step = run.create_step("buffer")
    log = step / "process.log"
    log.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError) as run_error:
        workspace.create_run_workspace(root, "task", "run")
    assert run_error.value.filename == run.path
    with pytest.raises(FileExistsError) as step_error:
        run.create_step("buffer")
    assert step_error.value.filename == step
    assert log.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("invalid", ["", ".", "..", "a/b", "a\\b", "a b", "C:drive", "bad\x00id"])
def test_invalid_ids_are_rejected_before_filesystem_writes(tmp_path, invalid):
    root = tmp_path / "workspace"
    with pytest.raises(ValueError, match="task_id"):
        workspace.create_run_workspace(root, invalid, "run")
    with pytest.raises(ValueError, match="run_id"):
        workspace.create_run_workspace(root, "task", invalid)
    assert not root.exists()

    run = workspace.create_run_workspace(root, "task", "run")
    with pytest.raises(ValueError, match="step_key"):
        run.create_step(invalid)
    assert not list((run.path / "steps").iterdir())


def test_root_alias_is_resolved_and_returned_paths_use_canonical_root(tmp_path):
    real_root = tmp_path / "actual workspace"
    real_root.mkdir()
    root_alias = tmp_path / "workspace alias"
    root_alias.symlink_to(real_root, target_is_directory=True)

    run = workspace.create_run_workspace(root_alias, "task-1", "run-1")
    assert run.path == real_root / "tasks" / "task-1" / "runs" / "run-1"
    assert run.create_step("read").is_relative_to(real_root)


@pytest.mark.parametrize("parts", [
    ("tasks",), ("tasks", "task"), ("tasks", "task", "runs"),
    ("tasks", "task", "runs", "run"),
])
def test_run_creation_rejects_symlinks_inside_workspace(tmp_path, parts):
    root = tmp_path / "workspace"
    external = tmp_path / "outside"
    external.mkdir()
    link = root.joinpath(*parts)
    link.parent.mkdir(parents=True)
    link.symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError) as error:
        workspace.create_run_workspace(root, "task", "run")
    assert error.value.filename == link
    assert link.is_symlink()
    assert not list(external.iterdir())


def test_create_step_rechecks_internal_links_after_run_creation(tmp_path):
    run = workspace.create_run_workspace(tmp_path / "workspace", "task", "run")
    external = tmp_path / "outside"
    external.mkdir()
    steps = run.path / "steps"
    steps.rmdir()
    steps.symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError) as error:
        run.create_step("buffer")
    assert error.value.filename == steps
    assert not list(external.iterdir())


def test_existing_file_in_directory_chain_reports_the_path(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    existing_file = root / "tasks"
    existing_file.write_text("keep", encoding="utf-8")

    with pytest.raises(NotADirectoryError) as error:
        workspace.create_run_workspace(root, "task", "run")
    assert error.value.filename == existing_file
    assert existing_file.read_text(encoding="utf-8") == "keep"


def test_concurrent_creators_can_claim_the_same_run_only_once(tmp_path):
    root = tmp_path / "workspace"
    expected_path = root / "tasks" / "task" / "runs" / "run"
    workers = 6
    ready = Barrier(workers)

    def create():
        ready.wait(timeout=5)
        try:
            return workspace.create_run_workspace(root, "task", "run")
        except FileExistsError as error:
            assert error.filename == expected_path
            return None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(lambda _: create(), range(workers)))

    successful = [run for run in results if run is not None]
    assert len(successful) == 1
    assert successful[0].create_step("compute").is_dir()
    assert len(list(successful[0].path.parent.iterdir())) == 1


def test_partial_creation_rolls_back_owned_empty_directories_and_can_retry(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    existing = workspace.create_run_workspace(root, "task", "existing-run")
    existing.request_path.write_text("{}", encoding="utf-8")
    failed_run = existing.path.parent / "failed-run"
    real_mkdir = workspace.os.mkdir

    def fail_outputs(path, mode=0o777, *, dir_fd=None):
        if path == "outputs" and dir_fd is not None:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_mkdir(path, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(workspace.os, "mkdir", fail_outputs)
        with pytest.raises(OSError) as error:
            workspace.create_run_workspace(root, "task", "failed-run")
    assert error.value.errno == errno.ENOSPC
    assert error.value.filename == failed_run / "outputs"
    assert not failed_run.exists()
    assert existing.request_path.read_text(encoding="utf-8") == "{}"
    assert workspace.create_run_workspace(root, "task", "failed-run").path == failed_run


def test_partial_cleanup_leaves_unexpected_nonempty_directory_intact(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    failed_run = root / "tasks" / "task" / "runs" / "failed-run"
    retained = failed_run / "inputs" / "retained.gpkg"
    real_mkdir = workspace.os.mkdir

    def fail_outputs(path, mode=0o777, *, dir_fd=None):
        if path == "outputs" and dir_fd is not None:
            retained.write_bytes(b"unexpected file")
            raise OSError(errno.ENOSPC, "No space left on device")
        real_mkdir(path, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(workspace.os, "mkdir", fail_outputs)
        with pytest.raises(OSError) as error:
            workspace.create_run_workspace(root, "task", "failed-run")
    assert error.value.errno == errno.ENOSPC
    assert any(str(retained.parent) in note for note in error.value.__notes__)
    assert retained.read_bytes() == b"unexpected file"
    with pytest.raises(FileExistsError):
        workspace.create_run_workspace(root, "task", "failed-run")
