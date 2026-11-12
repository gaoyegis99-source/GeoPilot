"""Inspect the actual local inputs of a prepared step before dispatch."""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pyogrio
import rasterio
import shapely
from affine import Affine
from geopandas import GeoDataFrame
from pydantic import BaseModel, ConfigDict, JsonValue
from pyogrio.errors import DataLayerError, DataSourceError
from pyproj import CRS
from pyproj.exceptions import CRSError
from rasterio.errors import RasterioError
from shapely.errors import GEOSException
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry

from geopilot.checks.raster_nodata import check_band_nodata
from geopilot.data.errors import SourceReadError
from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.data.vector_reader import read_layer_metadata
from geopilot.domain.data_asset import AssetMetadata, RasterMetadata, VectorMetadata
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.step_support import AdapterError, PreparedStep, matches_vector_type, resolve_step_path
from geopilot.gis.measurement.area import permits_empty_area_input
from geopilot.gis.measurement.coverage import MeasurementPolicyError
from geopilot.gis.measurement.distance import permits_empty_geodesic_input, validate_distance, validate_tool_distance
from geopilot.gis.measurement.projection import requires_area_target, validate_step_area
from geopilot.gis.validation.crs_validator import CRSValidationError, inspect_crs


_ID = "source_feature_id"
_DISTANCE_RULE = "projected_crs_suitable_for_extent"
_AREA_RULE = "equal_area_crs_suitable_for_extent"
_COVERAGE_RULE = "data_covers_required_extent"
_RASTER_NODATA_RULE = "raster_nodata"
_MEASUREMENT_RULES = {_DISTANCE_RULE, _AREA_RULE, "horizontal_unit_is_meter", "geodesic_measurement_is_supported"}


class InputCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    rule: str
    phase: Literal["input"] = "input"
    field: str
    severity: Literal["blocking", "warning"]
    passed: bool
    observed: JsonValue
    expected: JsonValue
    message: str


@dataclass(frozen=True)
class CoverageRequirement:
    """A rectangular area to cover in the input's explicitly declared CRS."""

    bounds: tuple[float, float, float, float]
    crs: str

    def __post_init__(self) -> None:
        if len(self.bounds) != 4 or not all(
            type(value) in (int, float) and math.isfinite(value) for value in self.bounds
        ):
            raise ValueError("Coverage requires four finite coordinates")
        left, bottom, right, top = self.bounds
        if left >= right or bottom >= top:
            raise ValueError("Coverage bounds must enclose a positive area")
        try:
            CRS.from_user_input(self.crs)
        except (CRSError, TypeError, ValueError) as error:
            raise ValueError("Coverage requires a known CRS") from error


class InputCheckError(AdapterError):
    """Keep all input evidence while blocking execution on the first hard failure."""

    def __init__(self, results: tuple[InputCheckResult, ...], blocking: tuple[InputCheckResult, ...]) -> None:
        first = blocking[0]
        codes = {"source_is_readable": "SOURCE_UNREADABLE", "crs_is_known": "CRS_UNKNOWN"}
        code = "MEASUREMENT_POLICY_REJECTED" if first.rule in _MEASUREMENT_RULES else codes.get(
            first.rule, "METHOD_PRECONDITION_FAILED",
        )
        super().__init__(first.message, code=code, field=first.field, phase="preflight", observed=first.observed)
        self.results = results
        self.blocking = blocking


def require_input_checks(results: Iterable[InputCheckResult]) -> None:
    """Raise for failed blocking rules and permit warning results."""
    evidence = tuple(results)
    blocking = tuple(result for result in evidence if result.severity == "blocking" and not result.passed)
    if blocking:
        raise InputCheckError(evidence, blocking)


def _result(
    rule: str, field: str, passed: bool, observed: JsonValue, expected: JsonValue, message: str,
) -> InputCheckResult:
    return InputCheckResult(
        rule=rule, field=field, severity="blocking", passed=passed, observed=observed, expected=expected, message=message,
    )


