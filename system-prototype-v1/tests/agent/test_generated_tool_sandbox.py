import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from geopilot.llm import generated_tool_sandbox as sandbox


ROOT = Path(__file__).resolve().parents[2]
GOOD = """\
def count_threshold(values, threshold):
    valid = 0
    selected = 0
    for value in values:
        valid += 1
        if value >= threshold:
            selected += 1
    return {"valid_count": valid, "selected_count": selected}
"""
BUSY = """\
def count_threshold(values, threshold):
    while True:
        pass
"""
MEMORY = """\
def count_threshold(values, threshold):
    padding = [0] * 20000000
    return {"valid_count": 0, "selected_count": 0}
"""


def forbid_process(*args, **kwargs):
    pytest.fail("No process may start for a statically rejected or unavailable candidate")


@pytest.mark.parametrize("body", [
    "import socket",
    'value = open("fixture.txt")',
    "value = os.environ",
    "return password",
    "value = eval(threshold)",
    "value = values[0]",
    "return [value for value in values]",
    "threshold = 0",
    'return {"unexpected": 1}',
    'return {"selected_count": 0, "selected_count": 1}',
])
def test_invalid_syntax_never_starts_a_process(body, monkeypatch):
    monkeypatch.setattr(sandbox, "capture_process", forbid_process)
    report = sandbox.review_candidate("def count_threshold(values, threshold):\n    " + body)
    assert report["status"] == "REJECTED"
    assert report["error"]["code"] == "SOURCE_REJECTED"
    assert report["sample"] is None and report["cleanup"] == {"attempted": False, "verified": None}


@pytest.mark.parametrize("source", [
    b"\xff",
    "x" * (sandbox.MAX_SOURCE_BYTES + 1),
    "def count_threshold(values, threshold):\n" + "    value = 0\n" * 90,
    "def count_threshold(values, threshold, extra=1):\n    pass",
    "def count_threshold(values, threshold):\n    pass\ndef other():\n    pass",
    "def count_threshold(values, threshold):\n    break",
])
def test_source_encoding_signature_node_and_byte_limits(source, monkeypatch):
    monkeypatch.setattr(sandbox, "capture_process", forbid_process)
    report = sandbox.review_candidate(source)
    assert report["status"] == "REJECTED" and report["error"]["phase"] == "static"


def test_cli_requires_a_bounded_regular_file_and_describes_only_the_contract(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "capture_process", forbid_process)
    original = tmp_path / "source.py"
    original.write_text(GOOD, encoding="utf-8")
    assert sandbox._read_candidate(original) == GOOD.encode("utf-8")
    symlink = tmp_path / "symlink.py"
    symlink.symlink_to(original)
    too_large = tmp_path / "long.py"
    too_large.write_bytes(b"x" * (sandbox.MAX_SOURCE_BYTES + 1))
    fifo = tmp_path / "fifo.py"
    os.mkfifo(fifo)
    for path in (symlink, too_large, fifo, tmp_path / "missing.py"):
        monkeypatch.setattr(sys, "argv", ["sandbox", "--candidate", str(path)])
        assert sandbox.main() == 2
        printed = capsys.readouterr().out
        assert str(path) not in printed
        assert json.loads(printed)["error"]["code"] == "INVALID_CANDIDATE_FILE"
    monkeypatch.setattr(sys, "argv", ["sandbox", "--describe"])
    assert sandbox.main() == 0
    description = json.loads(capsys.readouterr().out)
    assert description["function"] == "count_threshold"
    assert set(description["inputs"]) == {"values", "threshold"}
    assert not ({"sample", "path", "pixels"} & description.keys())
    monkeypatch.setattr(sys, "argv", ["sandbox", "--generate"])
    assert sandbox.main() == 0
    assert capsys.readouterr().out == GOOD


