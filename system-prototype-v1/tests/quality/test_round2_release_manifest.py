import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE = "system-prototype-v1"
GENERATOR = "artifacts/reports/final/delivery_manifest.py"
MANIFEST = "artifacts/reports/final/round2-delivery-manifest.json"
RECEIPT = "artifacts/reports/final/round2-validation.json"
VERIFIER = "scripts/quality/verify_delivery_manifest.py"
VALIDATOR = "scripts/quality/run_round2_validation.py"
REPORT_DIR = f"{PROTOTYPE}/artifacts/reports/final/round2-validation"
REPRODUCTION = f"{PROTOTYPE}/docs/release/reproduction.md"
FIXED_MAPPING = "docs/round2/audit/p12-07-mapping.json"
FIXED_NODES = [
    f'<testcase classname="tests.test_fixed" name="test_case[T{index:02}]" />' for index in range(1, 17)
]


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _commit(repo):
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    return _git(repo, "rev-parse", "HEAD")


def _write(directory, relative, content):
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _invoke(root, command, *args):
    return subprocess.run([sys.executable, "-B", str(root / command), *args], cwd=root,
                          text=True, capture_output=True, check=False)


def _generate(root, source, evidence, *options):
    return _invoke(root, GENERATOR, "--round2", "--source-ref", source, "--evidence-ref", evidence, *options)