def _bounds(metadata: VectorMetadata | RasterMetadata) -> list[float] | None:
    return list(metadata.bounds) if metadata.bounds is not None else None


def _read_spatial_input(step: PreparedStep, role: str, kind: str) -> tuple[Path, str | None, AssetMetadata]:
    reference = step.request["inputs"][role]
    path = resolve_step_path(step.workdir, reference["path"], field=f"inputs.{role}.path")
    layer = reference.get("layer")
    if kind.startswith("raster"):
        if layer is not None:
            raise SourceReadError("A raster input cannot specify a vector layer", field="source.layer")
        return path, None, read_raster_metadata(path)
    if path.suffix.lower() == ".gpkg":
        layer, metadata = read_geopackage_metadata(path, layer)
    else:
        metadata = read_layer_metadata(path, layer)
    return path, layer, metadata


def _raster_footprint(metadata: RasterMetadata) -> Polygon | None:
    transform = Affine(*metadata.transform)
    width, height = metadata.width, metadata.height
    corners = [transform * corner for corner in ((0, 0), (width, 0), (width, height), (0, height))]
    if not all(math.isfinite(value) for corner in corners for value in corner):
        return None
    footprint = Polygon(corners)
    return footprint if footprint.is_valid and math.isfinite(footprint.area) and footprint.area > 0 else None


def _known_crs(metadata: VectorMetadata | RasterMetadata, field: str, checks: list[InputCheckResult]) -> CRS | None:
    try:
        crs = CRS.from_user_input(metadata.crs) if metadata.crs is not None else None
    except CRSError:
        crs = None
    checks.append(_result(
        "crs_is_known", field, crs is not None, metadata.crs, "Known declared CRS",
        "Input CRS is known" if crs is not None else "Input requires a known CRS",
    ))
    return crs


def _metre_units(metadata: VectorMetadata | RasterMetadata, field: str) -> InputCheckResult:
    try:
        info = inspect_crs(metadata, field=field)
    except CRSValidationError as error:
        return _result("horizontal_unit_is_meter", field, False, metadata.crs, "meter", error.message)
    passed = info.metres_per_unit == 1
    return _result(
        "horizontal_unit_is_meter", field, passed,
        {"crs": metadata.crs, "unit": info.horizontal_unit, "metres_per_unit": info.metres_per_unit}, "meter",
        "Horizontal axes use metres" if passed else "Input requires horizontal metre axes",
    )


def _measurement_check(
    metadata: VectorMetadata | RasterMetadata, field: str, method: str, contract: ToolContract,
    *, target_crs: JsonValue = None,
) -> InputCheckResult:
    rule = {
        "local_projected": _DISTANCE_RULE, "equal_area": _AREA_RULE, "geodesic": "geodesic_measurement_is_supported",
    }[method]
    observed: dict[str, JsonValue] = {"crs": metadata.crs, "bounds": _bounds(metadata)}
    expected = {"method": method, "unit": "square_meter" if method == "equal_area" else "meter"}
    try:
        if method == "equal_area":
            decision = validate_step_area(metadata, contract, field=field, target_crs=target_crs)
        elif method == "geodesic":
            decision = validate_tool_distance(metadata, contract, field=field)
        else:
            decision = validate_distance(metadata, method=method, field=field)
    except (CRSValidationError, MeasurementPolicyError) as error:
        observed["detail"] = list(error.observed) if isinstance(error.observed, tuple) else error.observed
        return _result(rule, error.field if error.field == "parameters.area_crs" else field, False, observed, expected, error.message)
    observed["unit"] = decision.unit
    if requires_area_target(contract, field=field):
        observed["measurement_crs"] = decision.crs
    message = "Measurement policy is suitable for the input extent"
    if method == "equal_area" and permits_empty_area_input(metadata, contract, field=field):
        observed["feature_count"] = 0
        message = "The empty target layer has a valid equal-area metre CRS"
    return _result(rule, field, True, observed, expected, message)


