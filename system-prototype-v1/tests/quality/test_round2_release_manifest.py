import json
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
REPORT_DIR = f"{PROTOTYPE}/artifacts/reports/final/round2-validation"
REPRODUCTION = f"{PROTOTYPE}/docs/release/reproduction.md"


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
    }
    for path, content in files.items():
        _write(root, path, content)
    _write(repo, ".github/workflows/ci.yml", "name: fixture\n")
    for tool in (GENERATOR, VERIFIER, "scripts/quality/round2_release_manifest.py",
                 "scripts/quality/build_source_inventory.py", "scripts/quality/check_doc_evidence_links.py"):
        destination = root / tool
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / tool, destination)
    source = _commit(repo)
    suites = []
    for name in ("backend", "frontend", "fixed_gis"):
        junit, log = f"{REPORT_DIR}/{name}.xml", f"{REPORT_DIR}/{name}.log"
        _write(repo, log, f"commit {source}\nfixture validation\nexit 0\n")
        skip = '<testcase name="optional"><skipped /></testcase>' if name == "backend" else ""
        count = 2 if skip else 1
        _write(repo, junit, f'<testsuites><testsuite tests="{count}"><testcase name="observed"/>{skip}</testsuite></testsuites>')
        suites.append({
            "name": name, "command": ["fixture", name], "environment": {"runtime": "fixture-1"},
            "exit_code": 0, "junit": junit, "log": log,
        })
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
    }
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
