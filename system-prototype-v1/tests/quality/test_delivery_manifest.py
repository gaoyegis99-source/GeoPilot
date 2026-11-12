import csv
import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/quality/verify_delivery_manifest.py"
LEGACY = runpy.run_path(str(ROOT / "artifacts/reports/final/delivery_manifest.py"))
MANIFEST = LEGACY["MANIFEST"]
BASELINE = Path("docs/round2/audit/baseline.json")
EVIDENCE = Path("docs/round2/audit/evidence-link-audit.csv")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


def write(root, path, content):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")


def manifest_row(root, path):
    digest = hashlib.sha256((root / path).read_bytes()).hexdigest()
    return {"path": path, "sha256": digest}


def invoke(root, ref=None):
    args = ["--ref", ref] if ref is not None else []
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args],
                          text=True, capture_output=True, check=False)


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "--quiet")
    source = sorted((*LEGACY["SOURCE_FILES"], "src/地理 测试.py", "config/item.yaml"))
    for path in source:
        write(tmp_path, path, f"source {path}\n")
    for paths in LEGACY["EVIDENCE"].values():
        for path in paths:
            write(tmp_path, path, f"evidence {path}\n")
    rows = [manifest_row(tmp_path, path) for path in source]
    index = "".join(f"{row['sha256']}  {row['path']}\n" for row in rows)
    manifest = {
        "release": "fixture",
        "acceptance_date": "2026-11-12",
        "source_sha256": hashlib.sha256(index.encode()).hexdigest(),
        "source_files": rows,
        "evidence": {group: [manifest_row(tmp_path, path) for path in paths]
                     for group, paths in LEGACY["EVIDENCE"].items()},
    }
    write(tmp_path, MANIFEST, json.dumps(manifest, ensure_ascii=False))
    return tmp_path, commit(tmp_path), manifest


def test_matching_delivery_is_read_only(repository):
    root, ref, _ = repository
    original = (root / MANIFEST).read_bytes()
    result = invoke(root, ref)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["verification"] == "matched"
    assert report["pinned"]["commit"] == ref
    assert report["pinned"]["source"]["counts"]["matched"] == 10
    assert report["pinned"]["evidence"]["counts"]["matched"] == 31
    assert report["worktree"]["dirty"] is False
    assert (root / MANIFEST).read_bytes() == original
    assert not git(root, "status", "--short")


def test_reports_pinned_and_live_drift_and_evidence_provenance(repository):
    root, original, _ = repository
    missing = "artifacts/test-results/p11-05-junit.xml"
    retained = "artifacts/test-results/round2-new.xml"
    (root / missing).unlink()
    (root / "src/地理 测试.py").unlink()
    write(root, retained, "new tracked evidence")
    write(root, "config/item.yaml", "changed at second commit")
    write(root, "src/new.py", "added at second commit")
    write(root, "README.md", "changed evidence")
    ref = commit(root)

    write(root, "config/item.yaml", "live-only change")
    write(root, "src/draft.py", "untracked source")
    write(root, "src/__pycache__/draft.pyc", "ignored build output")
    write(root, BASELINE, json.dumps({"commit": ref}))
    (root / EVIDENCE).parent.mkdir(parents=True, exist_ok=True)
    with (root / EVIDENCE).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("commit", "source", "line", "status", "resolved"))
        writer.writerow((original, "outdated.md", 3, "tracked", missing))
        writer.writerow((ref, "report.md", 5, "untracked_run_output", missing))
    result = invoke(root)
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["manifest"]["source_index"]["matches"]
    assert report["pinned"]["source"]["counts"] == {
        "matched": 8, "added": 1, "missing": 1, "changed": 1, "invalid": 0,
    }
    assert report["worktree"]["source"]["counts"]["added"] == 2
    assert report["worktree"]["dirty"] is True
    assert report["pinned"]["source"]["differences"][0]["observed_sha256"] != (
        report["worktree"]["source"]["differences"][0]["observed_sha256"]
    )
    evidence = {row["path"]: row for row in report["pinned"]["evidence"]["differences"]}
    assert evidence["README.md"]["status"] == "changed"
    assert evidence[retained]["status"] == "added"
    assert evidence[missing]["status"] == "missing"
    assert evidence[missing]["historical_classification"] == "run_output_absent_from_pinned_commit"
    assert evidence[missing]["p12_03_references"] == [
        {"source": "report.md", "line": "5", "status": "untracked_run_output"},
    ]


@pytest.mark.parametrize("path", ["../secret", "src/../secret", "/etc/passwd", "C:/secret"])
def test_rejects_unsafe_manifest_paths(repository, path):
    root, _, manifest = repository
    manifest["source_files"][0]["path"] = path
    write(root, MANIFEST, json.dumps(manifest))
    result = invoke(root, commit(root))
    assert result.returncode == 2
    assert "unsafe relative path" in result.stderr
    assert not result.stdout


def test_rejects_duplicate_manifest_rows(repository):
    root, _, manifest = repository
    manifest["source_files"].append(manifest["source_files"][0])
    write(root, MANIFEST, json.dumps(manifest))
    result = invoke(root, commit(root))
    assert result.returncode == 2
    assert "duplicate path" in result.stderr


def test_reports_inconsistent_source_index(repository):
    root, _, manifest = repository
    manifest["source_sha256"] = "0" * 64
    write(root, MANIFEST, json.dumps(manifest))
    result = invoke(root, commit(root))
    assert result.returncode == 1
    assert json.loads(result.stdout)["manifest"]["source_index"]["matches"] is False


def test_does_not_hash_symlink_replacements(repository):
    root, ref, manifest = repository
    location = root / "config/item.yaml"
    location.unlink()
    location.symlink_to(root / "README.md")
    expected = next(row["sha256"] for row in manifest["source_files"]
                    if row["path"] == "config/item.yaml")
    result = invoke(root, ref)
    assert result.returncode == 1
    assert json.loads(result.stdout)["worktree"]["source"]["differences"] == [
        {"path": "config/item.yaml", "status": "invalid",
         "manifest_sha256": expected,
         "observed_sha256": None, "problem": "Path contains a symlink"},
    ]


def test_rejects_missing_ref(repository):
    root, _, _ = repository
    result = invoke(root, "missing-ref")
    assert result.returncode == 2
    assert "Delivery manifest audit failed:" in result.stderr
