import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.models import MethodDefinition
from geopilot.methods.validator import validate_method
from geopilot.plans.dependency_validator import validate_dependencies
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/methods/method_cli.py"
BUFFER = ROOT / "config/tools/qgis-buffer.yaml"
PYTHON_AREA = ROOT / "config/tools/python-area.yaml"


def invoke(cwd, *args, env=None):
    process = subprocess.run(
        [sys.executable, str(CLI), "template", *map(str, args)],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    assert not process.stderr
    report = json.loads(process.stdout)
    assert report["schema_version"] == 1
    assert report["command"] == "template"
    assert report["status"] == ("ok" if process.returncode == 0 else "error")
    return process.returncode, report


def error(cwd, args, expected_exit, code, field):
    exit_code, report = invoke(cwd, *args)
    assert exit_code == expected_exit
    assert report["data"] is None
    (finding,) = report["errors"]
    assert set(finding) == {"code", "field", "message"}
    assert (finding["code"], finding["field"]) == (code, field)
    return finding


def test_registered_tool_template_uses_declared_ports_and_never_writes_or_probes(tmp_path):
    existing = tmp_path / "method.yaml"
    existing.write_text("existing review draft\n", encoding="utf-8")
    marker = tmp_path / "qgis-was-called"
    probe = tmp_path / "qgis"
    probe.write_text('#!/bin/sh\ntouch "$GEOPILOT_TEMPLATE_MARKER"\nexit 1\n', encoding="utf-8")
    probe.chmod(0o700)
    source = BUFFER.read_bytes()
    args = ("--tool", "qgis-buffer@1", "--method", "buffer-draft@7")
    env = {"GEOPILOT_QGIS_PROCESS": str(probe), "GEOPILOT_TEMPLATE_MARKER": str(marker)}

    exit_code, report = invoke(tmp_path, *args, env=env)

    assert exit_code == 0 and report["errors"] == []
    assert invoke(tmp_path, *args, env=env) == (exit_code, report)
    data = report["data"]
    assert set(data) == {"scope", "tool", "method", "todos"}
    assert data["scope"] == "method_template"
    contract = ToolContract.model_validate(data["tool"])
    assert contract == ToolContract.model_validate(yaml.safe_load(source))
    method = MethodDefinition.model_validate(data["method"])
    assert (method.id, method.version) == ("buffer-draft", 7)
    (step,) = method.steps
    assert step.tool == contract.ref
    assert step.inputs["source"].asset == "source" and step.outputs == contract.outputs
    assert method.inputs == contract.inputs
    assert set(step.parameters) == set(contract.parameters)
    assert all(binding.option == name for name, binding in step.parameters.items())
    assert method.critical_options == contract.parameters
    assert all("default" not in spec for spec in data["method"]["critical_options"].values())
    assert {(todo["code"], todo["field"]) for todo in data["todos"]} == {
        ("REQUIRED_OPTION_VALUE", f"tool.parameters.{name}") for name in contract.parameters
    }
    assert existing.read_text(encoding="utf-8") == "existing review draft\n"
    assert BUFFER.read_bytes() == source
    assert set(tmp_path.iterdir()) == {existing, probe}


def test_exact_version_and_false_zero_defaults_are_preserved(tmp_path):
    config = tmp_path / "配置 catalog"
    directory = config / "tools"
    directory.mkdir(parents=True)
    raw = yaml.safe_load(PYTHON_AREA.read_text(encoding="utf-8"))
    (directory / "area.v1.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    raw["version"] = 2
    raw["parameters"].update({
        "enabled": {"type": "boolean", "default": False},
        "count": {"type": "integer", "required": False, "default": 0},
        "metadata": {"type": "json", "default": {"present_null": None}},
        "optional_label": {"type": "string", "required": False},
    })
    (directory / "area.v2.yml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    exit_code, report = invoke(
        tmp_path, "--tool", "python-area@2", "--method", "area-review@1", "--config", config.name,
    )

    assert exit_code == 0
    data = report["data"]
    contract = ToolContract.model_validate(data["tool"])
    assert contract.version == 2
    options = data["method"]["critical_options"]
    assert options["enabled"]["default"] is False
    assert options["count"]["default"] == 0 and type(options["count"]["default"]) is int
    assert options["metadata"]["default"] == {"present_null": None}
    assert "optional_label" not in options
    assert "optional_label" not in data["method"]["steps"][0]["parameters"]
    assert ("OPTIONAL_PARAMETER_UNBOUND", "tool.parameters.optional_label") in {
        (todo["code"], todo["field"]) for todo in data["todos"]
    }
    method = MethodDefinition.model_validate(data["method"])
    registry = ToolRegistry(python_implementations=[contract.implementation])
    registry.register(contract)
    assert validate_method(method, registry=registry, registered_rules=()) == ()
    assert validate_dependencies(method.inputs, method.steps) == ()


@pytest.mark.parametrize(("tool", "method", "field"), [
    ("qgis-buffer", "buffer-draft@1", "tool"),
    ("qgis-buffer@0", "buffer-draft@1", "tool"),
    ("qgis-buffer@1.0", "buffer-draft@1", "tool"),
    ("@1", "buffer-draft@1", "tool"),
    ("qgis-buffer@1", "buffer-draft@-1", "method"),
])
def test_invalid_exact_references_are_structured(tmp_path, tool, method, field):
    error(tmp_path, ("--tool", tool, "--method", method), 1, "INVALID_REFERENCE", field)


@pytest.mark.parametrize("tool", ["qgis-buffer@999", "no-such-tool@1"])
def test_unknown_tool_or_version_does_not_fallback(tmp_path, tool):
    error(tmp_path, ("--tool", tool, "--method", "buffer-draft@1"), 1, "TOOL_NOT_REGISTERED", "tool")


def test_missing_config_is_an_io_error(tmp_path):
    args = ("--tool", "qgis-buffer@1", "--method", "buffer-draft@1", "--config", "missing-config")
    error(tmp_path, args, 2, "INPUT_READ_ERROR", "config")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(("document", "code"), [
    (b"id: first\nid: duplicate\n", "INVALID_TOOL_CONFIG"),
    (b"id: [\n", "INVALID_TOOL_CONFIG"),
    (BUFFER.read_bytes() + b"unexpected: true\n", "INVALID_TOOL_CONFIG"),
    (BUFFER.read_bytes(), "DUPLICATE_TOOL_REFERENCE"),
])
def test_invalid_or_duplicated_tool_configs_fail_closed(tmp_path, document, code):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "buffer.yaml").write_bytes(BUFFER.read_bytes())
    (tools / "second.yaml").write_bytes(document)
    args = ("--tool", "qgis-buffer@1", "--method", "buffer-draft@1", "--config", ".")

    finding = error(tmp_path, args, 1, code, "config")

    assert "second.yaml" in finding["message"]
