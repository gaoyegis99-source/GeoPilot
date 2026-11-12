"""Register versioned tool contracts and check their local dependencies."""

import json
import os
import shutil
import subprocess
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from importlib.machinery import PathFinder
from importlib.util import find_spec
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Literal

from geopilot.domain.tool_contract import ToolContract


_QGIS_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class RegisteredTool:
    contract: ToolContract
    available: bool
    reason: str | None


@dataclass(frozen=True)
class _QgisCatalog:
    algorithms: frozenset[str] = frozenset()
    version: str | None = None
    reason: str | None = None


def _parse_qgis_catalog(text: str) -> _QgisCatalog:
    document = json.loads(text)
    if not isinstance(document, dict) or not isinstance(document.get("providers"), dict):
        raise ValueError("expected a providers object")
    algorithms: set[str] = set()
    for provider_id, provider in document["providers"].items():
        if not isinstance(provider, dict) or not isinstance(provider.get("algorithms"), dict):
            raise ValueError(f"expected an algorithms object for provider {provider_id}")
        if provider.get("is_active", True):
            algorithms.update(provider["algorithms"])
    version = document.get("qgis_version")
    return _QgisCatalog(frozenset(algorithms), version if isinstance(version, str) else None)


def _probe_qgis(configured: str | os.PathLike[str] | None) -> _QgisCatalog:
    requested = configured if configured is not None else os.environ.get("GEOPILOT_QGIS_PROCESS") or "qgis_process"
    requested = os.path.expanduser(os.fspath(requested))
    try:
        executable = shutil.which(requested)
        if executable is None:
            return _QgisCatalog(reason=f"QGIS executable not found or not executable: {requested}")
        env = os.environ.copy()
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        with TemporaryDirectory(prefix="geopilot-qgis-registry-") as scratch:
            result = subprocess.run(
                [os.path.abspath(executable), "--json", "list"],
                cwd=scratch,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_QGIS_TIMEOUT_SECONDS,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return _QgisCatalog(reason=f"QGIS algorithm query timed out after {_QGIS_TIMEOUT_SECONDS} seconds")
    except (OSError, ValueError) as error:
        return _QgisCatalog(reason=f"Unable to start QGIS algorithm query ({requested}): {error}")

    if result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip())[:500]
        reason = f"QGIS algorithm query failed with exit code {result.returncode}"
        return _QgisCatalog(reason=f"{reason}: {detail}" if detail else reason)
    try:
        return _parse_qgis_catalog(result.stdout)
    except ValueError as error:
        return _QgisCatalog(reason=f"QGIS algorithm query returned an invalid JSON catalog: {error}")


def _module_exists(name: str) -> bool:
    root, *children = name.split(".")
    try:
        spec = find_spec(root)
        for child in children:
            if spec is None or spec.submodule_search_locations is None:
                return False
            # Search only inside the parent's declared locations without importing it.
            spec = PathFinder.find_spec(child, spec.submodule_search_locations)
        return spec is not None
    except (ImportError, OSError, ValueError):
        return False


class ToolRegistry:
    """Keep validated snapshots of explicitly registered tools."""

    def __init__(
        self,
        *,
        qgis_process: str | os.PathLike[str] | None = None,
        python_implementations: Iterable[str] = (),
    ) -> None:
        self._qgis_process = qgis_process
        self._python_implementations = frozenset(python_implementations)
        self._qgis_catalog: _QgisCatalog | None = None
        self._tools: dict[str, dict[int, RegisteredTool]] = {}
        self._lock = Lock()

    @property
    def qgis_version(self) -> str | None:
        """Version reported by the cached QGIS catalog probe, when known."""
        with self._lock:
            return self._qgis_catalog.version if self._qgis_catalog is not None else None

    def _unavailable_reason(self, contract: ToolContract, required_modules: Iterable[str]) -> str | None:
        missing = sorted({module for module in required_modules if not _module_exists(module)})
        if missing:
            return f"Missing Python dependencies: {', '.join(missing)}"
        if contract.runner == "python":
            if contract.implementation not in self._python_implementations:
                return f"Python implementation is not configured: {contract.implementation}"
            return None

        if self._qgis_catalog is None:
            self._qgis_catalog = _probe_qgis(self._qgis_process)
        if self._qgis_catalog.reason:
            return self._qgis_catalog.reason
        if contract.implementation not in self._qgis_catalog.algorithms:
            return f"QGIS algorithm is not available: {contract.implementation}"
        return None

    def register(self, contract: ToolContract, *, required_modules: Iterable[str] = ()) -> None:
        snapshot = ToolContract.model_validate(contract).model_copy(deep=True)
        with self._lock:
            if snapshot.version in self._tools.get(snapshot.id, {}):
                raise ValueError(f"Tool {snapshot.id} version {snapshot.version} is already registered")
            reason = self._unavailable_reason(snapshot, required_modules)
            self._tools.setdefault(snapshot.id, {})[snapshot.version] = RegisteredTool(
                contract=snapshot, available=reason is None, reason=reason,
            )

    def get(self, tool_id: str, version: int | None = None) -> RegisteredTool:
        with self._lock:
            versions = self._tools[tool_id]
            if version is None:
                version = max(versions)
            elif type(version) is not int:
                raise KeyError((tool_id, version))
            return deepcopy(versions[version])

    def list(
        self,
        *,
        runner: Literal["qgis", "python"] | None = None,
        available_only: bool = False,
    ) -> list[RegisteredTool]:
        with self._lock:
            selected = [
                tool
                for versions in self._tools.values()
                for tool in versions.values()
                if (runner is None or tool.contract.runner == runner)
                and (not available_only or tool.available)
            ]
            return deepcopy(sorted(selected, key=lambda tool: (tool.contract.id, tool.contract.version)))
