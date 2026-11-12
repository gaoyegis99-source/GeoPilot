"""Verify the task and technical candidate prerequisites for the V2 decision."""

import argparse
import json
from pathlib import Path
import sys
from xml.etree import ElementTree

from round2_release_manifest import MANIFEST, verify_round2_manifest
from validate_round2_status import BOARD, ROOT, check_links, load_board, require


TASK = "P16-07"
PREREQUISITES = (
    "P13-08", "P14-08", "P15-08", "P16-01", "P16-02", "P16-04", "P16-05", "P16-06",
)


def validate(root: Path) -> dict:
    root = root.resolve(strict=True)
    board = load_board(root)
    missing = [task for task in (*PREREQUISITES, TASK) if task not in board]
    require(not missing, f"Missing V2 release tasks: {', '.join(missing)}")

    prerequisites = {}
    for task in PREREQUISITES:
        _, _, _, _, status, evidence = board[task]
        prerequisites[task] = status
        if status == "ACCEPTED":
            check_links(root, BOARD, evidence, task)
    blockers = [task for task, status in prerequisites.items() if status != "ACCEPTED"]
    _, _, _, _, decision_status, decision_evidence = board[TASK]
    if decision_status == "ACCEPTED":
        require(not blockers, f"{TASK}: accepted with unmet prerequisites")
        check_links(root, BOARD, decision_evidence, TASK)
    candidate = None
    if prerequisites["P16-06"] == "ACCEPTED":
        candidate = verify_round2_manifest(root, root / MANIFEST)

    return {
        "schema_version": 1,
        "task": TASK,
        "status": "BLOCKED" if blockers else "PREREQUISITES_MET",
        "decision_status": decision_status,
        "prerequisites": prerequisites,
        "blockers": blockers,
        "candidate": candidate,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="prototype directory")
    args = parser.parse_args(argv)
    try:
        report = validate(args.root)
    except (OSError, ValueError, KeyError, TypeError, ElementTree.ParseError) as error:
        print(f"V2 prerequisite verification failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
