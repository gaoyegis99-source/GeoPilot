"""Geographic extent checks shared by measurement policies."""

import math
from dataclasses import dataclass

from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError


Bounds = tuple[float, float, float, float]
_DEGREE_TOLERANCE = 1e-8


class MeasurementPolicyError(ValueError):
    code = "MEASUREMENT_POLICY_REJECTED"
    phase = "preflight"

    def __init__(
        self, message: str, *, field: str, observed: object, suggestion: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.observed = observed
        self.suggestion = suggestion


@dataclass(frozen=True, slots=True)
class MeasurementDecision:
    method: str
    crs: str
    unit: str


def geographic_bounds(crs: CRS, bounds: Bounds | None, *, field: str) -> Bounds:
    """Transform the input bounds to a finite, non-wrapping WGS84 extent."""
    if bounds is None or len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
        raise MeasurementPolicyError("Finite spatial bounds are required", field=field, observed=bounds)
    west, south, east, north = bounds
    if west > east or south > north:
        raise MeasurementPolicyError("Spatial bounds must be ordered", field=field, observed=bounds)
    try:
        transformer = Transformer.from_crs(crs.to_2d(), 4326, always_xy=True, allow_ballpark=False, only_best=True)
        extent = transformer.transform_bounds(*bounds, densify_pts=21, errcheck=True)
    except ProjError as error:
        raise MeasurementPolicyError(
            "Spatial bounds could not be transformed to WGS84", field=field, observed=bounds
        ) from error
    west, south, east, north = extent
    if (
        not all(math.isfinite(value) for value in extent)
        or not -180 - _DEGREE_TOLERANCE <= west <= east <= 180 + _DEGREE_TOLERANCE
        or not -90 - _DEGREE_TOLERANCE <= south <= north <= 90 + _DEGREE_TOLERANCE
    ):
        raise MeasurementPolicyError(
            "Measurement requires finite WGS84 bounds within a single longitude interval",
            field=field,
            observed=extent,
        )
    return max(west, -180), max(south, -90), min(east, 180), min(north, 90)


def verify_projection_coverage(crs: CRS, bounds: Bounds | None, *, field: str) -> Bounds:
    """Require the full geographic extent to fit in the CRS area of use."""
    area = crs.area_of_use
    if area is None:
        raise MeasurementPolicyError(
            "The CRS has no documented area of use", field=field, observed=crs.to_string()
        )
    extent = geographic_bounds(crs, bounds, field=field)
    west, south, east, north = extent
    tolerance = _DEGREE_TOLERANCE
    if area.west <= area.east:
        within_longitudes = area.west - tolerance <= west and east <= area.east + tolerance
    else:
        within_longitudes = area.west - tolerance <= west or east <= area.east + tolerance
    if not within_longitudes or not (area.south - tolerance <= south and north <= area.north + tolerance):
        raise MeasurementPolicyError(
            f"Input bounds fall outside the area of use for {crs.to_string()}",
            field=field,
            observed=extent,
            suggestion="Reproject the input to a measurement CRS that covers its full extent",
        )
    return extent
