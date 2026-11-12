"""Check the configured local environment without opening a project database."""

import json
import os
import platform
import re
import shutil
import socket
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory

from geopilot.execution.process_manager import capture_process


ROOT = Path(__file__).resolve().parents[3]
PYTHON_BASELINE = "3.13.13"
QGIS_BASELINE = "3.44.15"
NODE_BASELINE = "22.23.1"
NPM_BASELINE = "10.9.8"
QGIS_LIBRARIES = {"gdal_version": "3.8.4", "proj_version": "9.4.0", "geos_version": "3.12.1"}
PYTHON_GIS_LIBRARIES = {"pyogrio_gdal": "3.11.4", "rasterio_gdal": "3.9.3", "pyproj_proj": "9.5.1"}
PIN = re.compile(r"([\w.-]+)==([^\s;]+)")
NPM_PACKAGE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")
LIBRARY_VERSION = re.compile(r"(?:Rel\.\s+)?(\d+\.\d+\.\d+)(?:$|[,\s-])")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str
    details: dict = field(default_factory=dict)


def check_version(name, actual, baseline, details=None):
    details = {**(details or {}), "actual": actual, "verified_version": baseline}
    if actual != baseline:
        return Check(name, "WARN", f"Version differs from the verified {baseline}; rerun the installation matrix.", details)
    return Check(name, "PASS", "Matches the verified version.", details)


