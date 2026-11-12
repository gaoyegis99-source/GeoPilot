"""Run a reference case through the existing V1 services in a private project."""

import hashlib
import json
import math
import os
import platform
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from threading import Thread

import pyogrio
import pyproj
import rasterio
import shapely
import yaml
from pydantic import ValidationError

from geopilot.api.app import create_app
from geopilot.api.server import _preflight, _qgis_bindings, _worker_paths, _yaml_files
from geopilot.application.land_use_checks import LAND_USE_CHECKS
from geopilot.application.plan_runner import execute_claimed_run
from geopilot.application.remote_sensing_checks import REMOTE_SENSING_CHECKS
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.checks.raster_checks import DEM_ARTIFACT_RULES
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.process_manager import capture_process
from geopilot.execution.worker_lease import managed_workers_stopped
from geopilot.methods.loader import load_method_catalog, parse_method_yaml
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.remote_sensing.config_registry import load_sensor_registry
from geopilot.runs.query_service import get_run_status
from geopilot.scheduler.cancellation import request_cancellation
from geopilot.scheduler.loop import run_once
from geopilot.tools.registry import ToolRegistry, _parse_qgis_catalog

from scripts.methods.lint import LintError, lint_methods, read_yaml
from scripts.methods.reference_package import (
    OutputFile, ReferencePackageError, _assert_package_intact, compare_outputs, load_reference_package,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
RUN_RULES = {**RUN_CHECKS, **LAND_USE_CHECKS, **REMOTE_SENSING_CHECKS}
RULES = (*RUN_RULES, *DEM_ARTIFACT_RULES)


class TrialError(ValueError):
    def __init__(self, code, field, message, *, exit_code=1):
        super().__init__(message)
        self.code, self.field, self.exit_code = code, field, exit_code


def _issue(code, field, message):
    return {"code": code, "field": field, "message": message}


def _remaining(deadline):
    return max(0.0, deadline - time.monotonic())


def _before_deadline(deadline, phase):
    if _remaining(deadline) <= 0:
        raise TrialError("TRIAL_TIMEOUT", phase, "The trial deadline expired")


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, allow_nan=False, indent=2)
        stream.write("\n")


def _selected_method(package, candidate, fingerprints, config):
    if candidate is None:
        directory = config / "methods"
        for source in _yaml_files(directory):
            if source.is_symlink():
                raise TrialError("UNSAFE_PATH", "config", f"Configuration symlink: {source.name}", exit_code=2)
            fingerprints[source] = _digest(source)
        catalog = load_method_catalog(directory, include_releases=False)
        if catalog.issues:
            first = catalog.issues[0]
            raise TrialError("INVALID_METHOD_CONFIG", "method", f"{first.path}: {first.field}: {first.message}")
        try:
            method = catalog.get(package.method.id, package.method.version)
        except KeyError as error:
            raise TrialError("METHOD_NOT_REGISTERED", "method", "No built-in method matches the package version") from error
        return method, method.model_dump_json(indent=2).encode("utf-8"), None

    try:
        if candidate.is_symlink() or candidate.suffix.lower() not in {".yaml", ".yml"} or not candidate.is_file():
            raise OSError("Expected an external regular YAML candidate file")
        candidate = candidate.resolve(strict=True)
        if candidate.is_relative_to(config) or candidate.is_relative_to(CONFIG.resolve()):
            raise TrialError("UNSAFE_PATH", "method", "Use the built-in selector for a method inside config/",
                             exit_code=2)
        content = candidate.read_bytes()
        method = parse_method_yaml(content.decode("utf-8"))
    except OSError as error:
        raise TrialError("INPUT_READ_ERROR", "method", str(error), exit_code=2) from error
    except (ValidationError, UnicodeError, RecursionError, yaml.YAMLError) as error:
        raise TrialError("INVALID_METHOD_CONFIG", "method", str(error)) from error
    if method.ref != package.method:
        raise TrialError("METHOD_REFERENCE_MISMATCH", "method", "Candidate ID and version must exactly match the package")
    return method, content, candidate


