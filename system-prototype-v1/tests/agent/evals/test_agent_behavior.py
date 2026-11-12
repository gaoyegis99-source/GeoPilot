import json
import os
from pathlib import Path

import pytest

from eval_support import LOCK, EvaluationProject, evaluate, load_cases, prompt_lock, score_report, write_new_json


CASES = load_cases()


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    project = EvaluationProject(tmp_path_factory.mktemp("agent-eval"))
    rows = [evaluate(case, project) for case in CASES]
    lock = prompt_lock(rows)
    report = score_report(rows, {"kind": "mock", "version": "fixed-responses-v1",
                                 "responses_dataset_sha256": lock["dataset_sha256"]}, lock)
    if configured := os.environ.get("GEOPILOT_AGENT_EVAL_REPORT"):
        write_new_json(Path(configured), report)
    return {row["id"]: row for row in rows}, lock


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_fixed_mock_behavior(case, results):
    rows, _ = results
    row = rows[case["id"]]
    assert row["pass"], [item for item in row["checks"] if not item["pass"]]
    observed = row["observed"]
    if observed["provider_calls"]:
        assert observed["prompt_sha256"] == observed["request_sha256"]
    if observed.get("draft_status") is not None:
        assert observed["draft_status"] == "draft" and observed["gate"]


def test_model_and_feedback_prompts_match_frozen_digests(results):
    _, actual = results
    expected = json.loads(LOCK.read_bytes())
    assert actual == expected