def check_python_dependencies(requirements):
    try:
        entries = [
            line.strip() for line in requirements.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (OSError, UnicodeError) as error:
        return Check("python_dependencies", "FAIL", "Cannot read the project dependency list.", {"error": str(error)})

    if not entries or any(not PIN.fullmatch(entry) for entry in entries):
        return Check("python_dependencies", "FAIL", "Expected exact package pins in requirements.txt.")
    dependencies = {}
    for entry in entries:
        package, expected = entry.split("==", 1)
        canonical = re.sub(r"[-_.]+", "-", package.lower())
        if canonical in dependencies:
            return Check("python_dependencies", "FAIL", "Duplicate package pin in requirements.txt.",
                         {"package": canonical})
        try:
            actual = version(package)
        except PackageNotFoundError:
            actual = None
        dependencies[canonical] = {"expected": expected, "actual": actual}
    mismatches = [name for name, row in dependencies.items() if row["actual"] != row["expected"]]
    details = {"packages": dependencies, "mismatches": mismatches}
    if mismatches:
        return Check("python_dependencies", "FAIL", "Install requirements.txt in the selected Python environment.", details)
    return Check("python_dependencies", "PASS", "All pinned runtime packages are installed.", details)


def check_gis_libraries():
    try:
        import pyogrio
        import pyproj
        import rasterio

        with rasterio.Env() as environment:
            geotiff = "GTiff" in environment.drivers()
        geopackage = pyogrio.list_drivers().get("GPKG", "")
        geographic = pyproj.CRS.from_epsg(4326)
        projected = pyproj.CRS.from_epsg(32650)
    except (ImportError, OSError, ValueError, RuntimeError) as error:
        return Check("gis_libraries", "FAIL", "Check GDAL/PROJ library imports and the local PROJ database.", {"error": str(error)})
    details = {
        "pyogrio_gdal": pyogrio.__gdal_version_string__,
        "rasterio_gdal": rasterio.__gdal_version__,
        "pyproj_proj": pyproj.proj_version_str,
        "rasterio_proj": rasterio.__proj_version__,
        "proj_data_dir": pyproj.datadir.get_data_dir(),
        "drivers": {"GPKG": geopackage, "GTiff": geotiff},
    }
    if not (geotiff and "r" in geopackage and "w" in geopackage and geographic.is_geographic and projected.is_projected):
        return Check("gis_libraries", "FAIL", "Required GeoPackage, GeoTIFF or EPSG support is unavailable.", details)
    if any(details[key] != expected for key, expected in PYTHON_GIS_LIBRARIES.items()):
        return Check("gis_libraries", "WARN", "GDAL/PROJ probes passed with a different library combination.",
                     {**details, "verified_libraries": PYTHON_GIS_LIBRARIES})
    return Check("gis_libraries", "PASS", "Required GDAL drivers and the PROJ CRS database are accessible.", details)


def probe(executable, args, temporary, extra_env):
    env = {
        **os.environ,
        "XDG_RUNTIME_DIR": str(temporary),
        "QGIS_CUSTOM_CONFIG_PATH": str(temporary / "qgis"),
        "QT_QPA_PLATFORM": "offscreen",
        "PROJ_NETWORK": "OFF",
        **extra_env,
    }
    return capture_process([executable, *args], cwd=temporary, timeout=15, env=env)


def check_qgis(requested, temporary):
    candidate = requested or os.environ.get("GEOPILOT_QGIS_PROCESS") or "qgis_process"
    executable = shutil.which(os.path.expanduser(candidate))
    if not executable:
        return Check("qgis", "FAIL", "Install QGIS Processing or set GEOPILOT_QGIS_PROCESS to an executable.",
                     {"requested": candidate})
    executable = os.path.abspath(executable)
    details = {"executable": executable}
    try:
        result = probe(executable, ["--json", "help", "native:buffer"], temporary, {})
    except OSError as error:
        return Check("qgis", "FAIL", "The QGIS executable could not be started.", {**details, "error": str(error)})
    if result.timed_out or result.exit_code:
        return Check("qgis", "FAIL", "QGIS help probe failed; check the selected CLI and its libraries.",
                     {**details, "exit_code": result.exit_code, "timed_out": result.timed_out})
    try:
        document = json.loads(result.stdout)
        details["versions"] = {
            key: document[key] for key in ("qgis_version", "gdal_version", "proj_version", "geos_version", "python_version")
        }
        valid = (
            all(isinstance(value, str) and value for value in details["versions"].values())
            and document["algorithm_details"]["id"] == "native:buffer"
            and isinstance(document["parameters"], dict)
            and "DISTANCE" in document["parameters"]
            and isinstance(document["outputs"], dict)
            and "OUTPUT" in document["outputs"]
        )
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        return Check("qgis", "FAIL", "QGIS did not return the expected Processing JSON. Check native:buffer help.", details)
    comparable = {}
    for key in QGIS_LIBRARIES:
        match = LIBRARY_VERSION.match(details["versions"][key])
        comparable[key] = match.group(1) if match else details["versions"][key]
    if comparable != QGIS_LIBRARIES:
        return Check("qgis", "WARN", "QGIS Processing responded with a different GDAL/PROJ/GEOS combination.",
                     {**details, "verified_qgis": QGIS_BASELINE, "verified_libraries": QGIS_LIBRARIES})
    return check_version("qgis", details["versions"]["qgis_version"].split("-", 1)[0], QGIS_BASELINE, details)


def check_frontend_runtime(name, baseline, temporary):
    executable = shutil.which(name)
    if executable is None:
        return Check(name, "FAIL", f"Install {name} and put it on PATH.")
    executable = os.path.abspath(executable)
    try:
        result = probe(executable, ["--version"], temporary, {
            "NPM_CONFIG_CACHE": str(temporary / "npm"),
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "NPM_CONFIG_OFFLINE": "true",
        })
    except OSError as error:
        return Check(name, "FAIL", f"Unable to start {name}.", {"error": str(error)})
    actual = result.stdout.strip().removeprefix("v")
    if result.timed_out or result.exit_code or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][^\s]+)?", actual):
        return Check(name, "FAIL", f"Cannot read {name} version; check the executable on PATH.",
                     {"executable": executable, "exit_code": result.exit_code, "timed_out": result.timed_out})
    return check_version(name, actual, baseline, {"executable": executable})


