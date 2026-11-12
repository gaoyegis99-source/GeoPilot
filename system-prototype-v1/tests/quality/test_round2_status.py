import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/quality/validate_round2_status.py"
BOARD = Path("plans-round2/06_PROGRESS/TASK_BOARD.md")
REPORT = Path("docs/round2/audit/P12_FINAL_AUDIT.md")
HOSTED = Path("artifacts/test-results/round2/ci-hosted/run-43148595202.json")


@pytest.fixture
def release_root(tmp_path):
    for folder in ("plans-round2", "docs/round2/audit", "artifacts/test-results/round2"):
        shutil.copytree(ROOT / folder, tmp_path / folder)
    board = tmp_path / BOARD
    lines = board.read_text(encoding="utf-8").splitlines()
    board.write_text("\n".join(
        line.replace("| ACCEPTED |", "| DOING |") if line.startswith("| P12-12 |") else line
        for line in lines
    ) + "\n", encoding="utf-8")
    return tmp_path


def invoke(root, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True, text=True, check=False,
    )


def test_evidence_ready_before_and_after_signoff(release_root):
    result = invoke(release_root)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["release_ready"] and not report["released"]
    assert report["prerequisites_accepted"] == 11 and report["v1_passed"]["fixed_cases"] == 16

    strict = invoke(release_root, "--require-released")
    assert strict.returncode == 1 and "P12-12: gate requires ACCEPTED" in strict.stderr
    board = release_root / BOARD
    board.write_text("\n".join(
        line.replace("| DOING |", "| ACCEPTED |").rsplit("|", 2)[0]
        + "| [P12 audit](../../docs/round2/audit/P12_FINAL_AUDIT.md) |"
        if line.startswith("| P12-12 |") else line
        for line in board.read_text(encoding="utf-8").splitlines()
    ) + "\n", encoding="utf-8")
    strict = invoke(release_root, "--require-released")
    assert strict.returncode == 0, strict.stderr
    assert json.loads(strict.stdout)["released"]


@pytest.mark.parametrize(
    ("file", "old", "new", "diagnostic"),
    [
        (BOARD, "| P12-07 |", "| P12-06 |", "duplicate P12-06"),
        (BOARD, "| 主控 | 无 | ACCEPTED |", "| 主控 | 无 | REWORK |", "P12-01: gate requires ACCEPTED"),
        (REPORT, "| AUD-12 |", "| AUD-13 |", "exactly AUD-01 through AUD-12"),
        (REPORT, "](concurrency-model.md)", "](missing.md)", "AUD-06: invalid evidence"),
        (HOSTED, '"conclusion":"success"', '"conclusion":"failure"', "Hosted CI did not succeed"),
    ],
)
def test_release_rejects_incomplete_or_conflicting_evidence(release_root, file, old, new, diagnostic):
    path = release_root / file
    content = path.read_text(encoding="utf-8")
    assert old in content
    path.write_text(content.replace(old, new, 1), encoding="utf-8")
    result = invoke(release_root)
    assert result.returncode == 1 and diagnostic in result.stderr
    assert not result.stdout


def test_release_rejects_tampered_fixed_case_evidence(release_root):
    log = release_root / "artifacts/test-results/round2/gis-regression.log"
    log.write_bytes(log.read_bytes() + b"unexpected change\n")
    result = invoke(release_root)
    assert result.returncode == 1 and "Fixed-case hash mismatch" in result.stderr
