import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/quality/validate_config_schemas.py"
METHOD = "config/methods/attribute-filter-export.v1.yaml"
ROAD_EXAMPLE = "config/methods/road-buffer-statistics.v1.yaml"
TOOL = "config/tools/qgis-extract-by-attribute.yaml"
SENSOR = "config/sensors/landsat-8-c2-l2sp.v1.yaml"
BINDING = "config/runner_bindings/qgis-extract-by-attribute.yaml"
SCHEMAS = "artifacts/schemas"


@pytest.fixture
def config_root(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / SCHEMAS, tmp_path / SCHEMAS)
    return tmp_path


def invoke(root, *, env=None):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        env={**os.environ, **(env or {}), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stderr == ""
    return result.returncode, json.loads(result.stdout)


def write_yaml(path, document):
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def test_current_catalog_passes_without_probing_qgis(config_root):
    marker = config_root / "qgis-called"
    sentinel = config_root / "qgis-process"
    sentinel.write_text('#!/bin/sh\ntouch "$GEOPILOT_TEST_QGIS_MARKER"\nexit 1\n', encoding="utf-8")
    sentinel.chmod(0o700)

    code, report = invoke(config_root, env={
        "GEOPILOT_QGIS_PROCESS": str(sentinel),
        "GEOPILOT_TEST_QGIS_MARKER": str(marker),
    })

    assert code == 0
    assert report["status"] == "PASS"
    counts = report["counts"]
    for family in ("tools", "methods", "sensors"):
        assert counts[family] == sum(
            path.suffix in {".yaml", ".yml"} for path in (config_root / "config" / family).iterdir()
        )
    assert counts["qgis_bindings"] > 0
    assert counts["schemas_expected"] == counts["schemas_matching"]
    assert counts["schemas_matching"] == len(list((config_root / SCHEMAS).glob("*.schema.json")))
    assert report["findings"] == []
    examples = report["known_catalog_example_findings"]
    assert len(examples) == 10
    assert {example["subject"] for example in examples} == {"road-buffer-statistics@1"}
    assert not marker.exists()


def test_duplicate_versions_fail_in_all_three_catalogs(config_root):
    for source in (METHOD, TOOL, SENSOR):
        original = config_root / source
        shutil.copyfile(original, original.with_stem("duplicated-version"))

    code, report = invoke(config_root)

    assert code == 1 and report["status"] == "FAIL"
    issues = {(finding["code"], finding["path"]) for finding in report["findings"]}
    assert ("INVALID_METHOD_CONFIG", METHOD) in issues
    assert ("DUPLICATE_TOOL", TOOL) in issues
    assert ("INVALID_SENSOR_CONFIG", SENSOR) in issues


def test_unknown_fields_fail_in_all_three_config_models(config_root):
    for source in (METHOD, TOOL, SENSOR):
        path = config_root / source
        path.write_text(path.read_text(encoding="utf-8") + "\nunknown_field: true\n", encoding="utf-8")

    code, report = invoke(config_root)

    assert code == 1
    issues = {finding["path"]: finding for finding in report["findings"]}
    assert issues[METHOD]["field"] == issues[TOOL]["field"] == "unknown_field"
    assert "unknown_field" in issues[SENSOR]["message"]


def test_invalid_dependencies_bindings_and_sensor_refs_fail(config_root):
    method = config_root / METHOD
    definition = yaml.safe_load(method.read_text(encoding="utf-8"))
    definition["steps"][0]["tool"]["version"] = 999
    definition["steps"][0]["inputs"]["source"]["asset"] = "not_declared"
    write_yaml(method, definition)

    bindings = config_root / BINDING
    declared = yaml.safe_load(bindings.read_text(encoding="utf-8"))
    declared["native:extractbyattribute"]["inputs"]["source"] = "BAD ALIAS"
    declared["native:unregistered"] = {}
    write_yaml(bindings, declared)

    sensor = config_root / SENSOR
    product = yaml.safe_load(sensor.read_text(encoding="utf-8"))
    product["quality_rules"]["absent_band"] = product["quality_rules"]["quality"]
    write_yaml(sensor, product)

    code, report = invoke(config_root)

    assert code == 1
    issues = {(finding["code"], finding["field"]) for finding in report["findings"]}
    assert ("TOOL_NOT_REGISTERED", "steps.0.tool") in issues
    assert ("UNKNOWN_ASSET_ROLE", "steps.0.inputs.source.asset") in issues
    assert ("INVALID_PARAMETER", "bindings.native:extractbyattribute.inputs") in issues
    assert ("UNUSED_QGIS_BINDING", "native:unregistered") in issues
    assert ("INVALID_SENSOR_CONFIG", "$") in issues
    assert any("must reference declared quality bands" in issue["message"] for issue in report["findings"])


def test_legacy_example_does_not_hide_a_new_invalid_reference(config_root):
    path = config_root / ROAD_EXAMPLE
    example = yaml.safe_load(path.read_text(encoding="utf-8"))
    example["steps"][0]["tool"] = {"id": "unknown-tool", "version": 1}
    write_yaml(path, example)

    code, report = invoke(config_root)

    assert code == 1
    assert report["known_catalog_example_findings"] == []
    assert any(
        finding["code"] == "TOOL_NOT_REGISTERED" and finding["subject"] == "road-buffer-statistics@1"
        for finding in report["findings"]
    )


def test_ambiguous_sensor_selector_fails_even_with_distinct_ids(config_root):
    sensor = config_root / SENSOR
    product = yaml.safe_load(sensor.read_text(encoding="utf-8"))
    product["id"] = "alternate-landsat-product"
    write_yaml(sensor.with_stem("alternate-landsat-product"), product)

    code, report = invoke(config_root)

    assert code == 1
    errors = [finding for finding in report["findings"] if finding["code"] == "INVALID_SENSOR_LOOKUP"]
    assert len(errors) == 2
    assert all("Multiple sensor configurations match" in error["message"] for error in errors)


def test_schema_drift_is_located_and_dialect_is_checked(config_root):
    path = config_root / SCHEMAS / "method-definition.schema.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["properties"]["version"]["exclusiveMinimum"] = 1
    document["$schema"] = "https://json-schema.org/draft-07/schema#"
    path.write_text(json.dumps(document), encoding="utf-8")
    (config_root / SCHEMAS / "unexpected.schema.json").write_text("{}", encoding="utf-8")

    code, report = invoke(config_root)

    assert code == 1
    counts = report["counts"]
    assert counts["schemas_matching"] == counts["schemas_expected"] - 1
    assert {(issue["code"], issue["field"]) for issue in report["findings"]} == {
        ("SCHEMA_DRIFT", "/$schema"),
        ("SCHEMA_DRIFT", "/properties/version/exclusiveMinimum"),
        ("UNMAPPED_SCHEMA", "/"),
    }


def test_schema_json_duplicate_keys_fail_even_when_values_are_identical(config_root):
    path = config_root / SCHEMAS / "method-ref.schema.json"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("{", '{"title":"MethodRef",', 1), encoding="utf-8")

    code, report = invoke(config_root)

    assert code == 1
    assert len(report["findings"]) == 1
    (finding,) = report["findings"]
    assert finding["code"] == "SCHEMA_READ_ERROR"
    assert "Duplicate JSON key 'title'" in finding["message"]