def test_reference_change_and_missing_runtime_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "capture_process", forbid_process)
    fixture = tmp_path / "fixture"
    shutil.copytree(sandbox._FIXTURE, fixture)
    with monkeypatch.context() as change:
        change.setattr(sandbox, "_FIXTURE", fixture)
        dem = fixture / "inputs/dem.tif"
        dem.write_bytes(dem.read_bytes() + b"changed")
        rejected = sandbox.review_candidate(GOOD)
    assert rejected["error"]["code"] == "REFERENCE_INVALID"
    assert rejected["sample"] is None and rejected["cleanup"]["attempted"] is False
    monkeypatch.setattr(sandbox, "_DOCKER", tmp_path / "missing-docker")
    unavailable = sandbox.review_candidate(GOOD)
    assert unavailable["status"] == "FAILED" and unavailable["error"]["code"] == "SANDBOX_UNAVAILABLE"
    assert unavailable["cleanup"]["attempted"] is False


def test_output_limits_and_unverified_cleanup_fail_closed(monkeypatch):
    for text in (" " * (sandbox.MAX_OUTPUT_BYTES + 1), '{"runtime":null,"runtime":null,"results":[]}',
                 '{"runtime":null,"results":[]}'):
        with pytest.raises(sandbox._SandboxError, match="invalid or oversized"):
            sandbox._decode_result(text)

    def unverified(workspace, stage, report, cancel_requested):
        report["cleanup"].update({"attempted": True, "verified": False})
        return {}, [{"valid_count": 47, "selected_count": n} for n in (47, 27, 0)]

    monkeypatch.setattr(sandbox, "_run_container", unverified)
    monkeypatch.setattr(sandbox, "capture_process", forbid_process)
    report = sandbox.review_candidate(GOOD)
    assert report["status"] == "FAILED" and report["error"]["code"] == "SANDBOX_CLEANUP_FAILED"


@pytest.fixture(scope="module")
def local_docker(tmp_path_factory):
    client = sandbox._DockerClient(tmp_path_factory.mktemp("docker-client"))
    try:
        client.check()
    except sandbox._SandboxError as error:
        pytest.skip(str(error))
    return client


