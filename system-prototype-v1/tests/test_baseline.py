import json
import re
from collections import Counter
from pathlib import Path


PLANS = Path(__file__).resolve().parents[1] / "plans"


def test_task_catalog_has_one_entry_per_plan_document():
    task_dir = PLANS / "04_TASKS"
    progress_dir = PLANS / "06_PROGRESS"
    inventory = json.loads((progress_dir / "task_inventory.json").read_text(encoding="utf-8"))
    documents = sorted(task_dir.glob("P*/P*-*.md"))
    assert len(documents) == inventory["task_count"] > 0

    document_ids = Counter(path.stem for path in documents)
    inventory_ids = Counter(task["id"] for task in inventory["tasks"])
    board = (progress_dir / "TASK_BOARD.md").read_text(encoding="utf-8")
    board_ids = Counter(re.findall(r"^\| (P\d+-\d+) \|", board, re.MULTILINE))
    assert all(count == 1 for count in document_ids.values())
    assert inventory_ids == document_ids
    assert board_ids == document_ids

    index = (task_dir / "INDEX.md").read_text(encoding="utf-8")
    index_entries = re.findall(r"^- \[(P\d+-\d+) — [^\n]+\]\(([^\n)]+)\)", index, re.MULTILINE)
    expected_entries = Counter((path.stem, path.relative_to(task_dir).as_posix()) for path in documents)
    assert Counter(index_entries) == expected_entries
