"""Run a registered QGIS algorithm through the qgis_process JSON protocol."""

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.step_support import AdapterError, PreparedStep, execute_adapter, run_process
from geopilot.tools.registry import ToolRegistry


_VERSION_FIELDS = (
    "qgis_version", "qgis_code_revision", "qt_version", "gdal_version",
    "proj_version", "geos_version", "python_version",
)
_DISCOVERY_TIMEOUT = 20


def _invalid_argument(message: str, field: str) -> AdapterError:
    return AdapterError(message, code="INVALID_PARAMETER", field=field, phase="preflight")


def _validate_binding(contract: ToolContract, bindings: Mapping) -> tuple[Mapping, str]:
    key = f"{contract.id}@{contract.version}"
    if not isinstance(bindings, Mapping) or key not in bindings:
        key = contract.implementation
    field = f"bindings.{key}"
    binding = bindings.get(key) if isinstance(bindings, Mapping) else None
    if not isinstance(binding, Mapping) or set(binding) - {"inputs", "parameters", "outputs", "enum_values"}:
        raise _invalid_argument("Expected an explicit local QGIS binding for the registered algorithm", field)

    seen: set[str] = set()
    for group in ("inputs", "parameters", "outputs"):
        aliases = binding.get(group)
        if not isinstance(aliases, Mapping) or set(aliases) != set(getattr(contract, group)):
            raise _invalid_argument("QGIS binding roles must match the tool contract", f"{field}.{group}")
        for alias in aliases.values():
            if not isinstance(alias, str) or not alias or not alias.isprintable() or any(c.isspace() for c in alias):
                raise _invalid_argument("Expected a QGIS parameter name", f"{field}.{group}")
            if alias.casefold() in seen:
                raise _invalid_argument(f"Repeated QGIS parameter name: {alias}", f"{field}.{group}")
            seen.add(alias.casefold())

    enum_specs = {name: spec for name, spec in contract.parameters.items() if spec.type == "enum"}
    enum_values = binding.get("enum_values", {})
    if not isinstance(enum_values, Mapping) or set(enum_values) != set(enum_specs):
        raise _invalid_argument("Every enum parameter needs an explicit QGIS mapping", f"{field}.enum_values")
    for name, spec in enum_specs.items():
        choices = enum_values[name]
        enum_field = f"{field}.enum_values.{name}"
        if not isinstance(choices, Mapping) or set(choices) != set(spec.values or ()):
            raise _invalid_argument("QGIS enum mapping must cover every declared value", enum_field)
        indices = list(choices.values())
        if any(type(index) is not int or index < 0 for index in indices) or len(set(indices)) != len(indices):
            raise _invalid_argument("QGIS enum indices must be unique nonnegative integers", enum_field)
    return binding, field


def _file_argument(ref: Mapping, *, field: str, output: bool = False) -> str:
    relative = Path(ref["path"])
    path = f"./{relative.as_posix()}"
    layer = ref.get("layer")
    for name, value in (("path", path), ("layer", layer)):
        if value is not None and any(char in value for char in "|\x00\r\n"):
            raise _invalid_argument("QGIS local paths and layer names cannot contain URI delimiters", f"{field}.{name}")
    if layer is None:
        return path
    if not output:
        return f"{path}|layername={layer}"
    if relative.suffix.lower() != ".gpkg":
        raise _invalid_argument("An explicit QGIS output layer requires a GeoPackage", f"{field}.layer")

    # QgsDataSourceUri uses backslash escapes inside quoted database/table names.
    database = path.replace("\\", "\\\\").replace("'", "\\'")
    table = layer.replace("\\", "\\\\").replace('"', '\\"')
    return f"ogr:dbname='{database}' table=\"{table}\" (geom)"


def _build_parameters(step: PreparedStep, binding: Mapping) -> dict:
    request = step.request
    parameters = {
        binding["inputs"][role]: _file_argument(ref, field=f"inputs.{role}")
        for role, ref in request["inputs"].items()
    }
    enum_values = binding.get("enum_values", {})
    for name, value in request["parameters"].items():
        parameters[binding["parameters"][name]] = enum_values[name][value] if name in enum_values else value
    parameters.update({
        binding["outputs"][role]: _file_argument(ref, field=f"output_targets.{role}", output=True)
        for role, ref in request["output_targets"].items()
    })
    return parameters


