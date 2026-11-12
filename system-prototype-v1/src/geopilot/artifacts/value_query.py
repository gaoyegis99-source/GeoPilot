"""Sample one native pixel of an exact registered raster."""

import math
from typing import Annotated, Literal

import numpy as np
from affine import Affine, TransformNotInvertibleError
from pydantic import BaseModel, ConfigDict, Field
from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from rasterio.errors import RasterioError
from rasterio.io import DatasetReader
from rasterio.windows import Window

from geopilot.artifacts.models import Artifact


_WGS84 = CRS.from_epsg(4326)
_MAX_SAFE_INTEGER = 2**53 - 1
_Index = Annotated[int, Field(strict=True, ge=0)]


class RasterValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    artifact_id: str
    source_fingerprint: str
    band: Annotated[int, Field(strict=True, gt=0)]
    longitude: float
    latitude: float
    source_crs: str
    source_x: float
    source_y: float
    row: _Index | None
    column: _Index | None
    status: Literal["value", "nodata", "outside"]
    nodata: bool
    value: int | float | str | None
    dtype: str
    unit: str | None
    scale: float
    offset: float


class RasterValueError(ValueError):
    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


def _axis_index(position: float, coordinate: float, origin: float, step: float) -> int:
    # Compare with the same representable coordinate that the forward grid uses.
    # Dividing the translated coordinates alone can round to the wrong side.
    nearest = round(position)
    boundary = origin + nearest * step
    after_boundary = coordinate >= boundary if step > 0 else coordinate <= boundary
    return nearest if after_boundary else nearest - 1


def _gridline_floor(position: float, uncertainty: float) -> int:
    nearest = round(position)
    return nearest if abs(position - nearest) <= min(uncertainty, 1e-6) else math.floor(position)


def _grid_cell(grid: Affine, x: float, y: float, *, native_wgs84: bool) -> tuple[int, int]:
    try:
        inverse = ~Affine(grid.a, grid.b, 0, grid.d, grid.e, 0)
    except TransformNotInvertibleError:
        raise RasterValueError("The raster grid has no invertible affine transform.", field="artifact.transform") from None
    column, row = inverse * (x - grid.c, y - grid.f)
    if not math.isfinite(column) or not math.isfinite(row):
        raise RasterValueError("The raster grid cannot locate this coordinate.", field="artifact.transform")
    if native_wgs84 and grid.b == 0 and grid.d == 0:
        return _axis_index(row, y, grid.f, grid.e), _axis_index(column, x, grid.c, grid.a)

    # PROJ and a rotated inverse affine can introduce roundoff on an exact grid line.
    source_uncertainty = 8 * max(map(math.ulp, (x, y, grid.c, grid.f)))
    row_uncertainty = source_uncertainty * (abs(inverse.d) + abs(inverse.e))
    col_uncertainty = source_uncertainty * (abs(inverse.a) + abs(inverse.b))
    return _gridline_floor(row, row_uncertainty), _gridline_floor(column, col_uncertainty)


def _project(dataset: DatasetReader, longitude: float, latitude: float) -> tuple[float, float, bool]:
    if dataset.crs is None:
        raise RasterValueError("Pixel lookup requires a known raster CRS.", field="artifact.crs")
    try:
        horizontal = CRS.from_user_input(dataset.crs).to_2d()
        native_wgs84 = horizontal.equals(_WGS84, ignore_axis_order=True)
        if native_wgs84:
            return longitude, latitude, True
        x, y = Transformer.from_crs(_WGS84, horizontal, always_xy=True).transform(longitude, latitude, errcheck=True)
    except (ProjError, ValueError):
        raise RasterValueError("The point cannot be projected into the raster CRS.", field="artifact.crs") from None
    if not math.isfinite(x) or not math.isfinite(y):
        raise RasterValueError("The point cannot be projected into the raster CRS.", field="artifact.crs")
    return x, y, False


def _read_sample(dataset: DatasetReader, band: int, row: int, column: int) -> int | float | str | None:
    try:
        samples = dataset.read(band, window=Window(column, row, 1, 1), masked=True)
    except (OSError, RasterioError, ValueError):
        raise RasterValueError("Could not read the original raster pixel.", field="artifact") from None
    sample = samples[0, 0]
    if np.ma.is_masked(sample):
        return None
    value = sample.item()
    if not math.isfinite(value):
        return None
    if isinstance(value, int) and abs(value) > _MAX_SAFE_INTEGER:
        return str(value)
    return value


def query_raster_value(
    artifact: Artifact, dataset: DatasetReader, *, longitude: float, latitude: float, band: int = 1,
) -> RasterValue:
    """Read the original band value, using a half-open pixel grid in source coordinates."""
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise RasterValueError("Longitude must be between -180 and 180.", field="longitude")
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise RasterValueError("Latitude must be between -90 and 90.", field="latitude")
    if type(band) is not int or not 1 <= band <= dataset.count:
        raise RasterValueError("The selected raster band is unavailable.", field="band")
    dtype = dataset.dtypes[band - 1]
    if dtype.startswith("complex") or np.dtype(dtype).kind not in "iuf":
        raise RasterValueError("Pixel lookup requires a real-valued raster band.", field="band")
    scale, offset = dataset.scales[band - 1], dataset.offsets[band - 1]
    if not math.isfinite(scale) or not math.isfinite(offset):
        raise RasterValueError("The selected band has an invalid scale or offset.", field="band")

    x, y, native_wgs84 = _project(dataset, longitude, latitude)
    row, column = _grid_cell(dataset.transform, x, y, native_wgs84=native_wgs84)
    inside = 0 <= row < dataset.height and 0 <= column < dataset.width
    value = _read_sample(dataset, band, row, column) if inside else None
    nodata = inside and value is None
    epsg = dataset.crs.to_epsg(confidence_threshold=100)
    return RasterValue(
        artifact_id=artifact.id, source_fingerprint=artifact.fingerprint, band=band,
        longitude=longitude, latitude=latitude, source_crs=f"EPSG:{epsg}" if epsg is not None else dataset.crs.to_wkt(),
        source_x=x, source_y=y, row=row if inside else None, column=column if inside else None,
        status=("nodata" if nodata else "value") if inside else "outside", nodata=nodata, value=value,
        dtype=dtype, unit=dataset.units[band - 1], scale=scale, offset=offset,
    )