@pytest.fixture
def delivery(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    root = repo / PROTOTYPE
    files = {
        "README.md": "# 复现入口\n", "src/engine.py": "print('sealed')\n", "config/method.yaml": "id: fixture\n",
        "frontend/package-lock.json": "{}\n", "tests/fixture.json": "{}\n", "benchmarks/profile.yaml": "seed: 1\n",
        "artifacts/schemas/method.schema.json": "{}\n", "pyproject.toml": "# fixture\n",
        "requirements.txt": "", "requirements-test.txt": "",
        "plans/fixture.md": "", "plans-round2/fixture.md": "", "artifacts/test-results/round2/fixture.log": "",
        "docs/method-authoring/manual-registration.yaml": "{}\n", "docs/traceability/requirements-matrix.md": "",
        "docs/acceptance/FIXED_CASES.md": "", "docs/acceptance/traceability-follow-up.md": "",
        "docs/release/FINAL_ACCEPTANCE.md": "", "docs/round2/user-research/observations.blank.json": "{}\n",
        "docs/round2/user-research/observations.synthetic.json": "{}\n",
    }
    for path, content in files.items():
        _write(root, path, content)
    _write(repo, ".gitignore", ".environments/\n")
    _write(root, FIXED_MAPPING, json.dumps({
        "fixed_cases": {
            case_id: {"nodes": [{"id": f"tests/test_fixed.py::test_case[{case_id}]"}]}
            for case_id in (f"T{index:02}" for index in range(1, 17))
        }
    }))
    _write(repo, ".github/workflows/ci.yml", "name: fixture\n")
    for tool in (GENERATOR, VERIFIER, VALIDATOR, "scripts/quality/round2_release_manifest.py",
                 "scripts/quality/build_source_inventory.py", "scripts/quality/check_doc_evidence_links.py"):
        destination = root / tool
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / tool, destination)
    source = _commit(repo)
    suites = []
    for name in ("backend", "frontend", "fixed_gis"):
        junit, log = f"{REPORT_DIR}/{name}.xml", f"{REPORT_DIR}/{name}.log"
        _write(repo, log, f"commit {source}\ncwd fixture\ncommand fixture {name}\nfixture validation\nexit 0\n")
        cases = "".join(FIXED_NODES) if name == "fixed_gis" else '<testcase classname="tests.test_backend" name="observed"/>'
        if name == "backend":
            cases += '<testcase classname="tests.test_backend" name="optional"><skipped /></testcase>'
        _write(repo, junit, f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
        suite = {
            "name": name, "command": ["fixture", name], "environment": {"runtime": "fixture-1"},
            "exit_code": 0, "junit": junit, "log": log,
        }
        if name == "backend":
            collection = f"{REPORT_DIR}/backend-collection.log"
            _write(repo, collection, f"commit {source}\ncwd fixture\ncommand fixture --collect-only\n"
                   "tests/test_backend.py::observed\ntests/test_backend.py::optional\nexit 0\n")
            suite["collection"] = {"log": collection, "command": ["fixture", "--collect-only"]}
        suites.append(suite)
    _write(root, "docs/release/capacity.json", '{"status":"observed"}\n')
    _write(repo, REPRODUCTION, "# Offline\n\n[入口](../../README.md#复现入口)\n[容量](capacity.json)\n")
    receipt = {
        "schema_version": 1, "source_commit": source, "suites": suites,
        "evidence": [f"{PROTOTYPE}/docs/release/capacity.json"], "reproduction": [REPRODUCTION],
    }
    _write(root, RECEIPT, json.dumps(receipt, ensure_ascii=False))
    evidence = _commit(repo)
    return repo, root, source, evidence, receipt


def test_generate_and_verify_exact_source_and_evidence(delivery):
    _, root, source, evidence, _ = delivery
    first = _generate(root, source, evidence)
    assert first.returncode == 0, first.stderr
    assert _generate(root, source, evidence).stdout == first.stdout
    assert _generate(root, source, evidence, "--write").returncode == 0
    manifest = json.loads(first.stdout)
    assert manifest["source_commit"] == source and manifest["evidence_commit"] == evidence
    assert manifest["validation_suites"][0]["counts"] == {
        "tests": 2, "passed": 1, "skipped": 1, "errors": 0, "failures": 0,
    }
    assert {item["path"] for item in manifest["source_files"]} >= {
        f"{PROTOTYPE}/frontend/package-lock.json", f"{PROTOTYPE}/tests/fixture.json", ".github/workflows/ci.yml",
        f"{PROTOTYPE}/{FIXED_MAPPING}",
        f"{PROTOTYPE}/artifacts/test-results/round2/fixture.log",
    }
    assert manifest["fixed_gis_mapping"] == f"{PROTOTYPE}/{FIXED_MAPPING}"
    assert f"{PROTOTYPE}/docs/release/capacity.json" in {item["path"] for item in manifest["evidence"]}
    (root / "src/engine.py").write_text("dirty local file\n", encoding="utf-8")
    checked = _invoke(root, VERIFIER, "--round2-manifest", MANIFEST)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["verification"] == "matched"


def test_rejects_source_drift_in_evidence_commit(delivery):
    repo, root, source, _, _ = delivery
    _write(root, "src/engine.py", "changed release source\n")
    result = _generate(root, source, _commit(repo))
    assert result.returncode == 1
    assert "different release source" in result.stderr


def test_pins_non_code_regression_inputs(delivery):
    repo, root, source, _, _ = delivery
    _write(root, "docs/method-authoring/manual-registration.yaml", "changed template\n")
    result = _generate(root, source, _commit(repo))
    assert result.returncode == 1 and "different release source" in result.stderr


def test_archive_is_deterministic_and_verifies_without_a_git_checkout(delivery, tmp_path):
    _, root, source, evidence, _ = delivery
    archives = [tmp_path / f"round2-{number}.tar.gz" for number in (1, 2)]
    for archive in archives:
        result = _generate(root, source, evidence, "--archive", str(archive))
        assert result.returncode == 0, result.stderr
    assert archives[0].read_bytes() == archives[1].read_bytes()
    prior = archives[0].read_bytes()
    assert _generate(root, source, evidence, "--archive", str(archives[0])).returncode == 1
    assert archives[0].read_bytes() == prior
    extracted = tmp_path / "offline"
    with tarfile.open(archives[0]) as archive:
        assert all(member.isfile() and ".git/" not in member.name for member in archive)
        archive.extractall(extracted, filter="data")
    prototype = extracted / PROTOTYPE
    checked = _invoke(prototype, VERIFIER, "--round2-manifest", MANIFEST, "--offline-root", str(extracted))
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["verification"] == "matched"
    manifest_path = prototype / MANIFEST
    saved = manifest_path.read_text(encoding="utf-8")
    tampered = json.loads(saved)
    tampered["validation_suites"][0]["counts"]["passed"] = 5
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
    checked = _invoke(prototype, VERIFIER, "--round2-manifest", MANIFEST, "--offline-root", str(extracted))
    assert checked.returncode == 1 and "suite counts differ" in checked.stderr
    manifest_path.write_text(saved, encoding="utf-8")
    (prototype / "src/engine.py").write_text("altered release source\n", encoding="utf-8")
    checked = _invoke(prototype, VERIFIER, "--round2-manifest", MANIFEST, "--offline-root", str(extracted))
    assert checked.returncode == 1 and "Extracted delivery bytes differ" in checked.stderr


@pytest.mark.parametrize(("relative", "content", "diagnosis"), [
    (f"{REPORT_DIR}/backend.log", "commit missing\nexit 0\n", "source commit"),
    (f"{REPORT_DIR}/backend.xml", '<testsuite><testcase name="unrelated" /></testsuite>', "collected pytest nodes"),
    (f"{REPORT_DIR}/frontend.xml", '<testsuites><testsuite tests="2"><testcase name="one"/></testsuite></testsuites>', "aggregate"),
    (f"{REPORT_DIR}/fixed_gis.xml", '<testsuite><testcase name="fixed"><skipped/></testcase></testsuite>', "incomplete fixed GIS"),
    (REPRODUCTION, "# Offline\n[missing](absent.md)\n", "committed regular file"),
    (REPRODUCTION, "# Offline\n[anchor](../../README.md#不存在)\n", "Missing reproduction anchor"),
])
def test_requires_checked_log_report_and_reproduction_links(delivery, relative, content, diagnosis):
    repo, root, source, _, _ = delivery
    _write(repo, relative, content)
    result = _generate(root, source, _commit(repo))
    assert result.returncode == 1
    assert diagnosis in result.stderr


@pytest.mark.parametrize(("old", "new", "diagnosis"), [
    ("command fixture backend", "command fixture different", "does not match the recorded command"),
    ("exit 0\n", "exit 0\nexit 1\n", "does not bind the successful suite"),
])
def test_rejects_unrelated_or_unsuccessful_command_logs(delivery, old, new, diagnosis):
    repo, root, source, _, _ = delivery
    path = f"{REPORT_DIR}/backend.log"
    _write(repo, path, (repo / path).read_text(encoding="utf-8").replace(old, new))
    result = _generate(root, source, _commit(repo))
    assert result.returncode == 1 and diagnosis in result.stderr


@pytest.mark.parametrize("last_node", ['', FIXED_NODES[-1] * 2])
def test_fixed_gis_junit_requires_each_mapped_node_once(delivery, last_node):
    repo, root, source, _, _ = delivery
    cases = "".join(FIXED_NODES[:-1]) + last_node
    _write(repo, f"{REPORT_DIR}/fixed_gis.xml", f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    result = _generate(root, source, _commit(repo))
    assert result.returncode == 1
    assert "Missing or duplicate fixed GIS node for T16" in result.stderr


def test_refuses_unsafe_receipt_and_tampered_manifest(delivery):
    repo, root, source, evidence, receipt = delivery
    valid = _generate(root, source, evidence)
    assert valid.returncode == 0, valid.stderr
    manifest = json.loads(valid.stdout)
    manifest["schema_version"] = True
    _write(root, MANIFEST, json.dumps(manifest))
    result = _invoke(root, VERIFIER, "--round2-manifest", MANIFEST)
    assert result.returncode == 1 and "second-round delivery manifest" in result.stderr
    receipt["evidence"] = ["../outside.json"]
    _write(root, RECEIPT, json.dumps(receipt))
    result = _generate(root, source, _commit(repo))
    assert result.returncode == 1 and "Unsafe" in result.stderr


@pytest.fixture
def validation_checkout(delivery, tmp_path, monkeypatch):
    repo, _, source, _, _ = delivery
    checkout = tmp_path / "candidate"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(checkout), source)
    tools = tmp_path / "tools"
    tools.mkdir()
    fixture = """
import os
import sys
from pathlib import Path
from xml.etree import ElementTree

name = Path(sys.argv[0]).name
args = sys.argv[1:]
if "--version" in args:
    print(name, "1.0")
    sys.exit(0)
if args[:4] == ["-m", "pip", "list", "--format=json"]:
    print("[]")
    sys.exit(0)

if name == "fixture-python":
    assert os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert "PYTEST_ADDOPTS" not in os.environ
    assert Path(os.environ["GEOPILOT_QGIS_PROCESS"]).name == "fixture-qgis"
    assert os.environ["GEOPILOT_BENCH_QGIS"] == os.environ["GEOPILOT_QGIS_PROCESS"]
    if "--collect-only" in args:
        print("tests/test_backend.py::test_all")
        sys.exit(0)
    nodes = [arg for arg in args if arg.startswith("tests/")]
    if not nodes:
        assert "tests" in args
        nodes = ["tests/test_backend.py::test_all"]
    output = next(arg.removeprefix("--junitxml=") for arg in args if arg.startswith("--junitxml="))
elif name == "npm" and args[0] == "test":
    nodes = ["tests/test_frontend.py::test_render"]
    output = next(arg.removeprefix("--outputFile.junit=") for arg in args if arg.startswith("--outputFile.junit="))
else:
    assert name == "npm" and args[0] in {"ci", "run"}
    sys.exit(0)

if os.environ.get("RELEASE_FIXTURE_FAIL") == Path(output).stem:
    sys.exit(5)
if os.environ.get("RELEASE_FIXTURE_MISSING") == Path(output).stem:
    nodes = nodes[:-1]
suite = ElementTree.Element("testsuite", tests=str(len(nodes)))
for node in nodes:
    path, test = node.split("::", 1)
    ElementTree.SubElement(suite, "testcase", classname=path.removesuffix(".py").replace("/", "."), name=test)
ElementTree.ElementTree(suite).write(output)
"""
    for name in ("fixture-python", "fixture-qgis", "node", "npm"):
        executable = tools / name
        executable.write_text(f"#!{sys.executable}\n" + fixture, encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(tools), os.environ["PATH"])))
    monkeypatch.setenv("PYTEST_ADDOPTS", "--intentional-filter")
    root = checkout / PROTOTYPE
    output = tmp_path / "result"
    options = (
        "--commit", source, "--output", str(output), "--qgis-process", str(tools / "fixture-qgis"),
        "--python", str(tools / "fixture-python"), "--reproduction", REPRODUCTION,
    )
    return checkout, root, output, options


