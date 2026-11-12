"""Start the GeoPilot API and GIS scheduler on the local loopback interface."""

import argparse
import re
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI

from geopilot.api.app import create_app
from geopilot.application.land_use_checks import LAND_USE_CHECKS
from geopilot.application.plan_runner import execute_claimed_recheck, execute_claimed_run
from geopilot.application.remote_sensing_checks import REMOTE_SENSING_CHECKS
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.checks.raster_checks import DEM_ARTIFACT_RULES
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.step_support import PreparedStep
from geopilot.llm.base import ModelProvider
from geopilot.methods.land_use_overlay import preflight_land_use_step
from geopilot.methods.loader import _UniqueKeySafeLoader, load_method_catalog
from geopilot.persistence.schema import initialize_database
from geopilot.remote_sensing.config_registry import load_sensor_registry
from geopilot.runs.models import Run
from geopilot.tools.registry import ToolRegistry


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_WORKER_DIRECTORIES = (_SOURCE_ROOT / "gis/tools", _SOURCE_ROOT / "methods")
_LOCAL_IMPLEMENTATION = re.compile(r"geopilot\.([a-z][a-z0-9_]*)")


def _preflight(step: PreparedStep) -> None:
    preflight_step_inputs(step)
    preflight_land_use_step(step)


def _yaml_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.suffix in {".yaml", ".yml"} and path.is_file())


def _worker_paths(contracts: list[ToolContract]) -> dict[str, Path]:
    paths = {}
    for contract in contracts:
        implementation = _LOCAL_IMPLEMENTATION.fullmatch(contract.implementation)
        if contract.runner == "python" and implementation is not None:
            name = f"{implementation[1]}.py"
            candidates = [folder / name for folder in _WORKER_DIRECTORIES if (folder / name).is_file()]
            if len(candidates) > 1:
                raise ValueError(f"Multiple local workers match {contract.implementation!r}")
            if candidates:
                paths[contract.implementation] = candidates[0]
    return paths


def _qgis_bindings(directory: Path) -> dict:
    bindings = {}
    for path in _yaml_files(directory):
        declared = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
        if not isinstance(declared, dict) or declared.keys() & bindings.keys():
            raise ValueError(f"Invalid or duplicate QGIS bindings in {path}")
        bindings.update(declared)
    return bindings


def create_local_app(
    database: Path, workspace: Path, *, config: Path = _PROJECT_ROOT / "config", qgis_process: Path | None = None,
    model_provider: ModelProvider | None = None, model_timeout: float = 30.0,
) -> FastAPI:
    database = database.resolve()
    workspace = workspace.resolve()
    catalog = load_method_catalog(config / "methods")
    if catalog.issues:
        first = catalog.issues[0]
        raise ValueError(f"{first.path}: {first.field}: {first.message}")
    contracts = [
        ToolContract.model_validate(yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader))
        for path in _yaml_files(config / "tools")
    ]
    python_paths = _worker_paths(contracts)
    qgis_bindings = _qgis_bindings(config / "runner_bindings")
    registry = ToolRegistry(qgis_process=qgis_process, python_implementations=python_paths)
    for contract in contracts:
        registry.register(contract)
    database.parent.mkdir(parents=True, exist_ok=True)
    initialize_database(database)
    checks = {**RUN_CHECKS, **LAND_USE_CHECKS, **REMOTE_SENSING_CHECKS}
    sensor_config = config / "sensors"
    sensor_registry = load_sensor_registry(sensor_config) if sensor_config.is_dir() else None

    def execute(run: Run) -> None:
        execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry, qgis_bindings=qgis_bindings,
            python_implementations=python_paths, qgis_process=qgis_process, input_preflight=_preflight,
            check_rules=checks, artifact_rules=DEM_ARTIFACT_RULES,
        )

    def recheck(run, attempt) -> None:
        execute_claimed_recheck(
            database, run, attempt, registry=registry, qgis_bindings=qgis_bindings,
            python_implementations=python_paths, check_rules=checks, artifact_rules=DEM_ARTIFACT_RULES,
        )

    return create_app(
        database, workspace_root=workspace, catalog=catalog, registry=registry, execute_run=execute,
        execute_recheck=recheck, registered_rules=(*checks, *DEM_ARTIFACT_RULES),
        model_provider=model_provider, model_timeout=model_timeout, sensor_registry=sensor_registry,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=_PROJECT_ROOT / "config")
    parser.add_argument("--qgis-process", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ollama-model", help="Configured local Ollama model name")
    parser.add_argument("--ollama-port", type=int, default=11434)
    parser.add_argument("--model-timeout", type=float, default=30.0)
    args = parser.parse_args()
    provider = None
    if args.ollama_model:
        from geopilot.llm.providers.ollama import OllamaModelProvider

        provider = OllamaModelProvider(args.ollama_model, port=args.ollama_port)
    app = create_local_app(
        args.database, args.workspace, config=args.config, qgis_process=args.qgis_process,
        model_provider=provider, model_timeout=args.model_timeout,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
