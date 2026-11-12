import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/quality/validate_acceptance_consistency.py"
MATRIX = Path("docs/traceability/requirements-matrix.md")
FIXED_CASES = Path("docs/acceptance/FIXED_CASES.md")
BOARD = Path("plans/06_PROGRESS/TASK_BOARD.md")
INVENTORY = Path("plans/06_PROGRESS/task_inventory.json")
REPORT = Path("docs/release/FINAL_ACCEPTANCE.md")
FOLLOW_UP = Path("docs/acceptance/traceability-follow-up.md")
BASELINE = Path("docs/round2/audit/baseline.json")


@pytest.fixture
def ledger_root(tmp_path):
    for path in (MATRIX, FIXED_CASES, BOARD, INVENTORY, REPORT, FOLLOW_UP, BASELINE):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, tmp_path / path)
    return tmp_path


def run_validator(ledger_root):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(ledger_root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_reconciles_recorded_statuses_without_local_runtime_evidence(ledger_root):
    result = run_validator(ledger_root)
    assert result.returncode == 0, result.stderr
    assert not (ledger_root / "artifacts").exists()
    summary = json.loads(result.stdout)
    assert summary["scope"] == "historical_ledger_consistency"
    assert summary["baseline_commit"] == json.loads((ledger_root / BASELINE).read_text())["commit"]
    assert summary["ledgers"] == {
        "V2.0 通用需求": {"PASS": 40, "PARTIAL": 0, "NOT_IMPLEMENTED": 0},
        "T01–T16 固定用例": {"PASS": 16, "FAIL": 0, "NOT_RUN": 0},
        "P0–P11 原子任务": {"ACCEPTED": 122},
    }
    assert summary["follow_up_requirements"] == ["CTX-001", "CTX-003", "TOOL-003", "TOOL-004"]


@pytest.mark.parametrize(
    "path, old, new, diagnostic",
    [
        (MATRIX, "| DATA-002 |", "| DATA-001 |", "duplicate row DATA-001"),
        (FIXED_CASES, "| T16 |", "| T17 |", "missing ['T16'], unexpected ['T17']"),
        (BOARD, "| P11-08 |", "| P11-09 |", "missing ['P11-08'], unexpected ['P11-09']"),
        (REPORT, "| 40 | 40 PASS、", "| 40 | 36 PASS、", "historical ledger summary differs"),
        (REPORT, "80d724f35fba31cacb9d3f740265b2713c6be6f4", "0" * 40, "expected V1 baseline commit"),
        (FOLLOW_UP, "登记后运行工具成果规则（PASS）", "登记后运行工具成果规则（FAIL）", "TOOL-004: FAIL disagrees"),
        (FOLLOW_UP, "需求矩阵为 40 PASS", "需求矩阵为 36 PASS", "requirement totals disagree"),
    ],
)
def test_rejects_drift_in_a_ledger_or_summary(ledger_root, path, old, new, diagnostic):
    target = ledger_root / path
    original = target.read_text(encoding="utf-8")
    assert old in original
    target.write_text(original.replace(old, new, 1), encoding="utf-8")
    result = run_validator(ledger_root)
    assert result.returncode == 1
    assert diagnostic in result.stderr
    assert not result.stdout


def test_reports_missing_input(ledger_root):
    (ledger_root / MATRIX).unlink()
    result = run_validator(ledger_root)
    assert result.returncode == 1
    assert "requirements-matrix.md" in result.stderr
    assert not result.stdout