def test_validates_from_detached_commit_and_produces_usable_receipt(delivery, validation_checkout):
    repo, _, source, _, _ = delivery
    checkout, root, output, options = validation_checkout
    result = _invoke(root, VALIDATOR, *options)
    assert result.returncode == 0, result.stderr
    assert not _git(checkout, "status", "--short")
    for path in output.iterdir():
        target = root / "artifacts/reports/final" / path.name
        if path.is_dir():
            shutil.copytree(path, target)
        else:
            shutil.copyfile(path, target)
    for path in (REPRODUCTION, f"{PROTOTYPE}/docs/release/capacity.json"):
        _write(checkout, path, (repo / path).read_text(encoding="utf-8"))
    candidate = _generate(root, source, _commit(checkout))
    assert candidate.returncode == 0, candidate.stderr
    manifest = json.loads(candidate.stdout)
    suites = {entry["name"]: entry for entry in manifest["validation_suites"]}
    assert suites["fixed_gis"]["counts"]["passed"] == 16
    assert [node for node in suites["fixed_gis"]["command"] if node.startswith("tests/")] == [
        f"tests/test_fixed.py::test_case[T{index:02}]" for index in range(1, 17)
    ]
    assert {item["path"] for item in manifest["evidence"]} >= {
        f"{REPORT_DIR}/frontend-install.log", f"{REPORT_DIR}/frontend-build.log",
        f"{REPORT_DIR}/backend-collection.log", f"{REPORT_DIR}/backend-dependencies.log",
    }
    assert _invoke(root, VALIDATOR, *options).returncode == 1


