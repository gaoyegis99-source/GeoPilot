"""Execute an explicitly configured local Python tool through the step protocol."""

import json
import os
import platform
import sys
from collections.abc import Mapping
from pathlib import Path

from geopilot.execution.output_staging import (
    OUTPUT_STAGING_ENV,
    owned_output_staging,
)
from geopilot.execution.step_support import AdapterError, PreparedStep, execute_adapter, run_process
from geopilot.tools.registry import ToolRegistry


def _script_for(step: PreparedStep, implementations: Mapping[str, str | Path]) -> Path:
    implementation = step.contract.implementation
    try:
        configured = Path(implementations[implementation])
        if not configured.is_absolute():
            raise ValueError("Script configuration must use an absolute path")
        script = configured.resolve(strict=True)
        if not script.is_file():
            raise ValueError("Script configuration must point to a regular file")
        return script
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise AdapterError(
            f"Python implementation {implementation!r} cannot be started: {error}",
            code="PROCESS_START_FAILED", field="tool.implementation",
        ) from error


def _new_manifest_path(step: PreparedStep) -> Path:
    manifest = step.result_path.with_name("python-result.json")
    if manifest.exists() or manifest.is_symlink():
        raise AdapterError(
            f"Python child manifest location is already in use: {manifest.name}",
            code="OUTPUT_INVALID", field="outputs",
        )
    return manifest


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate manifest key: {key!r}")
        result[key] = value
    return result


def _manifest_error(message: str, *, field: str = "outputs", code: str = "OUTPUT_INVALID") -> AdapterError:
    return AdapterError(message, code=code, field=field, exit_code=0)


def _read_manifest(step: PreparedStep, manifest: Path) -> dict[str, str]:
    if manifest.is_symlink() or (manifest.exists() and not manifest.is_file()):
        raise _manifest_error("Python child manifest must be a regular file")
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    except FileNotFoundError as error:
        raise _manifest_error("Python child did not write its result manifest", code="OUTPUT_MISSING") from error
    except (OSError, ValueError) as error:
        raise _manifest_error(f"Cannot read Python child result manifest: {error}") from error

    if not isinstance(document, dict) or set(document) - {"outputs", "environment"}:
        raise _manifest_error("Expected a child manifest with outputs and optional environment")
    outputs = document.get("outputs")
    if not isinstance(outputs, dict):
        raise _manifest_error("Python child manifest outputs must be an object")

    expected = step.request["output_targets"]
    unexpected = sorted(outputs.keys() - expected.keys())
    if unexpected:
        role = unexpected[0]
        raise _manifest_error(f"Python child reported an unexpected output: {role!r}", field=f"outputs.{role}")
    for role, target in expected.items():
        field = f"outputs.{role}"
        if role not in outputs:
            raise _manifest_error(f"Python child did not report {role!r}", field=field, code="OUTPUT_MISSING")
        actual = outputs[role]
        if isinstance(actual, str):
            actual = {"path": actual}
        if (
            not isinstance(actual, dict)
            or set(actual) - {"path", "layer"}
            or actual.get("path") != target["path"]
            or actual.get("layer") != target.get("layer")
        ):
            raise _manifest_error(f"Python child output {role!r} does not match the requested path and layer", field=field)

    environment = document.get("environment", {})
    if not isinstance(environment, dict) or any(not isinstance(value, str) for value in environment.values()):
        raise _manifest_error("Python child environment versions must be strings", field="environment")
    return {**environment, "python": platform.python_version()}


def invoke_python(
    step: PreparedStep, *, implementations: Mapping[str, str | Path], timeout: float = 60
) -> dict[str, str]:
    """Run the configured script for an already prepared step."""
    script = _script_for(step, implementations)
    manifest = _new_manifest_path(step)
    env = os.environ.copy()
    env.pop(OUTPUT_STAGING_ENV, None)
    source_root = str(Path(__file__).resolve().parents[2])
    inherited_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join((source_root, inherited_path)) if inherited_path else source_root
    env_context = None
    if step.request["output_targets"]:
        env_context = owned_output_staging(step.workdir, step.request, env)
    run_process(
        step,
        [
            sys.executable, str(script),
            "--request", step.request_path.relative_to(step.workdir).as_posix(),
            "--result", manifest.relative_to(step.workdir).as_posix(),
        ],
        timeout=timeout,
        env=env,
        env_context=env_context,
    )
    return _read_manifest(step, manifest)


def run_python(
    request: Mapping[str, object],
    *,
    registry: ToolRegistry,
    workdir: str | Path,
    implementations: Mapping[str, str | Path],
    timeout: float = 60,
) -> dict[str, object]:
    """Run one registered Python tool and verify its named output manifest."""
    def invoke(step: PreparedStep) -> dict[str, str]:
        return invoke_python(step, implementations=implementations, timeout=timeout)

    return execute_adapter(request, registry=registry, workdir=workdir, runner="python", invoke=invoke)
