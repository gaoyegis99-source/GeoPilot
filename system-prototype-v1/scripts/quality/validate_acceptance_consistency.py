"""Reconcile V1's historical acceptance ledgers with the release report."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MATRIX = Path("docs/traceability/requirements-matrix.md")
FIXED_CASES = Path("docs/acceptance/FIXED_CASES.md")
BOARD = Path("plans/06_PROGRESS/TASK_BOARD.md")
INVENTORY = Path("plans/06_PROGRESS/task_inventory.json")
REPORT = Path("docs/release/FINAL_ACCEPTANCE.md")
FOLLOW_UP = Path("docs/acceptance/traceability-follow-up.md")
BASELINE = Path("docs/round2/audit/baseline.json")
REQUIREMENTS = {
    f"{group}-{number:03}"
    for group in ("DATA", "CTX", "MTH", "TOOL", "PLAN", "EXEC", "CHECK", "ART", "UI", "MODEL")
    for number in range(1, 5)
}
CASES = {f"T{number:02}" for number in range(1, 17)}
FOLLOW_UP_IDS = {"CTX-001", "CTX-003", "TOOL-003", "TOOL-004"}
REQUIREMENT_STATUSES = ("PASS", "PARTIAL", "NOT_IMPLEMENTED")
CASE_STATUSES = ("PASS", "FAIL", "NOT_RUN")


def table_rows(root: Path, path: Path, header: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    def cells(line: str) -> tuple[str, ...]:
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            return ()
        return tuple(cell.strip() for cell in re.split(r"(?<!\\)\|", line[1:-1]))

    lines = (root / path).read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if cells(line) == header]
    if len(starts) != 1 or starts[0] + 1 >= len(lines):
        raise ValueError(f"{path}: expected one table with header {header!r}")
    divider = cells(lines[starts[0] + 1])
    if len(divider) != len(header) or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in divider):
        raise ValueError(f"{path}: invalid Markdown table divider")
    rows = {}
    for line in lines[starts[0] + 2:]:
        if not line.strip().startswith("|"):
            break
        row = cells(line)
        if len(row) != len(header) or not all(row):
            raise ValueError(f"{path}: invalid table row {line!r}")
        if row[0] in rows:
            raise ValueError(f"{path}: duplicate row {row[0]}")
        rows[row[0]] = row
    return rows


def status_counts(
    path: Path, rows: dict[str, tuple[str, ...]], expected: set[str],
    status_column: int, allowed: tuple[str, ...],
) -> dict[str, int]:
    missing = sorted(expected - rows.keys())
    unexpected = sorted(rows.keys() - expected)
    if missing or unexpected:
        raise ValueError(f"{path}: missing {missing}, unexpected {unexpected}")
    counts = Counter()
    for identifier, row in rows.items():
        status = row[status_column]
        if status not in allowed:
            raise ValueError(f"{path}: {identifier}: unexpected status {status!r}")
        counts[status] += 1
    return {status: counts[status] for status in allowed}


def verify_follow_up(root: Path, requirements: dict[str, tuple[str, ...]], counts: dict[str, int]) -> None:
    content = (root / FOLLOW_UP).read_text(encoding="utf-8")
    outcomes = re.findall(r"^## ([A-Z]+-[0-9]{3}) [^\n]+（([A-Z_]+)）$", content, re.MULTILINE)
    if len(outcomes) != len(FOLLOW_UP_IDS) or {identifier for identifier, _ in outcomes} != FOLLOW_UP_IDS:
        raise ValueError(f"{FOLLOW_UP}: expected one heading for each of {sorted(FOLLOW_UP_IDS)}")
    for identifier, status in outcomes:
        if status != requirements[identifier][2] or status != "PASS":
            raise ValueError(f"{FOLLOW_UP}: {identifier}: {status} disagrees with matrix PASS")
    declared = re.findall(r"需求矩阵为 (\d+) PASS、(\d+) PARTIAL、(\d+) NOT_IMPLEMENTED。", content)
    expected = tuple(str(counts[status]) for status in REQUIREMENT_STATUSES)
    if declared != [expected]:
        raise ValueError(f"{FOLLOW_UP}: requirement totals disagree with matrix {counts}")


def verify_release_report(root: Path, commit: str, ledgers: dict[str, dict[str, int]]) -> None:
    content = (root / REPORT).read_text(encoding="utf-8")
    reported_commits = re.findall(r"^V1 审计基线提交：`([0-9a-f]{40})`。", content, re.MULTILINE)
    if reported_commits != [commit]:
        raise ValueError(f"{REPORT}: expected V1 baseline commit {commit}")
    rows = table_rows(root, REPORT, ("台账", "总数", "历史状态"))
    expected = {
        name: (name, str(sum(counts.values())), "、".join(f"{count} {status}" for status, count in counts.items()))
        for name, counts in ledgers.items()
    }
    if rows != expected:
        raise ValueError(f"{REPORT}: historical ledger summary differs from {expected}")


def reconcile(root: Path) -> dict:
    commit = json.loads((root / BASELINE).read_text(encoding="utf-8")).get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"{BASELINE}: expected a full baseline commit SHA")
    requirements = table_rows(root, MATRIX, ("需求 ID", "V2.0 验收目标", "状态", "实现", "测试证据", "缺口"))
    requirement_counts = status_counts(MATRIX, requirements, REQUIREMENTS, 2, REQUIREMENT_STATUSES)
    for identifier, row in requirements.items():
        if (row[2] == "PASS") != (row[5] == "—"):
            raise ValueError(f"{MATRIX}: {identifier}: status and gap disagree")
    if requirement_counts["PASS"] != len(REQUIREMENTS):
        raise ValueError(f"{MATRIX}: expected all 40 requirements to have recorded PASS")
    verify_follow_up(root, requirements, requirement_counts)

    cases = table_rows(root, FIXED_CASES, ("编号", "验证内容", "首次完整验收阶段", "结果", "证据"))
    case_counts = status_counts(FIXED_CASES, cases, CASES, 3, CASE_STATUSES)
    if case_counts["PASS"] != len(CASES):
        raise ValueError(f"{FIXED_CASES}: expected all 16 cases to have recorded PASS")

    inventory = json.loads((root / INVENTORY).read_text(encoding="utf-8"))
    task_ids = {task["id"] for task in inventory["tasks"]}
    if inventory["task_count"] != 122 or len(inventory["tasks"]) != 122 or len(task_ids) != 122:
        raise ValueError(f"{INVENTORY}: expected 122 unique historical task IDs")
    board = table_rows(root, BOARD, ("Task ID", "阶段", "任务", "Agent", "依赖", "状态", "验收证据"))
    task_counts = status_counts(BOARD, board, task_ids, 5, ("ACCEPTED",))
    ledgers = {"V2.0 通用需求": requirement_counts, "T01–T16 固定用例": case_counts, "P0–P11 原子任务": task_counts}
    verify_release_report(root, commit, ledgers)
    return {
        "scope": "historical_ledger_consistency",
        "baseline_commit": commit,
        "ledgers": ledgers,
        "follow_up_requirements": sorted(FOLLOW_UP_IDS),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="GeoPilot prototype directory")
    args = parser.parse_args()
    try:
        print(json.dumps(reconcile(args.root), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        parser.exit(1, f"Acceptance reconciliation failed: {error}\n")


if __name__ == "__main__":
    main()