def _vector_checks(
    path: Path, layer: str | None, metadata: VectorMetadata, kind: str, field: str,
    *, crs: CRS | None, geometry_required: bool, unique_ids: bool, coverage_required: bool,
    checks: list[InputCheckResult],
) -> BaseGeometry | None:
    geometry_field = f"{field}.geometry"
    expected = "Valid nonempty geometry in every feature"
    columns = [_ID] if unique_ids and any(column.name == _ID for column in metadata.fields) else []
    try:
        frame = pyogrio.read_dataframe(path, layer=layer, columns=columns, fid_as_index=True)
        if (
            not isinstance(frame, GeoDataFrame) or len(frame) != metadata.feature_count
            or (crs is not None and (frame.crs is None or not frame.crs.equals(crs, ignore_axis_order=True)))
        ):
            raise ValueError("The reopened layer differs from its inspected metadata")
    except (DataSourceError, DataLayerError, OSError, ValueError) as error:
        checks.append(_result("geometry_is_valid", geometry_field, False, None, expected, str(error)))
        return None

    if unique_ids:
        identifiers = frame[_ID] if _ID in frame else None
        valid_ids = False
        if identifiers is not None:
            values = identifiers.astype(str)
            valid_ids = bool(not identifiers.isna().any() and values.is_unique and not values.str.strip().eq("").any())
        checks.append(_result(
            "source_feature_id_is_unique", f"{field}.{_ID}", valid_ids,
            {"present": identifiers is not None, "feature_count": len(frame)}, "Nonempty unique source_feature_id values",
            "Original feature IDs are nonempty and unique" if valid_ids else "Input requires nonempty unique original feature IDs",
        ))
    if not geometry_required:
        return None

    geometry = frame.geometry
    types = geometry.geom_type
    allowed = [
        name for name in types.dropna().unique()
        if matches_vector_type(metadata.model_copy(update={"geometry_type": name}), kind)
    ]
    invalid = ~geometry.is_valid | geometry.is_empty | ~types.isin(allowed)
    invalid_count = int(invalid.sum())
    checks.append(_result(
        "geometry_is_valid", geometry_field, invalid_count == 0,
        {"feature_count": len(frame), "invalid_count": invalid_count, "invalid_fids": frame.index[invalid][:20].tolist()},
        expected, (
            "Input feature geometries are valid" if invalid_count == 0
            else "Input contains missing, empty, invalid or unexpected geometries"
        ),
    ))
    if not coverage_required or invalid_count or not types.isin({"Polygon", "MultiPolygon"}).all():
        return None
    try:
        return shapely.union_all(geometry.to_numpy())
    except GEOSException:
        return None


def _coverage_check(
    metadata: VectorMetadata | RasterMetadata, crs: CRS | None, footprint: BaseGeometry | None,
    required: CoverageRequirement, field: str,
) -> InputCheckResult:
    observed = {"crs": metadata.crs, "bounds": _bounds(metadata)}
    expected = {"crs": required.crs, "bounds": list(required.bounds)}
    passed = False
    if crs is None or not crs.equals(CRS.from_user_input(required.crs), ignore_axis_order=True):
        message = "Coverage requires the input and requested bounds to use the same known CRS"
    elif footprint is None:
        message = "Coverage requires a valid raster footprint or polygon layer"
    else:
        passed = bool(footprint.covers(box(*required.bounds)))
        message = "Data covers the requested bounds" if passed else "Data footprint does not cover the requested bounds"
    return _result(_COVERAGE_RULE, field, passed, observed, expected, message)


