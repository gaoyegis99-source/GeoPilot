import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/quality/validate_ci_matrix.py"
WORKFLOW = ROOT.parent / ".github/workflows/ci.yml"


def invoke(path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--workflow", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stderr == ""
    return result.returncode, json.loads(result.stdout)


def test_checked_in_workflow_preserves_pr_and_real_qgis_layers():
    code, report = invoke(WORKFLOW)

    assert code == 0
    assert report == {
        "status": "PASS",
        "events": ["pull_request", "push", "workflow_dispatch"],
        "jobs": ["python", "frontend", "gis"],
        "findings": [],
    }


@pytest.mark.parametrize(
    ("original", "replacement", "finding"),
    [
        ("  pull_request:", "  pull_request_target:", "without pull_request_target"),
        ("main, round2/ci-verify", "main", "missing exact verification branch"),
        ("contents: read", "contents: write", "permissions must be contents: read"),
        ("actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803", "actions/checkout@v6",
         "actions must be pinned"),
        ("persist-credentials: false", "persist-credentials: true", "disable credential persistence"),
        ('python-version: "3.13.13"', 'python-version: "3.13"', "expected python 3.13.13"),
        ("python scripts/quality/validate_config_schemas.py", "echo skipped-schema-validation",
         "missing command or argument 'python scripts/quality/validate_config_schemas.py'"),
        ("tests/assets tests/contracts tests/scheduler", "tests/assets tests/contracts",
         "missing command or argument 'tests/scheduler'"),
        ("npm ci --include=dev", "npm install", "missing command or argument 'npm ci --include=dev'"),
        ("npm run build", "npm run lint", "missing command or argument 'npm run build'"),
        ("github.event_name == 'workflow_dispatch' ||", "github.event_name == 'pull_request' ||",
         "expected manual or exact verification-branch execution gate"),
        ("GEOPILOT_QGIS_PROCESS: /usr/bin/qgis_process", "GEOPILOT_QGIS_PROCESS: /usr/bin/true",
         "expected the native offscreen QGIS executable"),
        ('report.iter("skipped")', 'report.iter("ignored")', "missing command or argument"),
        ("        if: always()", "        if: success()", "JUnit evidence must upload"),
    ],
)
def test_rejects_policy_drift(tmp_path, original, replacement, finding):
    text = WORKFLOW.read_text(encoding="utf-8")
    assert original in text
    path = tmp_path / "ci.yml"
    path.write_text(text.replace(original, replacement, 1), encoding="utf-8")

    code, report = invoke(path)

    assert code == 1
    assert report["status"] == "FAIL"
    assert any(finding in item for item in report["findings"])


@pytest.mark.parametrize(
    ("text", "expected_exit", "finding"),
    [
        ("[]\n", 1, "workflow must be a mapping"),
        ("jobs:\n  python:\n    steps: [not-a-step]\n", 1, "step mappings"),
        ("on:\n  push: [\n", 2, "while parsing"),
        ("on: push\non: pull_request\n", 2, "duplicate workflow key"),
    ],
)
def test_reports_empty_malformed_and_duplicate_inputs(tmp_path, text, expected_exit, finding):
    path = tmp_path / "ci.yml"
    path.write_text(text, encoding="utf-8")

    code, report = invoke(path)

    assert code == expected_exit
    assert report["status"] == "FAIL"
    assert any(finding in item for item in report["findings"])


def test_missing_workflow_is_an_input_error(tmp_path):
    code, report = invoke(tmp_path / "missing.yml")

    assert code == 2
    assert report["status"] == "FAIL"
    assert "missing.yml" in report["findings"][0]