def _reserve_directory(requested, package, config):
    try:
        if requested.exists() or requested.is_symlink():
            raise FileExistsError("Choose a new trial directory")
        root = requested.parent.resolve(strict=True) / requested.name
        protected = [config, CONFIG.resolve(), (ROOT / "src").resolve(), (ROOT / "local-data").resolve(), package.root]
        if configured := os.environ.get("GEOPILOT_DATA_DIR"):
            protected.append(Path(configured).resolve())
        if any(root.is_relative_to(path) for path in protected):
            raise TrialError("UNSAFE_PATH", "work_dir", "Trial directory overlaps source configuration, data, or fixtures",
                             exit_code=2)
        root.mkdir(mode=0o700)
    except OSError as error:
        raise TrialError("INPUT_READ_ERROR", "work_dir", str(error), exit_code=2) from error
    return root


def _stage_config(root, method_bytes, candidate, fingerprints, config_root):
    config = root / "config"
    for family in ("tools", "runner_bindings", "sensors"):
        destination = config / family
        destination.mkdir(parents=True)
        for source in _yaml_files(config_root / family):
            if source.is_symlink():
                raise TrialError("UNSAFE_PATH", "config", f"Configuration symlink: {source.name}")
            content = source.read_bytes()
            fingerprints[source] = hashlib.sha256(content).hexdigest()
            (destination / source.name).write_bytes(content)
    selected = config / "methods/selected.yaml"
    selected.parent.mkdir()
    selected.write_bytes(method_bytes)
    if candidate is not None:
        fingerprints[candidate] = hashlib.sha256(method_bytes).hexdigest()
    return selected


def _environment():
    return {
        "python": platform.python_version(), "pyogrio": pyogrio.__version__,
        "gdal_vector": pyogrio.__gdal_version_string__, "rasterio": rasterio.__version__,
        "gdal_raster": rasterio.__gdal_version__, "proj": pyproj.proj_version_str,
        "shapely": shapely.__version__,
    }


