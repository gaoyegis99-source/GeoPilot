import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/methods/method_cli.py"
FIXTURES = Path(__file__).parent / "fixtures"
VECTOR = FIXTURES / "attribute-filter" / "manifest.json"
RASTER = FIXTURES / "dem-terrain" / "manifest.json"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def tree_state(root):
    return {
        path.relative_to(root): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


def invoke(work_dir, package, *args, env=None, code=0):
    result = subprocess.run(
        [sys.executable, str(CLI), "trial", "--package", str(package), "--work-dir", str(work_dir), *map(str, args)],
        cwd=ROOT, env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=90, check=False,
    )
    assert result.returncode == code, (result.stdout, result.stderr)
    envelope = json.loads(result.stdout)
    assert (envelope["schema_version"], envelope["command"], envelope["status"]) == (
        1, "trial", "ok" if code == 0 else "error",
    )
    if code == 0:
        assert envelope["data"]["report"] == read_json(work_dir / "report.json")
        assert envelope["data"]["scope"] == "isolated_trial"
    else:
        assert envelope["data"] is None and len(envelope["errors"]) == 1
    return envelope


@pytest.fixture
def qgis_process():
    configured = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if not configured:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS for the real vector trial")
    return configured


@pytest.fixture
def formal(tmp_path):
    directory = tmp_path / "formal"
    directory.mkdir()
    with sqlite3.connect(directory / "project.sqlite") as connection:
        connection.execute("CREATE TABLE marker (label TEXT)")
        connection.execute("INSERT INTO marker VALUES ('formal')")
    (directory / "output.json").write_text('{"source": "formal"}', encoding="utf-8")
    return directory


def test_real_vector_external_candidate_is_isolated(tmp_path, formal, qgis_process):
    config = ROOT / "config/methods/attribute-filter-export.v1.yaml"
    candidate = tmp_path / "external.yaml"
    shutil.copyfile(config, candidate)
    before = tree_state(VECTOR.parent), tree_state(ROOT / "config"), tree_state(formal), candidate.read_bytes()
    work = tmp_path / "vector-trial"

    invoke(work, VECTOR, "--method", candidate, "--qgis-process", qgis_process, "--timeout", 60,
           env={"GEOPILOT_DATA_DIR": str(formal)})

    report = read_json(work / "report.json")
    assert report["verdict"] == "positive_match" and report["reference"]["status"] == "PASS"
    assert report["tools"] == [{"id": "qgis-extract-by-attribute", "version": 1}]
    assert report["actual"]["workers_stopped"] is True and report["actual"]["run_count"] == 1
    assert report["actual"]["run"]["execution_status"] == "SUCCEEDED"
    assert report["actual"]["run"]["check_status"] == "PASSED"
    assert report["fingerprints"]["method"] == hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert report["reference"]["outputs"]["filtered_features"]["checks"]["feature_count"] == 2
    assert report["source_integrity"] == {"status": "PASS", "error": None}
    assert work / "project.sqlite" != formal / "project.sqlite"
    assert (work / "project.sqlite").is_file()
    assert (work / "config/methods/selected.yaml").read_bytes() == candidate.read_bytes()
    assert (work / report["actual"]["run_dir"] / "request.json").is_file()
    assert all((work / item["path"]).stat().st_mode & 0o222 == 0 for item in report["inputs"].values())
    assert "<new-trial-directory>" in report["replay_argv"]
    assert (tree_state(VECTOR.parent), tree_state(ROOT / "config"), tree_state(formal), candidate.read_bytes()) == before


def test_real_raster_builtin_trial_matches_grid_nodata_and_area(tmp_path):
    before = tree_state(RASTER.parent)
    work = tmp_path / "raster-trial"

    invoke(work, RASTER, "--timeout", 60)

    report = read_json(work / "report.json")
    assert report["verdict"] == "positive_match"
    assert report["actual"]["run"]["check_status"] == "PASSED"
    assert report["actual"]["workers_stopped"] is True
    assert set(report["reference"]["outputs"]) == {"terrain_raster", "threshold_regions", "area_statistics"}
    assert all(result["status"] == "PASS" and result["sha256"] for result in report["reference"]["outputs"].values())
    assert report["environment"]["observed"]["gdal_raster"]
    assert tree_state(RASTER.parent) == before


@pytest.mark.parametrize(("package", "case", "phase", "error_code"), [
    (RASTER, "missing-vertical-unit", "draft", "DEM_VERTICAL_UNIT_REQUIRED"),
    (VECTOR, "missing-filter-field", "confirm", "METHOD_PRECONDITION_FAILED"),
])
def test_expected_failures_match_phase_and_never_create_a_run(tmp_path, package, case, phase, error_code, request):
    work = tmp_path / "negative-trial"
    args = ["--case", case]
    if package == VECTOR:
        args.extend(("--qgis-process", request.getfixturevalue("qgis_process")))

    invoke(work, package, *args)

    report = read_json(work / "report.json")
    assert report["verdict"] == "expected_failure_matched"
    assert report["expected_failure"] == {"phase": phase, "code": error_code}
    assert report["actual"]["phase"] == phase and report["actual"]["run_count"] == 0
    assert report["actual"]["run"] is None and report["reference"] is None
    assert error_code in {issue["code"] for issue in report["actual"]["issues"]}
    with sqlite3.connect(work / "project.sqlite") as connection:
        assert connection.execute("SELECT count(*) FROM run").fetchone() == (0,)


def test_rejects_work_dir_reuse_protected_paths_and_mismatched_candidate(tmp_path, formal):
    before = tree_state(formal)
    reused = invoke(formal, RASTER, code=2)
    assert reused["errors"][0]["field"] == "work_dir"
    rejected = invoke(formal / "nested-trial", RASTER, env={"GEOPILOT_DATA_DIR": str(formal)}, code=2)
    assert rejected["errors"][0]["code"] == "UNSAFE_PATH"
    assert tree_state(formal) == before and not (formal / "nested-trial").exists()

    candidate = tmp_path / "candidate.yaml"
    document = yaml.safe_load((ROOT / "config/methods/dem-terrain-analysis.v1.yaml").read_text())
    document["version"] = 99
    candidate.write_text(yaml.safe_dump(document), encoding="utf-8")
    work = tmp_path / "wrong-version"
    wrong = invoke(work, RASTER, "--method", candidate, code=1)
    assert wrong["errors"][0]["code"] == "METHOD_REFERENCE_MISMATCH"
    assert not work.exists()
    installed = invoke(work, RASTER, "--method", ROOT / "config/methods/dem-terrain-analysis.v1.yaml", code=2)
    assert installed["errors"][0]["code"] == "UNSAFE_PATH" and not work.exists()


def test_lint_failure_keeps_report_without_executing_unknown_tool(tmp_path):
    candidate = tmp_path / "candidate.yaml"
    document = yaml.safe_load((ROOT / "config/methods/dem-terrain-analysis.v1.yaml").read_text())
    document["steps"][0]["tool"]["id"] = "unregistered-worker"
    candidate.write_text(yaml.safe_dump(document), encoding="utf-8")
    work = tmp_path / "lint-failure"

    invoke(work, RASTER, "--method", candidate, code=1)

    report = read_json(work / "report.json")
    assert report["status"] == "FAIL" and report["actual"]["phase"] == "lint"
    assert any(issue["code"] == "TOOL_NOT_REGISTERED" for issue in report["actual"]["issues"])
    assert report["actual"]["run_count"] == 0
    assert not (work / "project.sqlite").exists()


def test_incorrect_expected_error_is_reported_as_mismatch_without_run(tmp_path):
    copied = tmp_path / "copied-package"
    shutil.copytree(RASTER.parent, copied)
    manifest = copied / "manifest.json"
    document = read_json(manifest)
    document["failure_cases"][0]["expected"]["code"] = "WRONG_EXPECTED_CODE"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    work = tmp_path / "unexpected-result"

    mismatch = invoke(work, manifest, "--case", "missing-vertical-unit", code=1)

    report = read_json(work / "report.json")
    assert mismatch["errors"][0]["code"] == "EXPECTED_FAILURE_MISMATCH"
    assert report["actual"]["phase"] == "draft" and report["actual"]["run_count"] == 0
    assert report["actual"]["issues"][0]["code"] == "DEM_VERTICAL_UNIT_REQUIRED"


def test_independent_reference_mismatch_is_reported_after_v1_checks_pass(tmp_path):
    copied = tmp_path / "copied-package"
    shutil.copytree(RASTER.parent, copied)
    manifest = copied / "manifest.json"
    document = read_json(manifest)
    area = copied / "expected" / "area.json"
    expected = read_json(area)
    expected["value"]["total_area_m2"] = 0.0
    area.write_text(json.dumps(expected), encoding="utf-8")
    document["outputs"]["area_statistics"]["reference"]["sha256"] = hashlib.sha256(area.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(document), encoding="utf-8")
    work = tmp_path / "reference-mismatch"

    mismatch = invoke(work, manifest, code=1)

    report = read_json(work / "report.json")
    assert mismatch["errors"][0]["code"] == "REFERENCE_MISMATCH"
    assert report["actual"]["phase"] == "reference"
    assert report["actual"]["run"]["execution_status"] == "SUCCEEDED"
    assert report["actual"]["run"]["check_status"] == "PASSED"
    assert report["reference"]["status"] == "FAIL"
    assert report["reference"]["outputs"]["area_statistics"]["status"] == "FAIL"


@pytest.fixture
def qgis_stand_in(tmp_path):
    executable = tmp_path / "qgis-foreground-fixture"
    executable.write_text(f"#!{sys.executable}\n" + dedent("""\
        import json
        import os
        import signal
        import subprocess
        import sys
        from pathlib import Path

        algorithm = "native:extractbyattribute"
        command = sys.argv[2]
        if command == "list" and not os.environ.get("TRIAL_TEST_DISCOVERY_HANG"):
            print(json.dumps({"providers": {"native": {"algorithms": {algorithm: {}}}},
                              "qgis_version": "3.44.15-fixture"}))
        elif command == "help":
            parameters = {name: {"is_destination": name == "OUTPUT", "optional": False}
                          for name in ("INPUT", "FIELD", "OPERATOR", "VALUE", "OUTPUT")}
            parameters["OPERATOR"]["available_options"] = {"0": "equals"}
            print(json.dumps({"algorithm_details": {"id": algorithm}, "parameters": parameters, "outputs": {"OUTPUT": {}}}))
        elif os.environ.get("TRIAL_TEST_RUN_ERROR"):
            print("fixture exits 7", file=sys.stderr)
            sys.exit(7)
        else:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

            def finish(*_):
                child.wait(timeout=1)
                sys.exit(143)

            signal.signal(signal.SIGTERM, finish)
            Path("worker-pids.json").write_text(json.dumps([os.getpid(), child.pid]))
            child.wait()
    """), encoding="utf-8")
    executable.chmod(0o700)
    return executable


def stopped(pid):
    try:
        record = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return True
    return record.rsplit(")", 1)[1].strip().startswith("Z ")


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="Linux process cleanup observation")
@pytest.mark.parametrize("phase", ["execute", "discovery"])
def test_timeout_reaps_real_worker_group_and_preserves_diagnostics(tmp_path, qgis_stand_in, phase):
    work = tmp_path / "timeout"
    env = {"TRIAL_TEST_DISCOVERY_HANG": "1"} if phase == "discovery" else {}
    timeout = 4 if phase == "execute" else 1.5

    payload = invoke(work, VECTOR, "--qgis-process", qgis_stand_in, "--timeout", timeout, env=env, code=1)

    report = read_json(work / "report.json")
    assert payload["errors"][0]["code"] == "TRIAL_TIMEOUT"
    assert report["actual"]["phase"] == phase and report["timing"]["deadline_expired"] is True
    if phase == "execute":
        assert report["actual"]["run_count"] == 1 and report["actual"]["workers_stopped"] is True
        assert report["actual"]["cancellation_accepted"] is True
        assert report["actual"]["run"]["execution_status"] == "CANCELLED"
        directory = work / report["actual"]["run_dir"]
        assert (directory / "steps/filter_features/process.log").is_file()
    else:
        assert report["actual"]["run_count"] == 0
        assert report["actual"]["discovery_workers_stopped"] is True
        directory = work / "discovery"
        assert (directory / "process.log").is_file()
    pids = read_json(directory / "worker-pids.json")
    assert len(pids) == 2 and all(stopped(pid) for pid in pids), pids


def test_failed_worker_retains_run_status_and_log(tmp_path, qgis_stand_in):
    work = tmp_path / "failed-trial"

    result = invoke(work, VECTOR, "--qgis-process", qgis_stand_in, env={"TRIAL_TEST_RUN_ERROR": "1"}, code=1)

    report = read_json(work / "report.json")
    assert result["errors"][0]["code"] == "PROCESS_FAILED"
    assert report["actual"]["run"]["execution_status"] == "FAILED"
    assert report["actual"]["workers_stopped"] is True
    log = work / report["actual"]["run_dir"] / "steps/filter_features/process.log"
    assert "fixture exits 7" in log.read_text(encoding="utf-8")
    assert report["reference"] is None
