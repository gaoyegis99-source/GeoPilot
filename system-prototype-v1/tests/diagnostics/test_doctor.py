import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from geopilot.diagnostics import doctor
from geopilot.execution.process_manager import ProcessOutcome


ROOT = Path(__file__).resolve().parents[2]


def test_python_pins_report_missing_and_mismatched_packages(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("# runtime pins\nFound==1.0\nmissing==2.0\n", encoding="utf-8")

    def installed(name):
        if name == "missing":
            raise doctor.PackageNotFoundError(name)
        return "1.1"

    monkeypatch.setattr(doctor, "version", installed)
    check = doctor.check_python_dependencies(requirements)
    assert check.status == "FAIL"
    assert check.details["packages"] == {
        "found": {"actual": "1.1", "expected": "1.0"},
        "missing": {"actual": None, "expected": "2.0"},
    }


def test_python_pins_detect_duplicate_distribution(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("Some_Package==1\nsome-package==1\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "version", lambda _: "1")
    check = doctor.check_python_dependencies(requirements)
    assert check.status == "FAIL" and "Duplicate" in check.message


def qgis_response(version):
    return {
        "algorithm_details": {"id": "native:buffer"},
        "parameters": {"DISTANCE": {}},
        "outputs": {"OUTPUT": {}},
        "qgis_version": version,
        "gdal_version": "3.8.4",
        "proj_version": "9.4.0",
        "geos_version": "3.12.1",
        "python_version": "3.12.3",
    }


@pytest.mark.parametrize(
    ("version", "status"), [("3.44.15-Solothurn", "PASS"), ("3.40.0-other", "WARN")]
)
def test_qgis_uses_offscreen_probe_and_reports_linked_libraries(tmp_path, version, status):
    tool = tmp_path / "qgis-test"
    content = json.dumps({
        **qgis_response(version),
        "proj_version": "Rel. 9.4.0, March 1st, 2024",
        "geos_version": "3.12.1-CAPI-1.18.1",
    })
    tool.write_text(
        "#!/bin/sh\n"
        'test "$QT_QPA_PLATFORM" = offscreen || exit 91\n'
        'test "$PROJ_NETWORK" = OFF || exit 92\n'
        'test "$1 $2 $3" = "--json help native:buffer" || exit 93\n'
        f"printf '%s\\n' '{content}'\n",
        encoding="utf-8",
    )
    tool.chmod(0o700)
    check = doctor.check_qgis(str(tool), tmp_path)
    assert check.status == status
    assert check.details["versions"]["gdal_version"] == "3.8.4"
    assert check.details["versions"]["proj_version"] == "Rel. 9.4.0, March 1st, 2024"


@pytest.mark.parametrize(
    "outcome",
    [
        ProcessOutcome(0, "not JSON", "", False, False),
        ProcessOutcome(0, json.dumps({**qgis_response("3.44.15"), "parameters": "DISTANCE"}), "", False, False),
        ProcessOutcome(1, "", "", False, False),
        ProcessOutcome(-9, "", "", True, False),
    ],
)
def test_qgis_rejects_bad_help_or_unsuccessful_probe(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/qgis-test")
    monkeypatch.setattr(doctor, "probe", lambda *_: outcome)
    check = doctor.check_qgis("/qgis-test", tmp_path)
    assert check.status == "FAIL"


def test_qgis_warns_about_different_gdal_even_when_qgis_version_matches(tmp_path, monkeypatch):
    document = {**qgis_response("3.44.15-Solothurn"), "gdal_version": "3.9.0"}
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/qgis-test")
    monkeypatch.setattr(doctor, "probe", lambda *_: ProcessOutcome(0, json.dumps(document), "", False, False))
    check = doctor.check_qgis("/qgis-test", tmp_path)
    assert check.status == "WARN"
    assert check.details["verified_libraries"]["gdal_version"] == "3.8.4"


def test_directory_checks_do_not_create_storage_or_accept_wrong_type(tmp_path):
    absent = tmp_path / "parent" / "database.sqlite"
    assert doctor.check_storage("database", absent, directory=False).status == "PASS"
    assert not absent.parent.exists()
    assert doctor.check_storage("database", tmp_path, directory=False).status == "FAIL"
    file = tmp_path / "not-a-directory"
    file.touch()
    assert doctor.check_storage("workspace", file / "subdir", directory=True).status == "FAIL"


def test_port_probe_detects_occupied_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        assert doctor.check_port("backend_port", port).status == "FAIL"
    assert doctor.check_port("backend_port", port).status == "PASS"


def test_frontend_lock_and_installed_direct_package_are_checked(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"@scope/pkg":"1.2.3"},"devDependencies":{}}', encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text(
        '{"packages":{"node_modules/@scope/pkg":{"version":"1.2.3"}}}', encoding="utf-8",
    )
    check = doctor.check_frontend_dependencies(tmp_path)
    assert check.status == "FAIL" and "npm ci" in check.message
    package = tmp_path / "node_modules/@scope/pkg/package.json"
    package.parent.mkdir(parents=True)
    package.write_text('{"version":"1.2.3"}', encoding="utf-8")
    assert doctor.check_frontend_dependencies(tmp_path).status == "PASS"
    package.write_text('{"version":"2.0.0"}', encoding="utf-8")
    assert doctor.check_frontend_dependencies(tmp_path).status == "FAIL"


def test_cli_leaves_closed_wal_database_and_workspace_untouched(tmp_path):
    database = tmp_path / "project.sqlite"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE sample (id INTEGER)")
    connection.close()
    before = database.read_bytes()
    assert not list(tmp_path.glob("*.sqlite-*"))
    workspace = tmp_path / "new-workspace"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [
            sys.executable, "-m", "geopilot.diagnostics", "--json", "--qgis-process", str(tmp_path / "missing-qgis"),
            "--database", str(database), "--workspace", str(workspace),
            "--port", "17171", "--frontend-port", "17171",
        ],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    checks = {row["name"]: row for row in report["checks"]}
    assert report["schema_version"] == 1 and report["status"] == "FAIL"
    assert checks["qgis"]["status"] == checks["frontend_port"]["status"] == "FAIL"
    assert database.read_bytes() == before and not list(tmp_path.glob("*.sqlite-*"))
    assert not workspace.exists()
