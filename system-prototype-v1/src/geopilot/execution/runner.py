"""Dispatch a versioned step after checking its actual measurement inputs."""

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict
from os import PathLike
from pathlib import Path

from pyproj import CRS
from pyproj.exceptions import CRSError

from geopilot.data.errors import SourceReadError
from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.data.vector_reader import read_layer_metadata
from geopilot.domain.data_asset import RasterMetadata, VectorMetadata
from geopilot.domain.step_io import StepRequest, StepResult
from geopilot.execution.python_runner import invoke_python
from geopilot.execution.qgis_runner import invoke_qgis
from geopilot.execution.step_support import (
    AdapterError,
    PreparedStep,
    execute_adapter,
    matches_vector_type,
    resolve_step_path,
)
from geopilot.gis.measurement.area import permits_empty_area_input
from geopilot.gis.measurement.coverage import MeasurementDecision, MeasurementPolicyError
from geopilot.gis.measurement.distance import validate_tool_distance
from geopilot.gis.measurement.projection import validate_step_area
from geopilot.gis.validation.crs_validator import CRSValidationError
from geopilot.tools.registry import ToolRegistry


def _read_measurement_input(step: PreparedStep, role: str, kind: str) -> VectorMetadata | RasterMetadata:
    ref = step.request["inputs"][role]
    field = f"inputs.{role}"
    path = resolve_step_path(step.workdir, ref["path"], field=f"{field}.path")
    layer = ref.get("layer")
    try:
        if kind.startswith("raster"):
            if layer is not None:
                raise SourceReadError("A raster file reference cannot select a vector layer", field="source.layer")
            return read_raster_metadata(path)
        if path.suffix.lower() == ".gpkg":
            _, metadata = read_geopackage_metadata(path, layer)
        else:
            metadata = read_layer_metadata(path, layer)
    except SourceReadError as error:
        raise AdapterError(
            error.message, code=error.code, phase="preflight",
            field=f"{field}.{error.field.rsplit('.', 1)[-1]}", suggestion=error.suggestion,
        ) from error
    if not isinstance(metadata, VectorMetadata) or not matches_vector_type(metadata, kind):
        raise AdapterError(
            f"The measurement input requires a {kind} layer",
            code="METHOD_PRECONDITION_FAILED", phase="preflight", field=field,
        )
    return metadata


def _verify_area_parameters(parameters: Mapping, decision: MeasurementDecision) -> None:
    if "area_crs" in parameters:
        requested_crs = parameters["area_crs"]
        try:
            matching_crs = CRS.from_user_input(requested_crs).equals(
                CRS.from_user_input(decision.crs), ignore_axis_order=True
            )
        except (CRSError, TypeError, ValueError):
            matching_crs = False
        if not matching_crs:
            raise AdapterError(
                f"The requested area CRS must match the input CRS {decision.crs}",
                code="INVALID_PARAMETER", phase="preflight", field="parameters.area_crs", observed=requested_crs,
            )
    if "area_unit" in parameters and parameters["area_unit"] != decision.unit:
        raise AdapterError(
            f"Area measurement requires {decision.unit}",
            code="INVALID_PARAMETER", phase="preflight", field="parameters.area_unit", observed=parameters["area_unit"],
        )


def _preflight_measurement(step: PreparedStep) -> None:
    policy = step.contract.measurement_policy
    if policy.distance == policy.area == "not_applicable":
        return

    spatial_ports = [
        (role, kind) for role, kind in step.contract.inputs.items() if kind.startswith(("vector", "raster"))
    ]
    if not spatial_ports:
        raise AdapterError(
            "The measurement contract requires a spatial input",
            code="MEASUREMENT_POLICY_REJECTED", phase="preflight", field="inputs",
        )
    decisions = []
    try:
        for role, kind in spatial_ports:
            metadata = _read_measurement_input(step, role, kind)
            field = f"inputs.{role}.crs"
            if policy.distance != "not_applicable":
                distance = validate_tool_distance(metadata, step.contract, field=field)
                decisions.append({"role": role, **asdict(distance)})
            if policy.area == "equal_area":
                area = validate_step_area(
                    metadata, step.contract, field=field, target_crs=step.request["parameters"].get("area_crs"),
                )
                _verify_area_parameters(step.request["parameters"], area)
                entry = {"role": role, **asdict(area)}
                if permits_empty_area_input(metadata, step.contract, field=field):
                    entry["extent"] = "empty"
                decisions.append(entry)
    except (CRSValidationError, MeasurementPolicyError) as error:
        raise AdapterError(
            error.message, code=error.code, phase=error.phase, field=error.field,
            observed=error.observed, suggestion=getattr(error, "suggestion", None),
        ) from error

    with step.log_path.open("a", encoding="utf-8") as log:
        for decision in decisions:
            log.write(f"measurement: {json.dumps(decision, ensure_ascii=False)}\n")


def run_step(
    request: StepRequest,
    *,
    registry: ToolRegistry,
    workdir: str | Path,
    qgis_bindings: Mapping | None = None,
    python_implementations: Mapping[str, str | Path] | None = None,
    qgis_process: str | PathLike[str] | None = None,
    input_preflight: Callable[[PreparedStep], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    timeout: float = 60,
) -> StepResult:
    """Run the exact registered version and return the common typed result."""
    def preflight(step: PreparedStep) -> None:
        if input_preflight is not None:
            input_preflight(step)
        _preflight_measurement(step)

    def invoke(step: PreparedStep) -> dict[str, str]:
        if step.contract.runner == "qgis":
            return invoke_qgis(step, bindings=qgis_bindings or {}, qgis_process=qgis_process, timeout=timeout)
        return invoke_python(step, implementations=python_implementations or {}, timeout=timeout)

    result = execute_adapter(
        request.model_dump(mode="json", exclude_unset=True),
        registry=registry, workdir=workdir, runner=None, invoke=invoke,
        preflight=preflight, cancel_requested=cancel_requested,
    )
    return StepResult.model_validate(result)