def _tree(root):
    return {
        path.relative_to(root).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


def _proof(report, tmp_path, local_docker):
    (tmp_path / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    assert report["cleanup"] == {"attempted": True, "verified": True}
    execution = report["execution"]
    assert local_docker.inspect(execution["container_id"]) is None
    assert local_docker.inspect(execution["container_name"]) is None


def test_container_cli_validates_geotiff_and_preserves_formal_paths(tmp_path, local_docker, monkeypatch):
    simulated_project = tmp_path / "formal"
    simulated_project.mkdir()
    with sqlite3.connect(simulated_project / "project.sqlite") as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('formal')")
    (simulated_project / "artifact.json").write_text('{"origin":"formal"}', encoding="utf-8")
    tools = ROOT / "config/tools"
    before = _tree(tools), _tree(simulated_project), _tree(sandbox._FIXTURE)
    candidate = tmp_path / "candidate.py"
    monkeypatch.setenv("GEOPILOT_TEST_PRIVATE_MARKER", "synthetic-private-value")
    monkeypatch.setenv("GEOPILOT_DATA_DIR", str(simulated_project))
    monkeypatch.setenv("DOCKER_HOST", "unix:///geopilot-unconfigured-docker.sock")
    command = [sys.executable, "-m", "geopilot.llm.generated_tool_sandbox"]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    described = subprocess.run([*command, "--describe"], cwd=ROOT, env=env, capture_output=True, timeout=10, check=True)
    generated = subprocess.run([*command, "--generate"], cwd=ROOT, env=env, capture_output=True, timeout=10, check=True)
    description = json.loads(described.stdout)
    assert description["function"] == "count_threshold" and generated.stdout.decode("utf-8") == GOOD
    candidate.write_bytes(generated.stdout)
    result = subprocess.run([*command, "--candidate", str(candidate)], cwd=ROOT, env=env, capture_output=True,
                            text=True, timeout=45, check=False)
    assert result.stderr == ""
    report = json.loads(result.stdout)
    _proof(report, tmp_path, local_docker)
    assert result.returncode == 0 and report["status"] == "REVIEW_REQUIRED" and report["error"] is None
    assert report["candidate_sha256"] == hashlib.sha256(GOOD.encode()).hexdigest()
    assert report["sample"]["nodata_count"] == 2 and report["sample"]["grid"] == [7, 7]
    assert report["sample"]["crs"] == "EPSG:6933" and report["sample"]["cell_area_m2"] == 100
    assert [(case["threshold"], case["actual"]["selected_count"], case["actual"]["selected_area_m2"])
            for case in report["reference"]] == [(100, 47, 4700), (130, 27, 2700), (161, 0, 0)]
    assert all(case["matched"] and case["actual"]["valid_count"] == 47 for case in report["reference"])
    execution = report["execution"]
    assert execution["policy"]["network"] == "none" and execution["policy"]["cgroup_namespace"] == "private"
    assert execution["policy"]["memory_bytes"] == execution["policy"]["memory_and_swap_bytes"] == 67108864
    assert execution["policy"]["pids"] == 16 and execution["policy"]["mount_count"] == 1
    assert execution["worker"]["clean_environment"] and execution["worker"]["seccomp_filter"]
    assert execution["worker"]["read_only_root"] and execution["worker"]["read_only_stage"]
    assert execution["worker"]["uid"] == 65534 and execution["worker"]["effective_capabilities"] == 0
    assert "synthetic-private-value" not in result.stdout and str(candidate) not in result.stdout
    assert str(simulated_project) not in result.stdout and str(sandbox._FIXTURE) not in result.stdout
    assert (_tree(tools), _tree(simulated_project), _tree(sandbox._FIXTURE)) == before
    (tmp_path / "generation.json").write_text(json.dumps({
        "contract": description, "generator": "deterministic_local_reference",
        "candidate_sha256": report["candidate_sha256"],
        "exit_codes": {"describe": described.returncode, "generate": generated.returncode, "review": result.returncode},
        "tool_project_and_source_fingerprints_unchanged": True,
    }, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize(("source", "code"), [
    (GOOD.replace(">= threshold", "> threshold"), "REFERENCE_MISMATCH"),
    ('def count_threshold(values, threshold):\n    return {"valid_count": True, "selected_count": 0}',
     "CANDIDATE_OUTPUT_INVALID"),
])
def test_container_rejects_wrong_reference_and_result_types(source, code, tmp_path, local_docker):
    report = sandbox.review_candidate(source)
    _proof(report, tmp_path, local_docker)
    assert report["status"] == "FAILED" and report["error"]["code"] == code
    if code == "REFERENCE_MISMATCH":
        assert [case["matched"] for case in report["reference"]] == [False, False, True]


def test_container_memory_limit_is_enforced_and_cleaned(tmp_path, local_docker):
    report = sandbox.review_candidate(MEMORY)
    _proof(report, tmp_path, local_docker)
    assert report["status"] == "FAILED" and report["error"]["code"] == "SANDBOX_MEMORY_LIMIT"
    assert report["execution"]["oom_killed"] is True and report["execution"]["container_exit_code"] == 137


@pytest.mark.parametrize(("cancel", "code"), [(False, "SANDBOX_TIMEOUT"), (True, "SANDBOX_CANCELLED")])
def test_container_timeout_and_cancel_remove_the_owned_container(cancel, code, tmp_path, local_docker):
    calls = 0
    attached_at = None

    def request_cancel():
        nonlocal calls, attached_at
        calls += 1
        if calls < 3:
            return False
        if attached_at is None:
            attached_at = time.monotonic()
        return time.monotonic() - attached_at >= 0.8

    report = sandbox.review_candidate(BUSY, cancel_requested=request_cancel if cancel else None)
    _proof(report, tmp_path, local_docker)
    assert report["status"] == "FAILED" and report["error"]["code"] == code
    assert report["execution"]["cancelled"] is cancel and report["execution"]["timed_out"] is not cancel