def check_frontend_dependencies(frontend):
    try:
        manifest = json.loads((frontend / "package.json").read_bytes())
        lock = json.loads((frontend / "package-lock.json").read_bytes())["packages"]
        packages = {**manifest["dependencies"], **manifest["devDependencies"]}
        if not packages or any(not NPM_PACKAGE.fullmatch(name) for name in packages):
            raise ValueError("Expected named direct packages")
        locked = {name: lock[f"node_modules/{name}"]["version"] for name in packages}
        if any(expected != locked[name] for name, expected in packages.items()):
            raise ValueError("Direct package pins differ from package-lock.json")
    except (OSError, KeyError, TypeError, ValueError) as error:
        return Check("frontend_dependencies", "FAIL", "Check package.json and restore the project lock file.",
                     {"error": str(error)})

    mismatches = {}
    for name, expected in locked.items():
        try:
            actual = json.loads((frontend / "node_modules" / name / "package.json").read_bytes())["version"]
        except (OSError, KeyError, TypeError, ValueError):
            actual = None
        if actual != expected:
            mismatches[name] = {"expected": expected, "actual": actual}
    if mismatches:
        return Check("frontend_dependencies", "FAIL", "Run npm ci --include=dev from frontend/.", {"mismatches": mismatches})
    return Check("frontend_dependencies", "PASS", "Installed direct frontend packages match the lock file.",
                 {"checked": len(locked)})


def check_config(path):
    required = ("methods", "runner_bindings", "sensors", "tools")
    unavailable = [
        name for name in required
        if not (path / name).is_dir() or not os.access(path / name, os.R_OK | os.X_OK)
    ]
    details = {"path": str(path), "unavailable": unavailable}
    if unavailable:
        return Check("config", "FAIL", "Select the readable GeoPilot configuration directory with --config.", details)
    return Check("config", "PASS", "Configuration directories are readable.", details)


def check_storage(name, path, directory):
    try:
        target = path.resolve()
        parent = target if directory else target.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        details = {"path": str(target), "existing_parent": str(parent), "exists": target.exists()}
        usable_parent = parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
        valid_file = not target.exists() or target.is_file() and os.access(target, os.R_OK | os.W_OK)
    except (OSError, RuntimeError) as error:
        return Check(name, "FAIL", "Check the storage path and resolve any invalid links.", {"error": str(error)})
    if not usable_parent or (not directory and not valid_file):
        return Check(name, "FAIL", "Choose a writable path of the correct type and check its parent permissions.", details)
    return Check(name, "PASS", "Storage path or its nearest parent is writable.", details)


def check_port(name, port):
    details = {"host": "127.0.0.1", "port": port}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
            probe_socket.bind(("127.0.0.1", port))
    except OSError as error:
        return Check(name, "FAIL", "Loopback bind failed. Free the port or select another one.", {**details, "errno": error.errno})
    return Check(name, "PASS", "Loopback bind succeeded at the time of the probe.", details)


def diagnose(root, config, database, workspace, qgis, port, frontend_port):
    database_check = check_storage("database", database, directory=False)
    workspace_check = check_storage("workspace", workspace, directory=True)
    if "path" in database_check.details and database_check.details["path"] == workspace_check.details.get("path"):
        database_check = Check("database", "FAIL", "The SQLite file and workspace must use distinct paths.",
                               database_check.details)
    frontend_port_check = (
        Check("frontend_port", "FAIL", "Choose a different port for the frontend and backend.",
              {"host": "127.0.0.1", "port": frontend_port})
        if port == frontend_port else check_port("frontend_port", frontend_port)
    )
    checks = [
        check_version("python", platform.python_version(), PYTHON_BASELINE),
        check_python_dependencies(root / "requirements.txt"),
        check_gis_libraries(),
        check_config(config),
        database_check,
        workspace_check,
        check_frontend_dependencies(root / "frontend"),
        check_port("backend_port", port),
        frontend_port_check,
    ]
    with TemporaryDirectory(prefix="geopilot-doctor-") as scratch:
        temporary = Path(scratch)
        checks.extend([
            check_qgis(qgis, temporary),
            check_frontend_runtime("node", NODE_BASELINE, temporary),
            check_frontend_runtime("npm", NPM_BASELINE, temporary),
        ])
    return checks