def _verify_algorithm_schema(
    step: PreparedStep, binding: Mapping, parameters: Mapping, stdout: str, *, field: str
) -> None:
    implementation = step.contract.implementation
    try:
        schema = json.loads(stdout)
        details = schema["algorithm_details"]
        definitions = schema["parameters"]
        outputs = schema["outputs"]
    except (ValueError, KeyError, TypeError) as error:
        raise _invalid_argument("QGIS did not return a usable algorithm definition", field) from error
    if (not isinstance(details, dict) or details.get("id") != implementation
            or not isinstance(definitions, dict) or not isinstance(outputs, dict)):
        raise _invalid_argument("QGIS did not return the selected algorithm definition", field)
    if any(
        not isinstance(definition, dict)
        or any(type(definition.get(flag)) is not bool for flag in ("is_destination", "optional"))
        for definition in definitions.values()
    ):
        raise _invalid_argument("QGIS returned invalid parameter definitions", field)

    for group in ("inputs", "parameters", "outputs"):
        destination = group == "outputs"
        for name, alias in binding[group].items():
            definition = definitions.get(alias)
            if (definition is None or definition["is_destination"] != destination
                    or (destination and alias not in outputs)):
                raise _invalid_argument(f"QGIS does not declare {alias} as a {group} binding", f"{field}.{group}.{name}")

    required = {
        name for name, definition in definitions.items()
        if not definition["optional"] and definition.get("default_value") is None
    }
    missing = sorted(required - parameters.keys())
    if missing:
        raise _invalid_argument(f"Required QGIS arguments are not supplied: {', '.join(missing)}", f"{field}.parameters")

    for name, choices in binding.get("enum_values", {}).items():
        alias = binding["parameters"][name]
        options = definitions[alias].get("available_options")
        enum_field = f"{field}.enum_values.{name}"
        if not isinstance(options, dict):
            raise _invalid_argument(f"QGIS does not report enum options for {alias}", enum_field)
        for index in choices.values():
            if str(index) not in options:
                raise _invalid_argument(f"QGIS does not declare option index {index} for {alias}", enum_field)


def _executable(configured: str | os.PathLike[str] | None) -> str:
    requested = configured
    if requested is None:
        requested = os.environ.get("GEOPILOT_QGIS_PROCESS") or "qgis_process"
    try:
        name = os.path.expanduser(os.fspath(requested))
        if not name or "\x00" in name:
            raise ValueError("Expected a nonempty executable path")
        located = shutil.which(name)
        return os.path.abspath(located or name) if located or os.path.dirname(name) else name
    except (OSError, TypeError, ValueError) as error:
        raise AdapterError(str(error), code="PROCESS_START_FAILED", field="tool") from error


def _output_error(message: str, *, field: str = "outputs", missing: bool = False) -> AdapterError:
    return AdapterError(message, code="OUTPUT_MISSING" if missing else "OUTPUT_INVALID", field=field, exit_code=0)


def _verify_report(step: PreparedStep, binding: Mapping, stdout: str) -> dict[str, str]:
    try:
        document = json.loads(stdout)
    except ValueError as error:
        raise _output_error("QGIS did not return a valid JSON result") from error
    if not isinstance(document, dict):
        raise _output_error("QGIS result must be a JSON object")
    outputs = document.get("results")
    if outputs is None:
        raise _output_error("QGIS result manifest is missing", missing=True)
    if not isinstance(outputs, dict) or set(outputs) - set(binding["outputs"].values()):
        raise _output_error("QGIS results must contain only declared output names")

    for role, ref in step.request["output_targets"].items():
        field = f"outputs.{role}"
        uri = outputs.get(binding["outputs"][role])
        if uri is None or uri == "":
            raise _output_error("QGIS did not report the requested output", field=field, missing=True)
        if not isinstance(uri, str):
            raise _output_error("Expected a local file in the QGIS output manifest", field=field)
        path, separator, layer = uri.partition("|layername=")
        if (step.workdir / path != step.workdir / ref["path"]
                or (layer if separator else None) != ref.get("layer")):
            raise _output_error("QGIS reported a different output path or layer", field=field)

    environment = {
        key: document[key] for key in _VERSION_FIELDS if isinstance(document.get(key), str) and document[key]
    }
    if "qgis_version" not in environment:
        raise _output_error("QGIS result did not report the runtime version", field="environment.qgis_version")
    return environment


def invoke_qgis(
    step: PreparedStep, *, bindings: Mapping,
    qgis_process: str | os.PathLike[str] | None = None, timeout: float = 60,
) -> dict[str, str]:
    """Run QGIS with an already prepared step and its configured bindings."""
    binding, field = _validate_binding(step.contract, bindings)
    parameters = _build_parameters(step, binding)
    payload = json.dumps({"inputs": parameters}, ensure_ascii=False, allow_nan=False)
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    executable = _executable(qgis_process)
    try:
        schema = run_process(
            step, [executable, "--json", "help", step.contract.implementation],
            timeout=min(timeout, _DISCOVERY_TIMEOUT), env=env,
        )
    except AdapterError as error:
        raise AdapterError(
            error.message, code=error.code, field=error.field, exit_code=error.exit_code, phase="preflight"
        ) from error
    _verify_algorithm_schema(step, binding, parameters, schema, field=field)
    stdout = run_process(
        step, [executable, "--json", "run", step.contract.implementation, "-"],
        timeout=timeout, stdin=payload, env=env,
    )
    return _verify_report(step, binding, stdout)


def run_qgis(
    request: Mapping, *, registry: ToolRegistry, workdir: Path, bindings: Mapping,
    qgis_process: str | os.PathLike[str] | None = None, timeout: float = 60,
) -> dict:
    """Execute the selected local tool using bindings keyed by its implementation ID."""
    def invoke(step: PreparedStep) -> dict[str, str]:
        return invoke_qgis(step, bindings=bindings, qgis_process=qgis_process, timeout=timeout)

    return execute_adapter(request, registry=registry, workdir=workdir, runner="qgis", invoke=invoke)
