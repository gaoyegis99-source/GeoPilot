"""Check that the GitHub workflow keeps the P12 quality gates separate."""

import argparse
import json
import re
from pathlib import Path

import yaml
from yaml.constructor import ConstructorError


WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/ci.yml"
VERIFY_BRANCH = "round2/ci-verify"
GIS_GATE = (
    "github.event_name == 'workflow_dispatch' || "
    "(github.event_name == 'push' && github.ref == 'refs/heads/round2/ci-verify')"
)
ACTION = re.compile(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}")
COMMANDS = {
    "python": (
        "python -m pip install --only-binary=:all: -r requirements-test.txt",
        "python -m compileall",
        "from geopilot.api.server import create_local_app",
        "python scripts/quality/validate_ci_matrix.py",
        "python scripts/quality/validate_config_schemas.py",
        "python -m pytest",
        "tests/assets",
        "tests/contracts",
        "tests/scheduler",
        "tests/llm/test_data_boundary.py",
        "--junitxml=",
    ),
    "frontend": (
        "npm install --global npm@10.9.8",
        "npm ci --include=dev",
        "npm test -- --run",
        "--reporter=junit",
        "npm run build",
    ),
    "gis": (
        "python -m pip install --only-binary=:all: -r requirements-test.txt",
        "https://qgis.org/ubuntu-ltr noble main",
        "qgis=1:3.44.15+40noble",
        'test -x "$GEOPILOT_QGIS_PROCESS"',
        '"$GEOPILOT_QGIS_PROCESS" --version',
        "native:buffer",
        "python -m pytest",
        "tests/smoke/test_qgis_smoke.py",
        "tests/golden",
        "tests/gis",
        "tests/methods",
        "--junitxml=",
        'report.iter("skipped")',
    ),
}


class WorkflowLoader(yaml.BaseLoader):
    """Retain literal keys such as `on` and reject duplicate declarations."""

    def construct_mapping(self, node, deep=False):
        result = super().construct_mapping(node, deep)
        if len(result) != len(node.value):
            raise ConstructorError(None, None, "duplicate workflow key", node.start_mark)
        return result


def mapping(value):
    return value if isinstance(value, dict) else {}


def validate_job(name, job, findings):
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps or any(not isinstance(step, dict) for step in steps):
        findings.append(f"{name}: expected a non-empty list of step mappings")
        return
    if "permissions" in job and job["permissions"] not in ({}, {"contents": "read"}):
        findings.append(f"{name}: job may not grant extra permissions")
    if "continue-on-error" in job or any("continue-on-error" in step for step in steps):
        findings.append(f"{name}: job and steps must surface failures")
    for step in steps:
        if "uses" in step and not ACTION.fullmatch(str(step["uses"])):
            findings.append(f"{name}: actions must be pinned to a full commit SHA")
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            if mapping(step.get("with")).get("persist-credentials") != "false":
                findings.append(f"{name}: checkout must disable credential persistence")
    if name not in COMMANDS:
        return
    if job.get("runs-on") != "ubuntu-24.04":
        findings.append(f"{name}: expected the Ubuntu 24.04 runner")
    runtime = ("node", "22.23.1") if name == "frontend" else ("python", "3.13.13")
    actions = {str(step.get("uses", "")).split("@")[0]: step for step in steps}
    version = mapping(actions.get(f"actions/setup-{runtime[0]}", {}).get("with"))
    if version.get(f"{runtime[0]}-version") != runtime[1]:
        findings.append(f"{name}: expected {runtime[0]} {runtime[1]}")
    if "actions/checkout" not in actions:
        findings.append(f"{name}: missing checkout")
    upload = actions.get("actions/upload-artifact", {})
    if upload.get("if") != "always()" or "-junit.xml" not in str(mapping(upload.get("with")).get("path", "")):
        findings.append(f"{name}: JUnit evidence must upload even if testing fails")
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    for expected in COMMANDS[name]:
        if expected not in commands:
            findings.append(f"{name}: missing command or argument {expected!r}")
    if name == "gis":
        if job.get("if") != GIS_GATE:
            findings.append("gis: expected manual or exact verification-branch execution gate")
        env = mapping(job.get("env"))
        if env.get("QT_QPA_PLATFORM") != "offscreen" or env.get("GEOPILOT_QGIS_PROCESS") != "/usr/bin/qgis_process":
            findings.append("gis: expected the native offscreen QGIS executable")
    elif job.get("if"):
        findings.append(f"{name}: PR job must run without an event gate")
    if name == "python" and ("qgis_process" in commands or "sudo apt" in commands):
        findings.append("python: the PR test layer may not install or run QGIS")


def validate_workflow(document):
    if not isinstance(document, dict):
        return [], [], ["workflow must be a mapping"]
    findings = []
    events = mapping(document.get("on"))
    required_events = {"pull_request", "push", "workflow_dispatch"}
    if required_events - events.keys() or "pull_request_target" in events:
        findings.append("workflow requires pull_request, push and workflow_dispatch without pull_request_target")
    branches = mapping(events.get("push")).get("branches")
    if not isinstance(branches, list) or VERIFY_BRANCH not in branches:
        findings.append(f"push: missing exact verification branch {VERIFY_BRANCH!r}")
    if document.get("permissions") != {"contents": "read"}:
        findings.append("workflow permissions must be contents: read")
    if mapping(mapping(document.get("defaults")).get("run")).get("working-directory") != "system-prototype-v1":
        findings.append("workflow: expected system-prototype-v1 working directory")
    jobs = mapping(document.get("jobs"))
    if COMMANDS.keys() - jobs.keys():
        findings.append("workflow requires python, frontend and gis jobs")
    for name, job in jobs.items():
        validate_job(name, mapping(job), findings)
    frontend_defaults = mapping(mapping(mapping(jobs.get("frontend")).get("defaults")).get("run"))
    if frontend_defaults.get("working-directory") != "system-prototype-v1/frontend":
        findings.append("frontend: expected frontend working directory")
    return list(events), list(jobs), findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, default=WORKFLOW)
    args = parser.parse_args()
    try:
        document = yaml.load(args.workflow.read_text(encoding="utf-8"), Loader=WorkflowLoader)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(json.dumps({"status": "FAIL", "findings": [str(error)]}, indent=2))
        return 2

    events, jobs, findings = validate_workflow(document)
    print(json.dumps({
        "status": "FAIL" if findings else "PASS",
        "events": events,
        "jobs": jobs,
        "findings": findings,
    }, indent=2))
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
