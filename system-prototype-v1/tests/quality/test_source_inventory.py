import hashlib
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/quality/build_source_inventory.py"
DIRECTORIES = ("src", "config", "frontend", "tests", "docs", "artifacts")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def fixture_repository(root, *, directories=DIRECTORIES):
    git(root, "init", "--quiet")
    for directory in directories:
        (root / directory).mkdir()
        (root / directory / "带 空格.txt").write_bytes(b"original\r\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--quiet", "-m", "fixture")


def inventory(root, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True, text=True, check=False,
    )


def test_inventory_hashes_committed_bytes_and_lists_worktree_changes(tmp_path):
    fixture_repository(tmp_path)
    commit = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "src/带 空格.txt").write_bytes(b"working copy")
    (tmp_path / "docs/带 空格.txt").unlink()
    (tmp_path / "tests/extra.txt").write_bytes(b"untracked")

    result = inventory(tmp_path, "--ref", commit)
    assert result.returncode == 0, result.stderr
    assert result.stdout == inventory(tmp_path, "--ref", commit).stdout
    payload = json.loads(result.stdout)
    assert payload["commit"] == commit
    assert payload["counts"] == dict.fromkeys(DIRECTORIES, 1)
    assert payload["files"] == [
        {"path": f"{name}/带 空格.txt", "git_mode": "100644",
         "sha256": hashlib.sha256(b"original\r\n").hexdigest()}
        for name in sorted(DIRECTORIES)
    ]
    assert payload["worktree"]["changes"] == [
        {"status": " D", "path": "docs/带 空格.txt"},
        {"status": " M", "path": "src/带 空格.txt"},
        {"status": "??", "path": "tests/extra.txt"},
    ]


def test_inventory_rejects_unknown_commit(tmp_path):
    fixture_repository(tmp_path)
    result = inventory(tmp_path, "--ref", "does-not-exist")
    assert result.returncode == 1
    assert not result.stdout
    assert "Source inventory failed:" in result.stderr


def test_inventory_rejects_missing_required_directory(tmp_path):
    fixture_repository(tmp_path, directories=DIRECTORIES[:-1])
    result = inventory(tmp_path)
    assert result.returncode == 1
    assert not result.stdout
    assert "empty or absent: artifacts" in result.stderr
