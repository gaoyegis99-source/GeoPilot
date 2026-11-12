import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from geopilot.methods.loader import load_method_catalog
from geopilot.remote_sensing.config_registry import load_sensor_registry


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/methods/check_versions.py"
METHOD = "methods/road-buffer-statistics.v2.yaml"
TOOL = "tools/qgis-buffer.yaml"
SENSOR = "sensors/landsat-8-c2-l2sp.v1.yaml"


@pytest.fixture
def candidate(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    return tmp_path


@pytest.fixture(scope="module")
def baseline_version_count():
    versions = set()
    for family in ("methods", "tools", "sensors"):
        for path in (ROOT / "config" / family).iterdir():
            if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
                continue
            document = yaml.safe_load(path.read_bytes())
            version = (family, document["id"], document["version"])
            assert version not in versions, version
            versions.add(version)
    return len(versions)


def compare(candidate, *, baseline=ROOT):
    result = subprocess.run([
        sys.executable, str(CLI), "--baseline", str(baseline), "--candidate", str(candidate),
    ], capture_output=True, text=True, check=False, env={
        **os.environ, "GEOPILOT_QGIS_PROCESS": "/nonexistent/qgis-process",
    })
    assert not result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert result.returncode == (0 if report["status"] == "PASS" else 1)
    return report


def load(root, name):
    return yaml.safe_load((root / "config" / name).read_bytes())


def write(root, name, value):
    (root / "config" / name).write_text(yaml.safe_dump(value), encoding="utf-8")


def subject(family, identifier, version):
    return {"family": family, "id": identifier, "version": version}


def test_unchanged_registry_passes_without_qgis_probe(candidate, baseline_version_count):
    before = {path: path.read_bytes() for path in candidate.rglob("*.yaml")}
    report = compare(candidate)

    assert report == {
        "schema_version": 1, "status": "PASS", "preserved_versions": baseline_version_count, "added": [], "issues": [],
    }
    assert {path: path.read_bytes() for path in candidate.rglob("*.yaml")} == before


def test_file_rename_and_yaml_formatting_keep_the_exact_version(candidate, baseline_version_count):
    method = load(candidate, METHOD)
    (candidate / "config" / METHOD).unlink()
    write(candidate, "methods/renamed.yml", method)

    report = compare(candidate)

    assert report["status"] == "PASS"
    assert report["preserved_versions"] == baseline_version_count and not report["added"]


def test_additive_versions_preserve_historical_method_and_sensor_lookup(candidate, baseline_version_count):
    method = load(candidate, METHOD)
    method["version"] = 4
    write(candidate, "methods/road-buffer-statistics.v4.yaml", method)
    tool = load(candidate, TOOL)
    tool["version"] = 2
    write(candidate, "tools/qgis-buffer.v2.yaml", tool)
    sensor = load(candidate, SENSOR)
    sensor.update(version=2, processing_version="99")
    write(candidate, "sensors/landsat-8-c2-l2sp.v2.yaml", sensor)

    report = compare(candidate)

    assert report["status"] == "PASS" and report["preserved_versions"] == baseline_version_count
    assert report["added"] == [
        subject("method", method["id"], 4), subject("sensor", sensor["id"], 2), subject("tool", tool["id"], 2),
    ]
    old_catalog = load_method_catalog(ROOT / "config/methods")
    new_catalog = load_method_catalog(candidate / "config/methods")
    assert new_catalog.get(method["id"]).version == 4
    assert new_catalog.get(method["id"], 2) == old_catalog.get(method["id"], 2)
    old_registry = load_sensor_registry(ROOT / "config/sensors")
    new_registry = load_sensor_registry(candidate / "config/sensors")
    prior = old_registry.get({"id": sensor["id"], "version": 1})
    assert new_registry.get(prior.ref) == prior == new_registry.lookup(prior.key)


@pytest.mark.parametrize(("family", "name", "edit"), [
    ("method", METHOD, lambda doc: doc.update(locked_order=False)),
    ("tool", TOOL, lambda doc: doc["parameters"]["segments"].update(default=18)),
    ("sensor", SENSOR, lambda doc: doc["band_roles"]["red"].update(nodata_dn=1)),
])
def test_valid_but_changed_definition_requires_a_new_version(candidate, family, name, edit):
    document = load(candidate, name)
    edit(document)
    write(candidate, name, document)

    report = compare(candidate)

    issue, = report["issues"]
    assert issue["code"] == "VERSION_REDEFINED"
    assert issue["subject"] == subject(family, document["id"], document["version"])


def test_higher_method_version_cannot_replace_a_historical_reference(candidate):
    method = load(candidate, METHOD)
    (candidate / "config" / METHOD).unlink()

    report = compare(candidate)

    issue, = report["issues"]
    assert issue["code"] == "VERSION_REMOVED"
    assert issue["subject"] == subject("method", method["id"], 2)
    assert load_method_catalog(candidate / "config/methods").get(method["id"]).version == 3


def test_effective_qgis_binding_must_preserve_exact_tool_version(candidate):
    name = "runner_bindings/qgis-buffer.yaml"
    bindings = load(candidate, name)
    bindings["native:buffer"]["enum_values"]["end_cap_style"].update(round=1, flat=0)
    write(candidate, name, bindings)

    report = compare(candidate)

    issue, = report["issues"]
    assert issue["code"] == "VERSION_BINDING_CHANGED"
    assert issue["subject"] == subject("tool", "qgis-buffer", 1)


def test_ambiguous_sensor_key_fails_before_publish(candidate):
    sensor = load(candidate, SENSOR)
    sensor["version"] = 2
    write(candidate, "sensors/landsat-8-c2-l2sp.v2.yaml", sensor)

    report = compare(candidate)

    assert report["status"] == "FAIL" and not report["added"]
    assert {issue["code"] for issue in report["issues"]} == {"INVALID_SENSOR_LOOKUP"}


def test_missing_root_has_structured_findings(tmp_path):
    report = compare(tmp_path / "missing")

    assert report["status"] == "FAIL"
    assert {issue["code"] for issue in report["issues"]} >= {
        "TOOL_DIRECTORY_ERROR", "METHOD_DIRECTORY_ERROR", "INVALID_SENSOR_CONFIG",
    }
