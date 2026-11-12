"""Verify the P12 release gate against the task board and archived evidence."""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[2]
PLAN = Path("plans-round2")
BOARD = PLAN / "06_PROGRESS/TASK_BOARD.md"
INVENTORY = BOARD.with_name("task_inventory.json")
FINDINGS = PLAN / "01_AUDIT/P12_FINDINGS_REGISTER.md"
AUDIT = Path("docs/round2/audit")
REPORT = AUDIT / "P12_FINAL_AUDIT.md"
RESULTS = Path("artifacts/test-results/round2")
HOSTED = RESULTS / "ci-hosted"
RUN = HOSTED / "run-43148595202.json"
TASK = re.compile(r"P\d+-\d{2}")
AUD = re.compile(r"AUD-\d{2}")
LINK = re.compile(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]*)?\)")
STATES = {"TODO", "DOING", "REVIEW", "ACCEPTED", "REWORK", "BLOCKED", "NOT_APPLICABLE"}
DISPOSITIONS = {"已修复", "真实缺陷", "设计边界", "未验证", "暂缓"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def rows(path, id_pattern, columns):
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        fields = [field.strip() for field in line.strip("|").split("|")]
        if not id_pattern.fullmatch(fields[0]):
            continue
        identifier = fields[0]
        require(len(fields) == columns and all(fields), f"{path.name}: invalid row for {identifier}")
        require(identifier not in entries, f"{path.name}: duplicate {identifier}")
        entries[identifier] = fields[1:]
    return entries


def check_links(root, source, cell, identifier):
    links = LINK.findall(cell)
    require(links, f"{identifier}: missing local evidence link")
    for link in links:
        target = (root / source.parent / link).resolve()
        require(target.is_relative_to(root) and target.is_file(), f"{identifier}: invalid evidence {link}")


def check_board(root, require_released):
    inventory = json.loads((root / INVENTORY).read_bytes())
    planned = {task["id"]: task["phase"] for task in inventory["tasks"]}
    require(len(planned) == len(inventory["tasks"]) == inventory["task_count"], "Invalid task inventory count")
    require(dict(Counter(planned.values())) == inventory["phase_counts"], "Invalid phase counts")
    board = rows(root / BOARD, TASK, 7)
    require(board.keys() == planned.keys(), "Task board and inventory IDs differ")
    for identifier, (phase, _, _, _, status, evidence) in board.items():
        require(phase == planned[identifier] and status in STATES, f"{identifier}: invalid phase or status")
        if phase == "P12" and (identifier != "P12-12" or require_released):
            require(status == "ACCEPTED", f"{identifier}: gate requires ACCEPTED, found {status}")
        if phase == "P12" and status == "ACCEPTED":
            check_links(root, BOARD, evidence, identifier)
    return board["P12-12"][4] == "ACCEPTED"


def check_findings(root):
    expected = {f"AUD-{number:02}" for number in range(1, 13)}
    register = rows(root / FINDINGS, AUD, 6)
    report = rows(root / REPORT, AUD, 6)
    require(register.keys() == report.keys() == expected, "Audit requires exactly AUD-01 through AUD-12")
    for identifier, (priority, disposition, _, evidence, _) in report.items():
        require(priority == register[identifier][0], f"{identifier}: priority differs from register")
        require(disposition in DISPOSITIONS, f"{identifier}: unknown disposition")
        check_links(root, REPORT, evidence, identifier)
    return dict(sorted(Counter(fields[1] for fields in report.values()).items()))


def junit(root, path, count, unique=True):
    report = ElementTree.parse(root / path)
    cases = list(report.iter("testcase"))
    identities = {(case.attrib["classname"], case.attrib["name"]) for case in cases}
    require(len(cases) == count, f"{path.name}: expected {count} test results")
    require(not unique or len(identities) == count, f"{path.name}: duplicate pytest case")
    for tag in ("failure", "error", "skipped"):
        require(not any(report.iter(tag)), f"{path.name}: contains {tag} results")
    return identities


def check_fixed_cases(root, commit, gis):
    mapping = json.loads((root / AUDIT / "p12-07-mapping.json").read_bytes())
    require(mapping["test_commit"] == commit and mapping["pytest_exit_code"] == 0, "Invalid fixed-case provenance")
    for path, digest in mapping["evidence"].items():
        evidence = (root / path).resolve()
        require(evidence.is_relative_to(root), "Fixed-case evidence escapes the prototype")
        require(hashlib.sha256(evidence.read_bytes()).hexdigest() == digest, f"Fixed-case hash mismatch: {path}")
    fixed = mapping["fixed_cases"]
    require(set(fixed) == {f"T{number:02}" for number in range(1, 17)}, "Missing or unexpected fixed-case ID")
    observed = set()
    for identifier, item in fixed.items():
        require(item["outcome"] == "PASS" and item["nodes"], f"{identifier}: no passing evidence")
        for node in item["nodes"]:
            path, name = node["id"].split("::", 1)
            case = (path.removesuffix(".py").replace("/", "."), name)
            require(node["outcome"] == "PASS" and case in gis, f"{identifier}: unmapped {node['id']}")
            observed.add(case)
    require(len(observed) == mapping["mapped_unique_nodes"] == 82, "Unexpected fixed-case mapped node count")


def validate(root, require_released=False):
    released = check_board(root, require_released)
    dispositions = check_findings(root)
    commit = json.loads((root / AUDIT / "baseline.json").read_bytes())["commit"]
    require(re.fullmatch(r"[0-9a-f]{40}", commit), "Invalid V1 commit SHA")
    for name in ("p12-05.md", "p12-06.md", "p12-07.md"):
        require(commit in (root / AUDIT / name).read_text(encoding="utf-8"), f"{name}: missing V1 commit")
    junit(root, RESULTS / "backend-rerun.xml", 1670)
    junit(root, RESULTS / "frontend-vitest.xml", 68, unique=False)
    gis = junit(root, RESULTS / "gis-regression.xml", 476)
    check_fixed_cases(root, commit, gis)
    run = json.loads((root / RUN).read_bytes())
    require(run["status"] == "completed" and run["conclusion"] == "success", "Hosted CI did not succeed")
    jobs = {job["name"]: job["conclusion"] for job in run["jobs"]}
    require(len(run["jobs"]) == 3 and jobs == {
        "Python and configuration": "success", "Frontend test and build": "success",
        "Real QGIS regression": "success",
    }, "Hosted CI lacks three successful jobs")
    junit(root, HOSTED / "python/python-junit.xml", 194)
    junit(root, HOSTED / "frontend/frontend-junit.xml", 68, unique=False)
    hosted_gis = junit(root, HOSTED / "gis/gis-junit.xml", 479)
    require(gis <= hosted_gis, "Hosted GIS misses local fixed-case regression nodes")
    return {
        "status": "PASS", "release_ready": True, "released": released, "v1_commit": commit,
        "prerequisites_accepted": 11, "audit_dispositions": dispositions,
        "v1_passed": {"backend": 1670, "frontend": 68, "gis": 476, "fixed_cases": 16},
        "hosted_ci": {"run": run["databaseId"], "head": run["headSha"], "jobs": jobs},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="prototype directory")
    parser.add_argument("--require-released", action="store_true", help="require final P12-12 ACCEPTED status")
    args = parser.parse_args()
    try:
        report = validate(args.root.resolve(strict=True), args.require_released)
    except (OSError, ValueError, KeyError, TypeError, ElementTree.ParseError) as error:
        parser.exit(1, f"P12 gate failed: {error}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