def check_step_inputs(
    step: PreparedStep, *, required_coverage: Mapping[str, CoverageRequirement] | None = None,
) -> tuple[InputCheckResult, ...]:
    """Inspect configured spatial inputs and return ordered observations and expectations."""
    checks: list[InputCheckResult] = []
    coverage = required_coverage or {}
    spatial = {role: kind for role, kind in step.contract.inputs.items() if kind.startswith(("raster", "vector"))}
    preconditions = frozenset(step.contract.preconditions)
    policy = step.contract.measurement_policy
    distance = policy.distance if policy.distance != "not_applicable" else None
    if distance is None and _DISTANCE_RULE in preconditions:
        distance = "local_projected"
    area = policy.area == "equal_area" or _AREA_RULE in preconditions
    needs_metres = distance == "local_projected" or "horizontal_unit_is_meter" in preconditions
    needs_crs = distance is not None or needs_metres or area or bool({"crs_is_known", "input_crs_match"} & preconditions)
    crs_by_role: dict[str, CRS] = {}

    for role in sorted(coverage.keys() - spatial.keys()):
        checks.append(_result(
            _COVERAGE_RULE, f"inputs.{role}.coverage", False, None, "Declared spatial input role",
            "Coverage refers to an undeclared or nonspatial input role",
        ))
    for role, kind in spatial.items():
        field = f"inputs.{role}"
        reference = step.request["inputs"][role]
        try:
            path, layer, metadata = _read_spatial_input(step, role, kind)
        except (SourceReadError, AdapterError) as error:
            location = f"{field}.{error.field.rsplit('.', 1)[-1]}" if error.field else field
            checks.append(_result("source_is_readable", location, False, reference, "Readable local spatial input", str(error)))
            continue
        checks.append(_result(
            "source_is_readable", f"{field}.path", True, {"path": reference["path"], "layer": layer},
            "Readable local spatial input", "Input reopened",
        ))
        matches = (
            isinstance(metadata, RasterMetadata) if kind.startswith("raster")
            else isinstance(metadata, VectorMetadata) and matches_vector_type(metadata, kind)
        )
        observed_type = metadata.geometry_type if isinstance(metadata, VectorMetadata) else metadata.kind
        checks.append(_result(
            "input_type_matches", field, matches, observed_type, kind,
            "Input type matches" if matches else "Input type differs from the contract",
        ))
        if not matches or not isinstance(metadata, (RasterMetadata, VectorMetadata)):
            continue

        raster = isinstance(metadata, RasterMetadata)
        footprint = _raster_footprint(metadata) if raster else None
        extent_valid = footprint is not None if raster else metadata.bounds is not None
        if isinstance(metadata, VectorMetadata) and metadata.feature_count == 0 and distance is None and not area:
            extent_valid = True
        if isinstance(metadata, VectorMetadata) and metadata.feature_count == 0 and permits_empty_geodesic_input(
            step.contract, role=role,
        ):
            extent_valid = True
        if area and permits_empty_area_input(metadata, step.contract, field=f"{field}.crs"):
            extent_valid = True
        checks.append(_result(
            "input_extent_is_valid", f"{field}.bounds", extent_valid, _bounds(metadata),
            "Finite ordered extent, with a positive raster footprint",
            "Input extent is usable" if extent_valid else "Input extent is missing or invalid",
        ))
        required = coverage.get(role)
        crs = _known_crs(metadata, f"{field}.crs", checks) if needs_crs or required is not None else None
        if crs is not None:
            crs_by_role[role] = crs
            if needs_metres or (area and not requires_area_target(step.contract, field=f"{field}.crs")):
                checks.append(_metre_units(metadata, f"{field}.crs.unit"))
            if distance is not None:
                checks.append(_measurement_check(metadata, f"{field}.crs", distance, step.contract))
            if area:
                checks.append(_measurement_check(
                    metadata, f"{field}.crs", "equal_area", step.contract,
                    target_crs=step.request["parameters"].get("area_crs"),
                ))

        if isinstance(metadata, VectorMetadata):
            geometry_required = "geometry_is_valid" in preconditions or required is not None
            unique_ids = "source_feature_id_is_unique" in preconditions
            if geometry_required or unique_ids:
                footprint = _vector_checks(
                    path, layer, metadata, kind, field, crs=crs, geometry_required=geometry_required,
                    unique_ids=unique_ids, coverage_required=required is not None, checks=checks,
                )
            if role == "source" and "selected_field_exists" in preconditions:
                selected = step.request["parameters"].get("field")
                names = [column.name for column in metadata.fields]
                present = isinstance(selected, str) and selected in names
                checks.append(_result(
                    "selected_field_exists", f"{field}.fields", present, names, selected,
                    "Selected field exists" if present else f"Selected field {selected!r} is absent",
                ))
        if required is not None:
            checks.append(_coverage_check(metadata, crs, footprint, required, f"{field}.coverage"))

    if "input_crs_match" in preconditions and len(spatial) > 1 and len(crs_by_role) == len(spatial):
        first = next(iter(crs_by_role.values()))
        match = all(first.equals(crs, ignore_axis_order=True) for crs in crs_by_role.values())
        checks.append(_result(
            "input_crs_match", "inputs.crs", match, {role: crs.to_string() for role, crs in crs_by_role.items()},
            "Same known CRS across spatial inputs", "Spatial input CRS match" if match else "Spatial inputs use different CRS",
        ))
    return tuple(checks)


