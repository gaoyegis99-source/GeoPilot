import importlib
import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[2]
BOARD = Path("plans-round2/06_PROGRESS/TASK_BOARD.md")
INVENTORY = BOARD.with_name("task_inventory.json")
MANIFEST = Path("artifacts/reports/final/round2-delivery-manifest.json")
DEPENDENCIES = {
    "P13-08": "ACCEPTED",
    "P14-08": "ACCEPTED",
    "P15-08": "ACCEPTED",
    "P16-01": "ACCEPTED",
    "P16-02": "ACCEPTED",
    "P16-04": "BLOCKED",
    "P16-05": "BLOCKED",
    "P16-06": "ACCEPTED",
}
CANDIDATE = {
    "verification": "matched",
    "source_commit": "a" * 40,
    "evidence_commit": "b" * 40,
    "source_files": 100,
    "evidence_files": 10,
    "validation_suites": {"backend": {"passed": 100}},
}


def set_task(root, task, status, evidence):
    board = root / BOARD
    lines = board.read_text(encoding="utf-8").splitlines()
    row, = (i for i, line in enumerate(lines) if line.startswith(f"| {task} |"))
    fields = lines[row].split("|")
    fields[-3:-1] = [f" {status} ", f" {evidence} "]
    lines[row] = "|".join(fields)
    board.write_text("\n".join(lines) + "\n", encoding="utf-8")


def accept_task(root, task):
    evidence = root / "evidence" / f"{task}.md"
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text("# Result\n", encoding="utf-8")
    set_task(root, task, "ACCEPTED", f"[Evidence](../../evidence/{evidence.name}#result)")


@pytest.fixture
def release_root(tmp_path):
    (tmp_path / BOARD.parent).mkdir(parents=True)
    for path in (BOARD, INVENTORY):
        shutil.copyfile(ROOT / path, tmp_path / path)
    for task, status in DEPENDENCIES.items():
        if status == "ACCEPTED":
            accept_task(tmp_path, task)
        else:
            set_task(tmp_path, task, status, "Awaiting acceptance")
    set_task(tmp_path, "P16-03", "NOT_APPLICABLE", "Participant browser study retired")
    set_task(tmp_path, "P16-07", "BLOCKED", "Awaiting dependencies")
    return tmp_path


@pytest.fixture
def readiness(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/quality"))
    module = importlib.import_module("validate_release_readiness")
    verify = Mock(return_value=CANDIDATE)
    monkeypatch.setattr(module, "verify_round2_manifest", verify)
    return module, verify


def test_blocked_prerequisites_still_verify_accepted_candidate(release_root, readiness, capsys):
    module, verify = readiness
    report = module.validate(release_root)
    assert report == {
        "schema_version": 1,
        "task": "P16-07",
        "status": "BLOCKED",
        "decision_status": "BLOCKED",
        "prerequisites": DEPENDENCIES,
        "blockers": ["P16-04", "P16-05"],
        "candidate": CANDIDATE,
    }
    assert list(report["prerequisites"]) == list(DEPENDENCIES)
    verify.assert_called_once_with(release_root, release_root / MANIFEST)

    assert module.main(["--root", str(release_root)]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == report
    assert not output.err


def test_prerequisites_met_preserves_decision_status(release_root, readiness, capsys):
    module, verify = readiness
    for task in ("P16-04", "P16-05"):
        accept_task(release_root, task)
    set_task(release_root, "P16-07", "TODO", "Awaiting release decision")

    assert module.main(["--root", str(release_root)]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "schema_version": 1,
        "task": "P16-07",
        "status": "PREREQUISITES_MET",
        "decision_status": "TODO",
        "prerequisites": dict.fromkeys(DEPENDENCIES, "ACCEPTED"),
        "blockers": [],
        "candidate": CANDIDATE,
    }
    assert not output.err
    verify.assert_called_once_with(release_root, release_root / MANIFEST)


def test_unaccepted_candidate_skips_manifest_verification(release_root, readiness):
    module, verify = readiness
    set_task(release_root, "P16-06", "REVIEW", "[Draft](../../missing.md)")

    report = module.validate(release_root)

    assert report["candidate"] is None
    assert report["prerequisites"]["P16-06"] == "REVIEW"
    assert report["blockers"] == ["P16-04", "P16-05", "P16-06"]
    verify.assert_not_called()


@pytest.mark.parametrize("problem", ["missing", "invalid status"])
def test_rejects_invalid_board(release_root, readiness, capsys, problem):
    module, verify = readiness
    if problem == "missing":
        (release_root / BOARD).unlink()
    else:
        set_task(release_root, "P16-05", "UNKNOWN", "Awaiting acceptance")

    with pytest.raises((ValueError, OSError)):
        module.validate(release_root)
    assert module.main(["--root", str(release_root)]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert output.err and "Traceback" not in output.err
    verify.assert_not_called()


@pytest.mark.parametrize("evidence", ["No local link", "[Missing](../../missing.md)"])
def test_rejects_invalid_accepted_dependency_evidence(release_root, readiness, evidence):
    module, verify = readiness
    set_task(release_root, "P14-08", "ACCEPTED", evidence)

    with pytest.raises(ValueError, match="P14-08"):
        module.validate(release_root)
    verify.assert_not_called()


@pytest.mark.parametrize("error_type", [ValueError, OSError])
def test_candidate_verification_failure_is_invalid(release_root, readiness, capsys, error_type):
    module, verify = readiness
    verify.side_effect = error_type("candidate verification failed")

    with pytest.raises(error_type, match="candidate verification failed"):
        module.validate(release_root)
    assert module.main(["--root", str(release_root)]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "candidate verification failed" in output.err


def test_rejects_accepted_decision_with_unmet_prerequisites(release_root, readiness):
    module, _ = readiness
    accept_task(release_root, "P16-07")

    with pytest.raises(ValueError, match="P16-07"):
        module.validate(release_root)


def test_rejects_accepted_decision_without_linked_evidence(release_root, readiness):
    module, verify = readiness
    for task in ("P16-04", "P16-05"):
        accept_task(release_root, task)
    set_task(release_root, "P16-07", "ACCEPTED", "Release approved")

    with pytest.raises(ValueError, match="P16-07.*missing local evidence"):
        module.validate(release_root)
    verify.assert_not_called()
