"""Choose a measurement CRS and prepare a separately reprojected vector layer."""

import errno
import os
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import shapely
from pandas.testing import assert_frame_equal
from pyogrio.util import vsi_path
from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info
from pyproj.exceptions import ProjError

from geopilot.data.assets import InputChangedError, inspect_local_source
from geopilot.data.errors import SourceReadError
from geopilot.data.fingerprint import capture_file_state, fingerprint_file
from geopilot.data.vector_reader import local_ogr_path
from geopilot.domain.data_asset import AssetMetadata, VectorMetadata
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.output_staging import temporary_output_directory
from geopilot.gis.measurement.area import validate_area, validate_tool_area
from geopilot.gis.measurement.coverage import (
    Bounds,
    MeasurementDecision,
    MeasurementPolicyError,
    geographic_bounds,
)
from geopilot.gis.measurement.distance import validate_distance
from geopilot.gis.validation.crs_validator import CRSValidationError, inspect_crs


_METHODS = {"local_projected": validate_distance, "equal_area": validate_area}
_TARGET_FIELD = "measurement.target_crs"
_VECTOR_SUFFIXES = {".geojson", ".json", ".shp", ".gpkg"}


@dataclass(frozen=True, slots=True)
class ReprojectionResult:
    path: Path
    layer: str
    decision: MeasurementDecision


def _strict_transformer(source: CRS, target: CRS) -> Transformer:
    return Transformer.from_crs(source, target, always_xy=True, allow_ballpark=False, only_best=True)


def _select_utm(extent: Bounds) -> str:
    candidates = {
        f"{info.auth_name}:{info.code}"
        for info in query_utm_crs_info(
            datum_name="WGS 84", area_of_interest=AreaOfInterest(*extent), contains=True
        )
    }
    if len(candidates) != 1:
        raise MeasurementPolicyError(
            "Automatic local projection requires exactly one WGS 84 UTM zone covering the input",
            field=_TARGET_FIELD,
            observed={"bounds": extent, "candidates": sorted(candidates)},
        )
    return next(iter(candidates))


def _target_definition(target: str | int | CRS) -> str:
    if isinstance(target, CRS):
        return target.to_string()
    if type(target) is int:
        return f"EPSG:{target}"
    if isinstance(target, str):
        return target
    raise CRSValidationError(
        "Expected a known target CRS definition", code="CRS_UNKNOWN", field=_TARGET_FIELD, observed=target
    )


def select_measurement_crs(
    metadata: AssetMetadata, *, method: str, target_crs: str | int | CRS | None = None
) -> MeasurementDecision:
    """Evaluate the selected method against the transformed input extent."""
    if method not in _METHODS:
        raise MeasurementPolicyError(
            "Unsupported projection measurement method", field="measurement.method", observed=method
        )
    source = inspect_crs(metadata)
    extent = geographic_bounds(source.crs, metadata.bounds, field="inputs.source.bounds")
    if target_crs is None:
        target_crs = _select_utm(extent) if method == "local_projected" else "EPSG:6933"
    target_metadata = metadata.model_copy(update={"crs": _target_definition(target_crs)})
    try:
        target = inspect_crs(target_metadata, field=_TARGET_FIELD, require_projected_metres=True)
    except CRSValidationError as error:
        if error.code == "CRS_UNKNOWN":
            raise
        raise MeasurementPolicyError(error.message, field=error.field, observed=error.observed) from error

    assert metadata.bounds is not None  # geographic_bounds checked the source extent.
    try:
        bounds = _strict_transformer(source.crs.to_2d(), target.crs.to_2d()).transform_bounds(
            *metadata.bounds, densify_pts=21, errcheck=True
        )
    except ProjError as error:
        raise MeasurementPolicyError(
            "Input extent could not be transformed to the target CRS",
            field=_TARGET_FIELD,
            observed=target_metadata.crs,
        ) from error
    return _METHODS[method](target_metadata.model_copy(update={"bounds": bounds}), field=_TARGET_FIELD)


