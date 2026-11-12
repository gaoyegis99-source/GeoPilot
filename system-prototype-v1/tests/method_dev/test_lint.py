import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/methods/method_cli.py"
ATTRIBUTE = "methods/attribute-filter-export.v1.yaml"
SPATIAL = "methods/spatial-select-and-clip.v1.yaml"
EXAMPLE = "methods/road-buffer-statistics.v1.yaml"
VARIANTS = "methods/road-buffer-statistics.v3.yaml"


@pytest.fixture
def config(tmp_path):
    return shutil.copytree(ROOT / "config", tmp_path / "review-config")


def run_lint(config, *args, cwd=ROOT, env=None):
    result = subprocess.run(
        [sys.executable, str(CLI), "lint", "--config", str(config), *map(str, args)],
        cwd=cwd, capture_output=True, text=True, check=False,
        env={**os.environ, **(env or {}), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert not result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1 and report["command"] == "lint"
    assert set(report) == {"schema_version", "command", "status", "data", "errors"}
    if result.returncode:
        assert report["status"] == "error" and report["data"] is None and report["errors"]
        assert all(set(item) == {"code", "field", "message", "path"} for item in report["errors"])
    else:
        assert report["status"] == "ok" and not report["errors"]
        assert report["data"]["scope"] == "method_static_lint"
        assert "candidate" not in report["data"]
    return result.returncode, report


def read_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_yaml(path, value):
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def locations(report):
    return {(item["code"], item["path"], item["field"]) for item in report["errors"]}


def test_formal_catalog_lints_without_runtime_probe_or_file_writes(config, tmp_path):
    sentinel = tmp_path / "qgis-process"
    sentinel.write_text('#!/bin/sh\ntouch "$GEOPILOT_TEST_QGIS_MARKER"\nexit 90\n', encoding="utf-8")
    sentinel.chmod(0o700)
    marker = tmp_path / "qgis-probed"
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    code, report = run_lint(config, cwd=tmp_path, env={
        "GEOPILOT_QGIS_PROCESS": str(sentinel),
        "GEOPILOT_TEST_QGIS_MARKER": str(marker),
    })
    assert code == 0
    data = report["data"]
    assert data["selection"] == "catalog"
    assert data["tools_checked"] == len(list((config / "tools").glob("*.yaml")))
    expected = {}
    for path in (config / "methods").glob("*.yaml"):
        document = read_yaml(path)
        expected[(document["id"], document["version"])] = f"config/{path.relative_to(config).as_posix()}"
    example_path = expected.pop(("road-buffer-statistics", 1))
    assert {(row["id"], row["version"]): row["path"] for row in data["methods"]} == expected
    example, = data["catalog_examples"]
    assert example["path"] == example_path and example["disposition"] == "catalog_example_only"
    assert len(example["diagnostics"]) == 10
    assert {(issue["code"], issue["field"]) for issue in example["diagnostics"]} == {
        *(("MISSING_TOOL_BINDING", f"steps.{index}.tool") for index in range(7)),
        *(("MISSING_ARTIFACT_BINDING", f"expected_artifacts.{index}") for index in range(3)),
    }
    assert all(issue["path"] == example_path for issue in example["diagnostics"])
    assert not marker.exists()
    assert before == {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_single_method_can_be_relative_to_the_callers_directory(config, tmp_path):
    method = tmp_path / "candidate.yaml"
    shutil.copyfile(config / ATTRIBUTE, method)
    code, report = run_lint(config, "--method", method.name, cwd=tmp_path)
    assert code == 0
    assert report["data"]["selection"] == "single_method"
    assert report["data"]["catalog_examples"] == []
    assert report["data"]["methods"] == [{
        "id": "attribute-filter-export", "version": 1, "path": method.as_posix(), "locked_order": True,
    }]


def test_historical_example_is_not_a_valid_single_method(config):
    code, report = run_lint(config, "--method", config / EXAMPLE)
    assert code == 1 and len(report["errors"]) == 10
    assert {issue["path"] for issue in report["errors"]} == {f"config/{EXAMPLE}"}


@pytest.mark.parametrize("change", ("unknown_rule", "unregistered_version", "new_invalid_dependency", "renamed"))
def test_historical_exception_does_not_hide_a_new_error_or_a_copy(config, change):
    path = config / EXAMPLE
    method = read_yaml(path)
    if change == "unknown_rule":
        field = f"check_rules.{len(method['check_rules'])}"
        method["check_rules"].append("unregistered_review_rule")
        expected = "UNKNOWN_CHECK_RULE"
    elif change == "unregistered_version":
        method["steps"][0]["tool"] = {"id": "qgis-expand-analysis-extent", "version": 999}
        field, expected = "steps.0.tool", "TOOL_NOT_REGISTERED"
    elif change == "new_invalid_dependency":
        method["steps"][0]["inputs"] = {"source": {"asset": "not_declared"}}
        field, expected = "steps.0.inputs.source.asset", "UNKNOWN_ASSET_ROLE"
    else:
        new_path = path.with_name("copied-example.yaml")
        path.rename(new_path)
        path = new_path
        field, expected = "steps.0.tool", "MISSING_TOOL_BINDING"
    write_yaml(path, method)
    code, report = run_lint(config)
    assert code == 1
    assert (expected, f"config/{path.relative_to(config).as_posix()}", field) in locations(report)


def test_missing_tool_version_roles_outputs_and_registered_rules_are_located(config):
    path = config / ATTRIBUTE
    method = read_yaml(path)
    method["steps"][0]["tool"]["version"] = 999
    method["steps"][0]["inputs"]["source"]["asset"] = "unbound_asset"
    method["expected_artifacts"][0]["output"] = "missing"
    method["check_rules"] = ["unknown_check_rule"]
    method["preconditions"] = ["unknown_precondition"]
    write_yaml(path, method)
    code, report = run_lint(config, "--method", path)
    assert code == 1
    assert locations(report) == {
        (rule, f"config/{ATTRIBUTE}", field)
        for rule, field in (
            ("TOOL_NOT_REGISTERED", "steps.0.tool"),
            ("UNKNOWN_ASSET_ROLE", "steps.0.inputs.source.asset"),
            ("UNKNOWN_ARTIFACT_OUTPUT", "expected_artifacts.0.output"),
            ("UNKNOWN_CHECK_RULE", "check_rules.0"),
            ("UNKNOWN_PRECONDITION", "preconditions.0"),
        )
    }


def test_registered_tool_ports_and_type_are_checked(config):
    path = config / ATTRIBUTE
    method = read_yaml(path)
    method["steps"][0]["inputs"]["wrong_port"] = method["steps"][0]["inputs"].pop("source")
    method["steps"][0]["outputs"]["filtered"] = "vector_polygon"
    write_yaml(path, method)
    code, report = run_lint(config, "--method", path)
    assert code == 1
    assert {
        ("MISSING_INPUT_PORT", f"config/{ATTRIBUTE}", "steps.0.inputs.source"),
        ("UNKNOWN_INPUT_PORT", f"config/{ATTRIBUTE}", "steps.0.inputs.wrong_port"),
        ("OUTPUT_TYPE_MISMATCH", f"config/{ATTRIBUTE}", "steps.0.outputs.filtered"),
    } <= locations(report)


@pytest.mark.parametrize("cycle", (False, True))
def test_dependencies_keep_the_declared_order_and_detect_cycles(config, cycle):
    path = config / SPATIAL
    method = read_yaml(path)
    if cycle:
        method["steps"][0]["inputs"]["targets"] = {"step": "clip_back_to_analysis_extent", "output": "targets"}
    else:
        method["steps"].reverse()
    assert method["locked_order"] is True
    write_yaml(path, method)
    code, report = run_lint(config, "--method", path)
    assert code == 1
    assert ("FUTURE_STEP_REFERENCE", f"config/{SPATIAL}", "steps.0.inputs.targets.step") in locations(report)
    assert any(item["code"] == "DEPENDENCY_CYCLE" for item in report["errors"]) is cycle


def test_geodesic_variant_checks_the_expanded_step_order(config):
    path = config / VARIANTS
    method = read_yaml(path)
    steps = method["step_variants"]["choices"]["geodesic"]
    steps["apply_spatial_relation"]["inputs"]["source"] = {"step": "clip_back_to_analysis_extent", "output": "targets"}
    write_yaml(path, method)
    code, report = run_lint(config, "--method", path)
    assert code == 1
    assert (
        "FUTURE_STEP_REFERENCE", f"config/{VARIANTS}",
        "step_variants.choices.geodesic.steps.4.inputs.source.step",
    ) in locations(report)
    assert any(
        item["code"] == "DEPENDENCY_CYCLE" and item["field"].startswith("step_variants.choices.geodesic.")
        for item in report["errors"]
    )


def test_unknown_tool_check_rule_is_reported_on_the_method(config):
    tool = config / "tools/qgis-extract-by-attribute.yaml"
    contract = read_yaml(tool)
    contract["check_rules"] = ["unknown_tool_rule"]
    write_yaml(tool, contract)
    code, report = run_lint(config, "--method", config / ATTRIBUTE)
    assert code == 1
    assert ("UNKNOWN_TOOL_CHECK_RULE", f"config/{ATTRIBUTE}", "steps.0.tool.check_rules.0") in locations(report)


def test_missing_python_worker_is_a_static_error(config):
    path = config / "tools/python-select-spatial-relation.yaml"
    contract = read_yaml(path)
    contract["implementation"] = "geopilot.absent_lint_test_worker"
    write_yaml(path, contract)
    code, report = run_lint(config, "--method", config / SPATIAL)
    assert code == 1
    assert ("TOOL_UNAVAILABLE", f"config/{SPATIAL}", "steps.0.tool") in locations(report)


@pytest.mark.parametrize("family,source", (("METHOD", ATTRIBUTE), ("TOOL", "tools/qgis-buffer.yaml")))
def test_duplicate_exact_versions_report_both_files(config, family, source):
    original = config / source
    duplicate = original.with_name("duplicate.yaml")
    shutil.copyfile(original, duplicate)
    code, report = run_lint(config)
    assert code == 1
    assert locations(report) == {
        (f"DUPLICATE_{family}_REFERENCE", f"config/{path.relative_to(config).as_posix()}", "version")
        for path in (original, duplicate)
    }


@pytest.mark.parametrize(
    "content,expected,field",
    [
        (b"id: one\nid: two\n", "INVALID_METHOD_YAML", "$"),
        (b"\xff", "INVALID_METHOD_YAML", "$"),
        (b"id: draft\nversion: '1'\n", "INVALID_METHOD_CONFIG", "version"),
    ],
)
def test_bad_yaml_or_model_is_not_a_registration_candidate(config, tmp_path, content, expected, field):
    candidate = tmp_path / "invalid.yaml"
    candidate.write_bytes(content)
    code, report = run_lint(config, "--method", candidate)
    assert code == 1
    assert (expected, candidate.as_posix(), field) in locations(report)
    assert candidate.read_bytes() == content


def test_missing_explicit_file_is_an_input_error(config, tmp_path):
    absent = tmp_path / "absent.yaml"
    code, report = run_lint(config, "--method", absent)
    assert code == 2
    assert locations(report) == {("INPUT_READ_ERROR", absent.as_posix(), "$")}
    assert not absent.exists()
