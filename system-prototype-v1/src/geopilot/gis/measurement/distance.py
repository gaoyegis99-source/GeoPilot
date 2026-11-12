"""Check whether a projected metre grid is suitable for local distance."""

import math
from itertools import product

from pyproj import CRS, Proj, Transformer
from pyproj.exceptions import ProjError

from geopilot.domain.data_asset import AssetMetadata
from geopilot.domain.tool_contract import ToolContract
from geopilot.gis.measurement.coverage import (
    Bounds,
    MeasurementDecision,
    MeasurementPolicyError,
    verify_projection_coverage,
)
from geopilot.gis.validation.crs_validator import CRSValidationError, inspect_crs


_MAX_LINEAR_DISTORTION = 0.001
_SAMPLE_FRACTIONS = (0.0, 0.25, 0.5, 0.75, 1.0)
_GEODESIC_BUFFER = ("python-wgs84-geodesic-buffer", 1, "python", "geopilot.wgs84_geodesic_buffer")
_GEODESIC_SCOPE = ("python-expand-wgs84-analysis-extent", 1, "python", "geopilot.expand_wgs84_analysis_extent")


def permits_empty_geodesic_input(contract: ToolContract, *, role: str) -> bool:
    """Identify the exact geodesic ports that can produce a useful empty result."""
    identity = contract.id, contract.version, contract.runner, contract.implementation
    if contract.measurement_policy.distance != "geodesic":
        return False
    return (identity == _GEODESIC_BUFFER and role == "source") or (
        identity == _GEODESIC_SCOPE and role in {"targets", "zones"}
    )


def _sample_distortion(crs: CRS, extent: Bounds) -> float:
    west, south, east, north = extent
    longitudes = [west + fraction * (east - west) for fraction in _SAMPLE_FRACTIONS]
    latitudes = [south + fraction * (north - south) for fraction in _SAMPLE_FRACTIONS]
    sample_x, sample_y = zip(*product(longitudes, latitudes))
    horizontal = crs.to_2d()
    if horizontal.is_bound and horizontal.source_crs is not None:
        horizontal = horizontal.source_crs.to_2d()
    geodetic = horizontal.geodetic_crs
    if geodetic is None or len(geodetic.axis_info) != 2:
        raise ProjError("Projected CRS has no two-dimensional geodetic base")
    transformer = Transformer.from_crs(4326, geodetic, always_xy=True, allow_ballpark=False, only_best=True)
    longitude, latitude = transformer.transform(sample_x, sample_y, errcheck=True)
    unit_factors = {axis.unit_conversion_factor for axis in geodetic.axis_info}
    if len(unit_factors) != 1:
        raise ProjError("Geodetic axes have incompatible units")
    to_radians = unit_factors.pop()
    factors = Proj(horizontal).get_factors(
        [value * to_radians for value in longitude],
        [value * to_radians for value in latitude],
        radians=True,
        errcheck=True,
    )
    scales = (*factors.tissot_semiminor, *factors.tissot_semimajor)
    if not all(math.isfinite(scale) for scale in scales):
        raise ProjError("Projection returned non-finite scale factors")
    return max(abs(scale - 1) for scale in scales)


def validate_distance(
    metadata: AssetMetadata, *, method: str = "local_projected", field: str = "inputs.source.crs"
) -> MeasurementDecision:
    """Require a metre projection with adequate coverage and local linear scale."""
    if method == "geodesic":
        raise MeasurementPolicyError(
            "Geodesic distance is unavailable without a supported tool contract",
            field="measurement.distance_method",
            observed=method,
        )
    if method != "local_projected":
        raise MeasurementPolicyError(
            f"Unsupported distance method: {method}", field="measurement.distance_method", observed=method
        )
    try:
        info = inspect_crs(metadata, field=field, require_projected_metres=True)
    except CRSValidationError as error:
        if error.code == "CRS_UNKNOWN":
            raise
        raise MeasurementPolicyError(
            error.message, field=error.field, observed=error.observed,
            suggestion="Reproject the input to a suitable local metre CRS",
        ) from error

    extent = verify_projection_coverage(info.crs, metadata.bounds, field=field)
    try:
        distortion = _sample_distortion(info.crs, extent)
    except ProjError as error:
        raise MeasurementPolicyError(
            "Local linear distortion could not be evaluated", field=field, observed=metadata.crs
        ) from error
    if distortion > _MAX_LINEAR_DISTORTION:
        raise MeasurementPolicyError(
            f"Sampled relative linear distortion {distortion:.6g} exceeds {_MAX_LINEAR_DISTORTION}",
            field=field,
            observed=distortion,
            suggestion="Choose a metre projection with lower distortion over the input extent",
        )
    assert metadata.crs is not None
    return MeasurementDecision(method=method, crs=metadata.crs, unit="meter")


def validate_tool_distance(
    metadata: AssetMetadata, contract: ToolContract, *, field: str = "inputs.source.crs",
) -> MeasurementDecision:
    """Permit geodesic measurement only for verified worker contracts."""
    method = contract.measurement_policy.distance
    identity = contract.id, contract.version, contract.runner, contract.implementation
    if method == "geodesic" and identity == _GEODESIC_BUFFER:
        from geopilot.gis.measurement.geodesic_buffer import validate_geodesic_buffer_input

        return validate_geodesic_buffer_input(metadata, field=field)
    if method == "geodesic" and identity == _GEODESIC_SCOPE:
        from geopilot.gis.measurement.geodesic_road_scope import validate_geodesic_scope_input

        role = field.removeprefix("inputs.").removesuffix(".crs")
        allow_empty = permits_empty_geodesic_input(contract, role=role)
        return validate_geodesic_scope_input(metadata, field=field, allow_empty=allow_empty)
    return validate_distance(metadata, method=method, field=field)