def preflight_step_inputs(
    step: PreparedStep, *, required_coverage: Mapping[str, CoverageRequirement] | None = None,
) -> None:
    """Record typed input evidence before blocking any further execution."""
    _record_input_checks(step, check_step_inputs(step, required_coverage=required_coverage))


def _record_input_checks(step: PreparedStep, results: tuple[InputCheckResult, ...]) -> None:
    with step.log_path.open("a", encoding="utf-8") as log:
        for result in results:
            log.write(f"input_check: {result.model_dump_json()}\n")
    require_input_checks(results)


def preflight_raster_nodata(step: PreparedStep) -> None:
    """Enforce the exact tool version's declared raster NoData prerequisites."""
    checks: list[InputCheckResult] = []

    def check_cancelled() -> None:
        if step.cancel_requested is not None and step.cancel_requested():
            raise AdapterError(
                "The step was cancelled during raster input checks.", code="PROCESS_CANCELLED", phase="preflight",
            )

    for role, policy in step.contract.raster_nodata.items():
        check_cancelled()
        field = f"inputs.{role}"
        kind = step.contract.inputs[role]
        reference = step.request["inputs"][role]
        if reference.get("layer") is not None:
            checks.append(_result(
                "source_is_readable", f"{field}.layer", False, reference["layer"], None,
                "A raster input cannot specify a vector layer",
            ))
            continue
        path = resolve_step_path(step.workdir, reference["path"], field=f"{field}.path")
        try:
            if path.as_posix().startswith("/vsi"):
                raise ValueError("GDAL virtual filesystem paths are outside the local source contract")
            with rasterio.open(path, "r", driver="GTiff") as dataset:
                if policy.band_parameter is not None:
                    bands = (step.request["parameters"][policy.band_parameter],)
                else:
                    bands = dataset.indexes if policy.bands == "all" else policy.bands
                for band in bands:
                    band_field = f"{field}.bands.{band}"
                    if not 1 <= band <= dataset.count:
                        checks.append(_result(
                            _RASTER_NODATA_RULE, band_field, False,
                            {"input_type": kind, "band": band, "band_count": dataset.count},
                            "Available one-based raster band", f"Raster band {band} is unavailable",
                        ))
                        continue
                    passed, observed, message = check_band_nodata(dataset, band, policy, check_cancelled=check_cancelled)
                    checks.append(_result(
                        _RASTER_NODATA_RULE, f"{band_field}.nodata", passed, {"input_type": kind, **observed},
                        {"mode": policy.mode, "nodata_declaration": policy.nodata_declaration}, message,
                    ))
        except AdapterError:
            raise
        except (OSError, RasterioError, RuntimeError, ValueError) as error:
            checks.append(_result(
                "source_is_readable", f"{field}.path", False, {"path": reference["path"], "input_type": kind},
                "Readable local GeoTIFF or COG", f"Unable to inspect raster NoData: {error}",
            ))
    _record_input_checks(step, tuple(checks))