def requires_area_target(contract: ToolContract, *, field: str) -> bool:
    """Identify the input whose cell boundaries are projected within the tool."""
    legacy = (contract.id, contract.version, contract.runner, contract.implementation, field) == (
        "python-dem-area-stats", 1, "python", "geopilot.summarize_dem_area", "inputs.threshold_regions.crs",
    )
    if legacy:
        return True
    if contract.measurement_policy.area != "equal_area" or "area_projected_from_raster_cells" not in contract.preconditions:
        return False
    target = contract.parameters.get("area_crs")
    raster_roles = [role for role, kind in contract.inputs.items() if kind.startswith("raster")]
    return target is not None and target.type == "string" and len(raster_roles) == 1 and field == f"inputs.{raster_roles[0]}.crs"


def validate_step_area(
    metadata: AssetMetadata, contract: ToolContract, *, field: str, target_crs: object = None,
) -> MeasurementDecision:
    if not requires_area_target(contract, field=field):
        return validate_tool_area(metadata, contract, field=field)
    parameter = "parameters.area_crs"
    if not isinstance(target_crs, str) or not target_crs.strip():
        raise CRSValidationError("Specify the equal-area target CRS", code="CRS_UNKNOWN", field=parameter, observed=target_crs)
    try:
        return select_measurement_crs(metadata, method="equal_area", target_crs=target_crs)
    except CRSValidationError as error:
        if error.field == _TARGET_FIELD:
            raise CRSValidationError(error.message, code=error.code, field=parameter, observed=error.observed) from error
        raise
    except MeasurementPolicyError as error:
        if error.field == _TARGET_FIELD:
            raise MeasurementPolicyError(
                error.message, field=parameter, observed=error.observed, suggestion=error.suggestion,
            ) from error
        raise