def _qgis_catalog(qgis, root, deadline, actual):
    _before_deadline(deadline, "discovery")
    directory = root / "discovery"
    directory.mkdir()
    outcome = capture_process(
        [str(qgis), "--json", "list"], cwd=directory, timeout=min(20, _remaining(deadline)),
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    (directory / "process.log").write_text(
        f"stdout:\n{outcome.stdout}\nstderr:\n{outcome.stderr}\nexit_code: {outcome.exit_code}\n", encoding="utf-8",
    )
    actual["discovery_workers_stopped"] = managed_workers_stopped(directory)
    if not actual["discovery_workers_stopped"]:
        raise TrialError("WORKERS_STILL_ACTIVE", "discovery", "QGIS discovery did not release its worker lease")
    if outcome.timed_out:
        code = "TRIAL_TIMEOUT" if _remaining(deadline) <= 0 else "QGIS_DISCOVERY_TIMEOUT"
        raise TrialError(code, "discovery", "QGIS catalog discovery exceeded its time limit")
    if outcome.exit_code != 0:
        raise TrialError("QGIS_DISCOVERY_FAILED", "qgis_process", f"QGIS discovery exited {outcome.exit_code}")
    try:
        return _parse_qgis_catalog(outcome.stdout)
    except ValueError as error:
        raise TrialError("QGIS_DISCOVERY_FAILED", "qgis_process", str(error)) from error


def _runtime(root, method, qgis, deadline, report):
    config = root / "config"
    contracts = [ToolContract.model_validate(read_yaml(path)) for path in _yaml_files(config / "tools")]
    refs = {step.tool for step in method.steps if step.tool is not None}
    if method.step_variants:
        refs.update(variant.tool for branch in method.step_variants.choices.values() for variant in branch.values())
    selected = [contract for contract in contracts if contract.ref in refs]
    report["tools"] = [contract.ref.model_dump(mode="json") for contract in selected]
    workers = _worker_paths(selected)
    registry = ToolRegistry(qgis_process=qgis, python_implementations=workers)
    if any(contract.runner == "qgis" for contract in selected):
        if qgis is None:
            raise TrialError("QGIS_UNAVAILABLE", "qgis_process", "Configure a trusted local QGIS Processing executable")
        # Keep V1 registration and catalog interpretation, with a group-owned discovery subprocess.
        registry._qgis_catalog = _qgis_catalog(qgis, root, deadline, report["actual"])
        if registry.qgis_version:
            report["environment"]["observed"]["qgis"] = registry.qgis_version.split("-", 1)[0]
            report["environment"]["observed"]["qgis_build"] = registry.qgis_version
    for contract in selected:
        registry.register(contract)
    catalog = load_method_catalog(config / "methods")
    if catalog.issues:
        raise TrialError("INVALID_METHOD_CONFIG", "method", catalog.issues[0].message)
    database, workspace = root / "project.sqlite", root / "workspace"
    initialize_database(database)
    app = create_app(
        database, workspace_root=workspace, catalog=catalog, registry=registry,
        registered_rules=RULES, sensor_registry=load_sensor_registry(config / "sensors"),
    )
    bindings = _qgis_bindings(config / "runner_bindings")

    def execute(run):
        execute_claimed_run(
            database, run, workspace_root=workspace, registry=registry, qgis_bindings=bindings,
            python_implementations=workers, qgis_process=qgis, check_rules=RUN_RULES,
            artifact_rules=DEM_ARTIFACT_RULES, input_preflight=_preflight,
            timeout=max(0.001, min(60, _remaining(deadline))),
        )

    return app, execute


def _response(response, expected, phase):
    if response.status_code != expected:
        details = response.json().get("detail", {})
        detail = details if isinstance(details, dict) else {}
        raise TrialError(detail.get("code", "SERVICE_REJECTED"), phase,
                         detail.get("message", f"V1 returned HTTP {response.status_code}"))
    return response.json()


def _draft(client, root, package, case, report, deadline):
    report["actual"]["phase"] = "inputs"
    directory = root / "inputs"
    directory.mkdir()
    inputs = {}
    for role, item in package.case_inputs(case).items():
        _before_deadline(deadline, "inputs")
        source = package.resolve_input(role, case)
        target = directory / f"{role}{source.suffix.lower()}"
        shutil.copyfile(source, target)
        if _digest(target) != item.sha256:
            raise TrialError("FILE_INTEGRITY_MISMATCH", f"inputs.{role}", "The staged copy differs from the package")
        target.chmod(0o400)
        asset = _response(client.post("/api/assets", json={"path": str(target), "layer": item.layer}), 200, "inputs")
        inputs[role] = {key: asset[key] for key in ("asset_id", "asset_version")}
        report["inputs"][role] = {
            "path": target.relative_to(root).as_posix(), "sha256": item.sha256, "layer": item.layer, "asset": inputs[role],
        }
    report["actual"]["phase"] = "draft"
    _before_deadline(deadline, "draft")
    task = _response(client.post("/api/tasks", json={"goal": {"summary": case}}), 201, "draft")["task_id"]
    return _response(client.post(f"/api/tasks/{task}/plans", json={
        "method": package.method.model_dump(mode="json"), "inputs": inputs,
        "critical_options": report["critical_options"],
    }), 200, "draft")


def _execute_run(root, execute, run_id, run_dir, deadline, actual):
    database = root / "project.sqlite"
    faults = []

    def dispatch():
        try:
            run_once(database, execute)
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
            faults.append(str(error))

    worker = Thread(target=dispatch, name="geopilot-trial-dispatch")
    started = _remaining(deadline) > 0
    if started:
        worker.start()
    try:
        if started:
            worker.join(_remaining(deadline))
    finally:
        if not started or worker.is_alive():
            try:
                _, accepted = request_cancellation(database, run_id)
                actual["cancellation_accepted"] = accepted
            except (OSError, sqlite3.Error, ValueError) as error:
                actual["cancellation_error"] = str(error)
            finally:
                if started:
                    worker.join()
        actual["workers_stopped"] = managed_workers_stopped(run_dir)
        actual["run"] = get_run_status(database, run_id).model_dump(mode="json")
    if not actual["workers_stopped"]:
        raise TrialError("WORKERS_STILL_ACTIVE", "execute", "The run worker lease is still active")
    _before_deadline(deadline, "execute")
    if faults:
        raise TrialError("TRIAL_DISPATCH_FAILED", "execute", faults[0])


def _trial_case(root, app, execute, package, case, deadline, report):
    from fastapi.testclient import TestClient

    actual = report["actual"]
    with TestClient(app) as client:
        draft = _draft(client, root, package, case, report, deadline)
        actual["plan"] = {"id": draft["id"], "version": draft["version"], "task_id": draft["task_id"]}
        actual["issues"] = draft["issues"]
        _before_deadline(deadline, "draft")
        if draft["issues"]:
            return
        endpoint = f"/api/plans/{draft['id']}"
        version = {"plan_version": draft["version"]}
        actual["phase"] = "confirm"
        confirmation = client.post(endpoint + "/confirm", json=version)
        _before_deadline(deadline, "confirm")
        if confirmation.status_code == 422:
            detail = confirmation.json().get("detail")
            if isinstance(detail, dict) and detail.get("code") == "PLAN_CONFIRMATION_FAILED":
                actual["issues"] = detail["issues"]
                return
        _response(confirmation, 200, "confirm")
        if report["expected_failure"] is not None:
            return
        actual["phase"] = "execute"
        run_id = _response(client.post(endpoint + "/runs", json=version), 202, "execute")["run_id"]
        actual["run_count"] = 1
        run_dir = root / "workspace" / "tasks" / draft["task_id"] / "runs" / run_id
        actual["run_dir"] = run_dir.relative_to(root).as_posix()
        _execute_run(root, execute, run_id, run_dir, deadline, actual)
        state = actual["run"]
        if (state["execution_status"], state["check_status"]) != ("SUCCEEDED", "PASSED"):
            if state["execution_status"] == "SUCCEEDED":
                actual["phase"] = "checks"
            failure = state["error"] or {}
            raise TrialError(failure.get("code", "TRIAL_RUN_FAILED"), actual["phase"],
                             failure.get("message", f"{state['execution_status']} / {state['check_status']}"))
        actual["phase"] = "reference"
        artifacts = _response(client.get(f"/api/runs/{run_id}/artifacts"), 200, "reference")["artifacts"]
        actual["artifacts"] = {item["role"]: {"path": item["uri"], "layer": item["layer"]} for item in artifacts}
        _before_deadline(deadline, "reference")
        report["reference"] = compare_outputs(package, {
            item["role"]: OutputFile(item["uri"], item["layer"]) for item in artifacts
        }, workspace=run_dir)
        _before_deadline(deadline, "reference")


def _assess(report):
    actual = report["actual"]
    if expected := report["expected_failure"]:
        if (actual["phase"] != expected["phase"] or actual["run_count"]
                or not any(issue["code"] == expected["code"] for issue in actual["issues"])):
            raise TrialError("EXPECTED_FAILURE_MISMATCH", actual["phase"], "The expected phase and error did not match")
        return "expected_failure_matched"
    if actual["issues"]:
        first = actual["issues"][0]
        raise TrialError(first["code"], first["field"], first["message"])
    if report["reference"] is None or report["reference"]["status"] != "PASS":
        raise TrialError("REFERENCE_MISMATCH", "reference", "The independent reference comparison failed")
    return "positive_match"


def resolve_qgis(qgis_process):
    executable = qgis_process or os.environ.get("GEOPILOT_QGIS_PROCESS") or "qgis_process"
    qgis = shutil.which(os.path.expanduser(executable))
    return Path(qgis).absolute() if qgis is not None else None


def run_trial(package_path, work_dir, *, case, candidate, qgis_process, timeout, config_root=CONFIG):
    if not math.isfinite(timeout) or timeout <= 0:
        raise TrialError("INVALID_TIMEOUT", "timeout", "Timeout must be positive and finite", exit_code=2)
    try:
        config = config_root.resolve(strict=True)
        if not config.is_dir():
            raise NotADirectoryError(config)
    except OSError as error:
        raise TrialError("INPUT_READ_ERROR", "config", str(error), exit_code=2) from error
    try:
        package = load_reference_package(package_path)
        failure = package._failure(case)
    except ReferencePackageError as error:
        raise TrialError(error.code, error.field, str(error)) from error
    case = case or package.case_id
    fingerprints = {}
    try:
        method, method_bytes, candidate = _selected_method(package, candidate, fingerprints, config)
    except OSError as error:
        raise TrialError("INPUT_READ_ERROR", "method", str(error), exit_code=2) from error
    if package.inputs.keys() != method.inputs.keys():
        raise TrialError("METHOD_INPUT_MISMATCH", "inputs", "Package input roles must match the method")
    root = _reserve_directory(work_dir, package, config)
    started = time.monotonic()
    deadline = started + timeout
    qgis = resolve_qgis(qgis_process)
    manifest = package.root / package_path.name
    replay = [
        sys.executable, str(Path(__file__).with_name("method_cli.py")), "trial", "--package", str(manifest),
        "--work-dir", "<new-trial-directory>", "--case", case, "--timeout", str(timeout),
    ]
    if candidate is not None:
        replay.extend(("--method", str(candidate)))
    if qgis is not None:
        replay.extend(("--qgis-process", str(qgis)))
    if config != CONFIG.resolve():
        replay.extend(("--config", str(config)))
    report = {
        "schema_version": 1, "status": "FAIL", "verdict": None, "case_id": case,
        "method": package.method.model_dump(mode="json"), "tools": [],
        "expected_failure": failure.expected.model_dump(mode="json") if failure else None,
        "fingerprints": {"manifest": package._files[manifest], "method": hashlib.sha256(method_bytes).hexdigest(),
                         "config": {}},
        "inputs": {}, "critical_options": package.case_options(case),
        "environment": {"declared": package.environment, "observed": _environment()},
        "actual": {"phase": "lint", "issues": [], "plan": None, "run": None, "run_dir": None, "artifacts": {},
                   "run_count": 0, "cancellation_accepted": None, "cancellation_error": None, "workers_stopped": None,
                   "discovery_workers_stopped": None},
        "reference": None, "source_integrity": None, "replay_argv": replay, "error": None,
    }
    previous_profile = os.environ.get("QGIS_CUSTOM_CONFIG_PATH")
    os.environ["QGIS_CUSTOM_CONFIG_PATH"] = str(root / "qgis-profile")
    fault = None
    try:
        selected = _stage_config(root, method_bytes, candidate, fingerprints, config)
        report["fingerprints"]["config"] = {
            path.relative_to(config).as_posix(): digest for path, digest in fingerprints.items()
            if path.is_relative_to(config)
        }
        lint_methods(root / "config", selected)
        _before_deadline(deadline, "lint")
        report["actual"]["phase"] = "discovery"
        app, execute = _runtime(root, method, qgis, deadline, report)
        _before_deadline(deadline, "discovery")
        _trial_case(root, app, execute, package, case, deadline, report)
        with open_database(root / "project.sqlite", read_only=True) as connection:
            report["actual"]["run_count"] = connection.execute("SELECT count(*) FROM run").fetchone()[0]
        report["verdict"] = _assess(report)
    except TrialError as error:
        fault = error
    except ReferencePackageError as error:
        fault = TrialError(error.code, error.field, str(error))
    except LintError as error:
        report["actual"]["issues"] = error.errors
        first = error.errors[0]
        fault = TrialError(first["code"], first["field"], first["message"], exit_code=error.exit_code)
    except (OSError, sqlite3.Error, ValidationError, ValueError, RuntimeError) as error:
        fault = TrialError("TRIAL_FAILED", report["actual"]["phase"], str(error))
    finally:
        if previous_profile is None:
            os.environ.pop("QGIS_CUSTOM_CONFIG_PATH", None)
        else:
            os.environ["QGIS_CUSTOM_CONFIG_PATH"] = previous_profile
    try:
        _assert_package_intact(package)
        if any(_digest(path) != digest for path, digest in fingerprints.items()):
            raise TrialError("FILE_INTEGRITY_MISMATCH", "config", "Source configuration changed during the trial")
        report["source_integrity"] = {"status": "PASS", "error": None}
    except (OSError, ReferencePackageError, TrialError) as error:
        integrity_error = TrialError("FILE_INTEGRITY_MISMATCH", getattr(error, "field", "inputs"), str(error))
        report["source_integrity"] = {"status": "FAIL", "error": _issue(
            integrity_error.code, integrity_error.field, str(integrity_error),
        )}
        fault = fault or integrity_error
    report["timing"] = {"timeout_seconds": timeout, "elapsed_seconds": round(time.monotonic() - started, 6),
                        "deadline_expired": _remaining(deadline) <= 0}
    if fault is None and report["timing"]["deadline_expired"]:
        fault = TrialError("TRIAL_TIMEOUT", report["actual"]["phase"], "The trial deadline expired")
    report["environment"]["matches_declared"] = all(
        report["environment"]["observed"].get(key) == value for key, value in package.environment.items()
    )
    if fault is None:
        report["status"] = "PASS"
    else:
        report["verdict"] = None
        report["error"] = _issue(fault.code, fault.field, str(fault))
    path = root / "report.json"
    _write_json(path, report)
    if fault is not None:
        raise TrialError(fault.code, fault.field, f"{fault}. Report: {path}", exit_code=fault.exit_code)
    return {"scope": "isolated_trial", "report_path": str(path), "report": report}
