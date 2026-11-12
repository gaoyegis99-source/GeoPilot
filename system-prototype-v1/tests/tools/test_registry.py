import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from geopilot.domain.tool_contract import ToolContract
from geopilot.tools.registry import RegisteredTool, ToolRegistry


def tool(tool_id, *, version=1, runner="python", implementation="calculate-area"):
    return ToolContract(
        id=tool_id,
        version=version,
        runner=runner,
        implementation=implementation,
        inputs={"source": "vector_polygon"},
        outputs={"result": "table"},
        parameters={"options": {"type": "json", "default": {"weights": [1, 2]}}},
        measurement_policy={"distance": "not_applicable", "area": "not_applicable"},
        preconditions=[],
        check_rules=[],
    )


def test_register_lookup_version_order_and_runner_filter(tmp_path):
    registry = ToolRegistry(qgis_process=tmp_path / "missing-qgis", python_implementations=("calculate-area",))
    registry.register(tool("z-area", version=2, implementation="future-area"))
    registry.register(tool("z-area"), required_modules=("math", "json.decoder"))
    registry.register(tool("a-qgis", runner="qgis", implementation="native:buffer"))

    assert [(row.contract.id, row.contract.version) for row in registry.list()] == [
        ("a-qgis", 1), ("z-area", 1), ("z-area", 2),
    ]
    assert [row.contract.version for row in registry.list(runner="python")] == [1, 2]
    assert [row.contract.id for row in registry.list(runner="qgis")] == ["a-qgis"]
    assert registry.list(available_only=True) == [registry.get("z-area", 1)]
    assert registry.get("z-area", 1) == RegisteredTool(tool("z-area"), available=True, reason=None)
    latest = registry.get("z-area")
    assert latest.contract.version == 2 and not latest.available
    assert "future-area" in latest.reason
    with pytest.raises(KeyError):
        registry.get("missing")
    with pytest.raises(KeyError):
        registry.get("z-area", 3)
    with pytest.raises(KeyError):
        registry.get("z-area", True)
    with pytest.raises(ValueError, match="z-area"):
        registry.register(tool("z-area", implementation="other"))
    assert registry.get("z-area", 1).contract.implementation == "calculate-area"


def test_missing_dependency_explained(tmp_path):
    registry = ToolRegistry(python_implementations=("calculate-area",))
    dependency = "geopilot_p2_02_missing_dependency"
    registry.register(tool("area"), required_modules=(dependency,))

    registered = registry.get("area")
    assert not registered.available
    assert dependency in registered.reason
    assert not registry.list(available_only=True)


def test_dependency_discovery_does_not_import_parent_package(tmp_path, monkeypatch):
    package_name = "geopilot_p2_02_test_dependency"
    package = tmp_path / package_name
    package.mkdir()
    (package / "__init__.py").write_text('raise RuntimeError("unexpected import")\n', encoding="utf-8")
    (package / "core.py").touch()
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = ToolRegistry(python_implementations=("calculate-area",))
    registry.register(tool("area"), required_modules=(f"{package_name}.core",))

    assert registry.get("area").available
    assert package_name not in sys.modules


def test_nested_contract_mutations_cannot_change_registry():
    registry = ToolRegistry(python_implementations=("calculate-area",))
    original = tool("area")
    registry.register(original)

    original.inputs["source"] = "vector_line"
    original.parameters["options"].default["weights"].append(3)
    from_lookup = registry.get("area")
    assert from_lookup.contract.inputs == {"source": "vector_polygon"}
    assert from_lookup.contract.parameters["options"].default == {"weights": [1, 2]}

    from_lookup.contract.parameters["options"].default["weights"].clear()
    registry.list()[0].contract.outputs.clear()
    assert registry.get("area").contract == tool("area")


def test_concurrent_registration_rejects_duplicate_versions():
    registry = ToolRegistry(python_implementations=("calculate-area",))

    def register_one(_):
        try:
            registry.register(tool("area"))
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(register_one, range(4))).count(True) == 1
    assert len(registry.list()) == 1


def test_explicit_executable_takes_precedence_over_environment(tmp_path, monkeypatch):
    missing = tmp_path / "missing-qgis"
    monkeypatch.setenv("GEOPILOT_QGIS_PROCESS", sys.executable)
    registry = ToolRegistry(qgis_process=missing)
    registry.register(tool("qgis-buffer", runner="qgis", implementation="native:buffer"))

    registered = registry.get("qgis-buffer")
    assert not registered.available
    assert str(missing) in registered.reason
    assert registry.qgis_version is None


def test_missing_path_and_failing_cli_have_distinct_reasons(monkeypatch):
    monkeypatch.delenv("GEOPILOT_QGIS_PROCESS", raising=False)
    monkeypatch.setenv("PATH", "")
    qgis_tool = tool("qgis-buffer", runner="qgis", implementation="native:buffer")

    registry = ToolRegistry()
    registry.register(qgis_tool)
    missing = registry.get("qgis-buffer")
    assert not missing.available
    assert "qgis_process" in missing.reason and "executable" in missing.reason

    registry = ToolRegistry(qgis_process=sys.executable)
    registry.register(qgis_tool)
    failed = registry.get("qgis-buffer")
    assert not failed.available
    assert "exit code" in failed.reason


@pytest.mark.parametrize("failure", ["timeout", "non_json", "invalid_catalog"])
def test_catalog_probe_failures_are_unavailable(monkeypatch, failure):
    def fail_probe(args, *, cwd, timeout, **kwargs):
        assert args == [sys.executable, "--json", "list"]
        assert Path(cwd).is_dir() and Path(cwd) != Path.cwd()
        assert 0 < timeout < 60
        assert kwargs.get("shell", False) is False
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, timeout)
        stdout = "not JSON" if failure == "non_json" else '{"providers": []}'
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", fail_probe)
    registry = ToolRegistry(qgis_process=sys.executable)
    registry.register(tool("qgis-buffer", runner="qgis", implementation="native:buffer"))

    registered = registry.get("qgis-buffer")
    assert not registered.available
    assert ("timed out" if failure == "timeout" else "JSON") in registered.reason


def test_real_qgis_catalog_only_exposes_registered_contracts(monkeypatch):
    executable = os.getenv("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if not executable:
        pytest.skip("需要真实 qgis_process；请配置 GEOPILOT_QGIS_PROCESS")
    monkeypatch.setenv("GEOPILOT_QGIS_PROCESS", executable)
    registry = ToolRegistry()
    registry.register(tool("qgis-buffer", runner="qgis", implementation="native:buffer"))
    registry.register(tool("unknown-algorithm", runner="qgis", implementation="native:geopilot_missing"))

    supported = registry.get("qgis-buffer")
    assert supported.available, supported.reason
    assert supported.reason is None
    assert registry.qgis_version
    missing = registry.get("unknown-algorithm")
    assert not missing.available and "native:geopilot_missing" in missing.reason
    assert [row.contract.id for row in registry.list()] == ["qgis-buffer", "unknown-algorithm"]
    assert registry.list(runner="qgis", available_only=True) == [supported]
    with pytest.raises(KeyError):
        registry.get("native:buffer")
    with pytest.raises(KeyError):
        registry.get("native:centroids")