def _require_unused_output(path: Path) -> None:
    for candidate in (path, *(path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal"))):
        if candidate.exists() or candidate.is_symlink():
            raise FileExistsError(errno.EEXIST, "Projection output already exists", candidate)


def _new_geopackage_path(destination: str | Path) -> Path:
    path = Path(destination).expanduser()
    path = path.parent.resolve(strict=True) / path.name
    if path.suffix.lower() != ".gpkg" or not path.parent.is_dir():
        raise ValueError("Expected a new .gpkg path in an existing directory")
    if path.as_posix().startswith("/vsi") or vsi_path(path) != str(path):
        raise ValueError("Projection output must be a local filesystem path")
    _require_unused_output(path)
    return path


def _geometry_bounds(frame: gpd.GeoDataFrame, *, field: str) -> Bounds:
    shapes = frame.geometry.to_numpy()
    coordinates = shapely.get_coordinates(shapes)
    if (
        not coordinates.size
        or not np.isfinite(coordinates).all()
        or not shapely.is_valid(shapes[~shapely.is_missing(shapes)]).all()
    ):
        raise MeasurementPolicyError(
            "Vector layer must have finite and valid geometries", field=field, observed=frame.crs.to_string()
        )
    left, bottom, right, top = frame.total_bounds
    return float(left), float(bottom), float(right), float(top)


def _transform_vector(frame: gpd.GeoDataFrame, source: CRS, target: CRS) -> gpd.GeoDataFrame:
    try:
        transformer = _strict_transformer(source, target)

        def transform_coordinates(x, y, z=None):
            return transformer.transform(x, y, z, errcheck=True)

        shapes = shapely.transform(
            frame.geometry.to_numpy(), transform_coordinates, include_z=None, interleaved=False
        )
    except ProjError as error:
        raise MeasurementPolicyError(
            "Vector geometries could not be transformed", field=_TARGET_FIELD, observed=target.to_string()
        ) from error
    return frame.set_geometry(gpd.GeoSeries(shapes, index=frame.index, name=frame.geometry.name, crs=target))


def _layer_options(frame: gpd.GeoDataFrame) -> dict[str, str]:
    used_names = {name.casefold() for name in frame.columns}
    options = {"FID": "geopilot_fid", "GEOMETRY_NAME": "geopilot_geometry"}
    for key, name in options.items():
        while name.casefold() in used_names:
            name += "_"
        options[key] = name
    return options


def _write_verified_layer(frame: gpd.GeoDataFrame, path: Path, layer: str, target: CRS) -> None:
    pyogrio.write_dataframe(
        frame, path, layer=layer, driver="GPKG", promote_to_multi=False, layer_options=_layer_options(frame)
    )
    reopened = pyogrio.read_dataframe(path, layer=layer)
    if reopened.crs is None or not target.equals(CRS.from_user_input(reopened.crs), ignore_axis_order=True):
        raise OSError("Reopened projection output has an unexpected CRS")
    if reopened.geometry.to_wkb().tolist() != frame.geometry.to_wkb().tolist():
        raise OSError("Reopened projection output has unexpected geometry")
    try:
        assert_frame_equal(
            reopened.drop(columns=reopened.geometry.name).reset_index(drop=True),
            frame.drop(columns=frame.geometry.name).reset_index(drop=True),
            check_dtype=False,
            check_exact=True,
        )
    except AssertionError as error:
        raise OSError("Reopened projection output has unexpected attributes") from error
    if any(entry != path for entry in path.parent.iterdir()):
        raise OSError("Projection output did not close to a single GeoPackage file")


def reproject_vector(
    source: str | Path,
    destination: str | Path,
    *,
    method: str,
    target_crs: str | int | CRS | None = None,
    source_layer: str | None = None,
    output_layer: str | None = None,
) -> ReprojectionResult:
    """Transform a local layer and publish its verified GeoPackage without replacing a file."""
    destination = _new_geopackage_path(destination)
    layer = "projected" if output_layer is None else output_layer
    if not isinstance(layer, str) or not layer.strip() or "\x00" in layer:
        raise ValueError("Expected a nonempty output layer name")

    source_path = local_ogr_path(source)
    if source_path.suffix.lower() not in _VECTOR_SUFFIXES:
        raise SourceReadError(f"Expected a local vector file: {source_path}")
    inspected = inspect_local_source(source_path, layer=source_layer)
    metadata = inspected.metadata
    if not isinstance(metadata, VectorMetadata):
        raise SourceReadError("The selected layer has no vector geometry", field="source.layer")

    decision = select_measurement_crs(metadata, method=method, target_crs=target_crs)
    source_crs = CRS.from_user_input(metadata.crs)
    target = CRS.from_user_input(decision.crs)
    try:
        frame = pyogrio.read_dataframe(source_path, layer=inspected.source.layer)
    except (RuntimeError, ValueError, OSError) as error:
        raise SourceReadError(f"Cannot reopen vector geometries from {source_path}: {error}") from error
    if (
        not isinstance(frame, gpd.GeoDataFrame)
        or frame.crs is None
        or not source_crs.equals(CRS.from_user_input(frame.crs), ignore_axis_order=True)
        or len(frame) != metadata.feature_count
    ):
        raise InputChangedError(f"Reopened geometry or CRS differs from the inspected layer: {source_path}")
    _geometry_bounds(frame, field="inputs.source.geometry")
    projected = _transform_vector(frame, source_crs, target)
    output_bounds = _geometry_bounds(projected, field=_TARGET_FIELD)
    _METHODS[method](metadata.model_copy(update={"crs": decision.crs, "bounds": output_bounds}), field=_TARGET_FIELD)

    with temporary_output_directory(destination, prefix=".geopilot-projection-") as directory:
        stage = Path(directory) / "projection.gpkg"
        _write_verified_layer(projected, stage, layer, target)
        try:
            unchanged = (
                fingerprint_file(source_path).digest == inspected.fingerprint
                and capture_file_state(source_path) == inspected.state
            )
        except OSError as error:
            raise InputChangedError(f"Could not verify unchanged projection input: {source_path}") from error
        if not unchanged:
            raise InputChangedError(f"Vector source changed during reprojection: {source_path}")
        _require_unused_output(destination)
        os.link(stage, destination)
    return ReprojectionResult(path=destination, layer=layer, decision=decision)
