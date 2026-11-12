import csv
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/quality/check_doc_evidence_links.py"


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def init_repository(root):
    git(root, "init", "--quiet")
    git(root, "config", "user.name", "Evidence Test")
    git(root, "config", "user.email", "evidence-test@example.invalid")


def commit(root, title):
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", title)
    return git(root, "rev-parse", "HEAD")


def scan(root, report, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--report", str(report), *args],
        capture_output=True, text=True, check=False,
    )


def test_snapshot_classifies_evidence_from_committed_tree(tmp_path):
    init_repository(tmp_path)
    prototype = tmp_path / "prototype"
    docs = prototype / "docs"
    docs.mkdir(parents=True)
    (docs / "old.png").write_bytes(b"image")
    (tmp_path / "design.pdf").write_bytes(b"design document")
    commit(tmp_path, "historical delivery")

    git(tmp_path, "mv", "prototype/docs/old.png", "prototype/docs/new.png")
    git(tmp_path, "rm", "design.pdf")
    (docs / "guide with space.md").write_text("# 第一步\n# 第一步\n", encoding="utf-8")
    (docs / "source.py").write_text("# first\n# second\n", encoding="utf-8")
    (prototype / "frontend/src").mkdir(parents=True)
    (prototype / "frontend/src/component.ts").touch()
    (docs / "report.md").write_text(
        """# Read Me
[local](#read-me)
[Unicode anchor](guide%20with%20space.md#%E7%AC%AC%E4%B8%80%E6%AD%A5-1)
[angle path](<guide with space.md> "title")
[source lines](source.py#L1-L2)
[line beyond EOF](source.py#L3)
[section absent](guide%20with%20space.md#unknown)
[external](https://example.invalid/file.png)
[scheme relative](//example.invalid/file.png)
[local temp](file:///tmp/session/case.json)
[moved image](old.png)
![missing image](broken.png)
[historical design](../../design.pdf)
[outside](../../../outside.md)
[invalid]()
[ref]: guide%20with%20space.md
[artifacts/smoke/screen.png](../artifacts/smoke/screen.png)
`artifacts/test-results/suite.xml` `/tmp/session/request.json`
`src/.../template.py` `src/component.ts`
```sh
python scripts/run.py --junitxml=artifacts/test-results/other.xml
[example](a-link-inside-code.md)
```
""",
        encoding="utf-8",
    )
    sha = commit(tmp_path, "current delivery")

    # Historical references still fail when leftover local output exists.
    (prototype / "artifacts/test-results").mkdir(parents=True)
    (prototype / "artifacts/test-results/suite.xml").touch()
    report = tmp_path / "audit.csv"
    result = scan(prototype, report, "--ref", sha)
    assert result.returncode == 0, result.stderr
    with report.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert {row["commit"] for row in rows} == {sha}
    actual = {row["target"]: row for row in rows}
    expected = {
        "#read-me": "anchor",
        "guide%20with%20space.md#%E7%AC%AC%E4%B8%80%E6%AD%A5-1": "anchor",
        "guide with space.md": "tracked",
        "source.py#L1-L2": "anchor",
        "source.py#L3": "missing_anchor",
        "guide%20with%20space.md#unknown": "missing_anchor",
        "https://example.invalid/file.png": "external",
        "//example.invalid/file.png": "external",
        "file:///tmp/session/case.json": "machine_local",
        "old.png": "moved",
        "broken.png": "missing",
        "../../design.pdf": "removed_from_tree",
        "../../../outside.md": "outside_repository",
        "": "invalid_target",
        "guide%20with%20space.md": "tracked",
        "../artifacts/smoke/screen.png": "untracked_run_output",
        "artifacts/test-results/suite.xml": "untracked_run_output",
        "/tmp/session/request.json": "machine_local",
        "src/.../template.py": "template_path",
        "src/component.ts": "tracked",
        "scripts/run.py": "missing",
        "artifacts/test-results/other.xml": "untracked_run_output",
    }
    assert {target: row["status"] for target, row in actual.items()} == expected
    assert len(rows) == len(expected)
    assert actual["old.png"]["resolved"] == "prototype/docs/new.png"
    assert actual["src/component.ts"]["resolved"] == "prototype/frontend/src/component.ts"

    original = report.read_bytes()
    (docs / "report.md").write_text("[uncommitted](bad.md)\n", encoding="utf-8")
    assert scan(prototype, report, "--ref", sha).returncode == 0
    assert report.read_bytes() == original


def test_scan_ref_and_root_errors_leave_report_unchanged(tmp_path):
    init_repository(tmp_path)
    (tmp_path / "README.md").write_text("[self](README.md)\n", encoding="utf-8")
    commit(tmp_path, "fixture")
    report = tmp_path / "audit.csv"
    report.write_text("previous report", encoding="utf-8")

    assert scan(tmp_path, report, "--ref", "missing-ref").returncode == 1
    assert scan(tmp_path / "missing-root", report).returncode == 1
    assert report.read_text(encoding="utf-8") == "previous report"


def test_empty_markdown_scope_writes_header_only(tmp_path):
    init_repository(tmp_path)
    (tmp_path / "fixture.txt").touch()
    commit(tmp_path, "fixture")
    report = tmp_path / "audit.csv"
    result = scan(tmp_path, report)
    assert result.returncode == 0, result.stderr
    assert "0 Markdown files, 0 references" in result.stdout
    assert report.read_text(encoding="utf-8") == "commit,source,line,kind,target,status,resolved,note\n"