@pytest.mark.parametrize(("key", "diagnosis"), [
    ("RELEASE_FIXTURE_FAIL", "fixed_gis exited 5"),
    ("RELEASE_FIXTURE_MISSING", "Missing or duplicate fixed GIS node for T16"),
])
def test_validation_preserves_failed_evidence_without_receipt(validation_checkout, monkeypatch, key, diagnosis):
    _, root, output, options = validation_checkout
    monkeypatch.setenv(key, "fixed_gis")
    result = _invoke(root, VALIDATOR, *options)
    assert result.returncode == 1 and diagnosis in result.stderr
    assert (output / "round2-validation" / "fixed_gis.log").is_file()
    assert not (output / "round2-validation.json").exists()


def test_validation_refuses_dirty_checkout_before_running(validation_checkout):
    _, root, output, options = validation_checkout
    _write(root, "src/untracked.py", "outside the candidate\n")
    result = _invoke(root, VALIDATOR, *options)
    assert result.returncode == 1 and "checkout has tracked or untracked changes" in result.stderr
    assert not output.exists()


def test_validation_refuses_a_conflicting_managed_qgis(validation_checkout):
    _, root, output, options = validation_checkout
    _write(root, ".environments/qgis-deb/qgis_process", "fixture with a different installation\n")
    result = _invoke(root, VALIDATOR, *options)
    assert result.returncode == 1 and "managed QGIS executable different" in result.stderr
    assert not output.exists()
