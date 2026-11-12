"""Read and hash the local runtime configuration and Python PROJ data."""

from contextlib import ExitStack
from hashlib import file_digest
import os
from pathlib import Path
from stat import S_ISDIR, S_ISLNK, S_ISREG

import pyproj
import yaml

from geopilot.api.server import _qgis_bindings, _yaml_files
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.qgis_runner import _validate_binding
from geopilot.methods.loader import _UniqueKeySafeLoader, load_method_catalog
from geopilot.methods.release_ledger import LEDGER_NAME, fingerprint
from geopilot.persistence.workspace import _open_directory, _open_path
from geopilot.remote_sensing.config_registry import load_sensor_registry


def _path(path):
    path = Path(path).absolute()
    if path.anchor != "/" or ".." in path.parts:
        raise ValueError(f"Expected a local runtime path: {path}")
    return path


def _state(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _digest(directory, name):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(name, flags, dir_fd=directory), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not S_ISREG(before.st_mode):
            raise OSError(f"Expected a regular runtime file: {name}")
        digest = file_digest(stream, "sha256").hexdigest()
        if _state(os.fstat(stream.fileno())) != _state(before) or _state(
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        ) != _state(before):
            raise OSError(f"Runtime file changed while hashing: {name}")
    return digest


def _files(directory, *, accepted=None, recursive=False, optional=False):
    path = _path(directory)

    def visit(current, fd):
        before = _state(os.fstat(fd))
        with os.scandir(fd) as entries:
            names = sorted(entry.name for entry in entries)
        files = {}
        for name in names:
            if accepted is not None and not accepted(name):
                continue
            mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
            if S_ISLNK(mode):
                raise OSError(f"Unverified runtime symlink: {current / name}")
            if S_ISDIR(mode) and (recursive or accepted is not None and name != LEDGER_NAME):
                if recursive:
                    child = current / name
                    with _open_directory(child, fd) as subdirectory:
                        files.update(visit(child, subdirectory))
                continue
            files[(current / name).relative_to(path).as_posix()] = _digest(fd, name)
        if _state(os.fstat(fd)) != before:
            raise OSError(f"Runtime directory changed while hashing: {current}")
        return files

    with ExitStack() as directories:
        try:
            fd = directories.enter_context(_open_path(Path("/"), path.parts[1:]))
        except FileNotFoundError:
            if optional:
                return None
            raise
        return visit(path, fd)


def _configuration_files(config):
    result = {}
    for family in ("methods", "tools", "runner_bindings", "sensors"):
        def accepted(name):
            return Path(name).suffix in {".yaml", ".yml"} or (family == "methods" and name == LEDGER_NAME)

        result[family] = _files(config / family, accepted=accepted, optional=family == "sensors")
    return result


def _validate_configuration(config, files):
    catalog = load_method_catalog(config / "methods")
    if catalog.issues:
        issue = catalog.issues[0]
        raise ValueError(f"{issue.path}: {issue.field}: {issue.message}")
    contracts = [
        ToolContract.model_validate(yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader))
        for path in _yaml_files(config / "tools")
    ]
    if len({(contract.id, contract.version) for contract in contracts}) != len(contracts):
        raise ValueError("Duplicate configured tool version")
    bindings = _qgis_bindings(config / "runner_bindings")
    for contract in contracts:
        if contract.runner == "qgis":
            _validate_binding(contract, bindings)
    if files["sensors"] is not None:
        load_sensor_registry(config / "sensors")


def _proj_files():
    paths = pyproj.datadir.get_data_dir().split(os.pathsep)
    roots = [_files(path, recursive=True) for path in paths]
    if not any("proj.db" in files for files in roots):
        raise ValueError("Selected Python PROJ search roots lack proj.db")
    user = pyproj.datadir.get_user_data_dir(create=False)
    user_files = _files(user, recursive=True, optional=True) if user else None

    auxiliary = []
    if declared := os.environ.get("PROJ_AUX_DB"):
        for filename in declared.split(os.pathsep):
            path = _path(filename)
            with _open_path(Path("/"), path.parent.parts[1:]) as parent:
                auxiliary.append({"name": path.name, "sha256": _digest(parent, path.name)})
    epsg = pyproj.database.get_database_metadata("EPSG.VERSION")
    if not epsg:
        raise ValueError("Could not read the selected Python PROJ database metadata")
    return {
        "version": pyproj.proj_version_str, "epsg_version": epsg, "network_enabled": pyproj.network.is_network_enabled(),
        "search_roots": roots, "user_files": user_files, "auxiliary_databases": auxiliary,
    }


def runtime_config_snapshot(config):
    config = _path(config)
    files = _configuration_files(config)
    _validate_configuration(config, files)
    if files != _configuration_files(config):
        raise ValueError("Runtime configuration changed while loading")
    document = {"schema_version": 1, "config": files, "python_proj": _proj_files()}
    return {"sha256": fingerprint(document), **document}
