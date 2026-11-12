"""Render a bounded RGBA image on a Web Mercator display grid."""

import colorsys
import math
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from rasterio.enums import Resampling
from rasterio.errors import NotGeoreferencedWarning, RasterioError
from rasterio.io import DatasetReader, MemoryFile
from rasterio.transform import Affine, from_bounds
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform, transform_bounds

from geopilot.artifacts.models import Artifact


Palette = Literal["terrain", "grayscale"]
MAX_PREVIEW_SIZE = 1024
RENDERER_VERSION = 1
_DISPLAY_CRS = "EPSG:3857"
_WORLD_LIMIT = math.pi * 6378137
_Longitude = Annotated[float, Field(ge=-180, le=180)]
_Latitude = Annotated[float, Field(ge=-90, le=90)]
_Point = tuple[_Longitude, _Latitude]
_Corners = tuple[_Point, _Point, _Point, _Point]
_Dimension = Annotated[int, Field(strict=True, ge=1, le=MAX_PREVIEW_SIZE)]
_Positive = Annotated[float, Field(gt=0)]
_Byte = Annotated[int, Field(strict=True, ge=0, le=255)]
_Color = tuple[_Byte, _Byte, _Byte, _Byte]


class _PreviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class RampStop(_PreviewModel):
    position: Annotated[float, Field(ge=0, le=1)]
    rgba: _Color


class Stretch(_PreviewModel):
    minimum: float
    maximum: float


class ClassColor(_PreviewModel):
    value: Annotated[int, Field(strict=True, ge=0, le=254)]
    rgba: _Color


class RasterPreviewDescriptor(_PreviewModel):
    artifact_id: str
    source_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    image_url: str
    coordinates: _Corners
    width: _Dimension
    height: _Dimension
    band: Annotated[int, Field(strict=True, gt=0)]
    palette: Palette
    color_mode: Literal["continuous", "categorical"]
    source_crs: str
    source_resolution: tuple[_Positive, _Positive]
    display_crs: Literal["EPSG:3857"] = _DISPLAY_CRS
    display_resolution: tuple[_Positive, _Positive]
    resampling: Literal["nearest"] = "nearest"
    ramp: tuple[RampStop, ...]
    stretch: Stretch | None
    classes: tuple[ClassColor, ...]
    nodata_alpha: Literal[0] = 0


@dataclass(frozen=True, slots=True)
class RasterPreview:
    descriptor: RasterPreviewDescriptor
    png: bytes


class RasterPreviewError(ValueError):
    """The registered raster has no renderable preview for these parameters."""


@dataclass(frozen=True, slots=True)
class _DisplayGrid:
    transform: Affine
    width: int
    height: int
    coordinates: _Corners


_RAMPS = {
    "terrain": (
        RampStop(position=0, rgba=(47, 76, 116, 255)),
        RampStop(position=0.5, rgba=(87, 154, 107, 255)),
        RampStop(position=1, rgba=(239, 218, 141, 255)),
    ),
    "grayscale": (
        RampStop(position=0, rgba=(30, 30, 30, 255)),
        RampStop(position=1, rgba=(230, 230, 230, 255)),
    ),
}


def _display_grid(dataset: DatasetReader, max_size: int) -> _DisplayGrid:
    if dataset.crs is None:
        raise RasterPreviewError("A raster preview requires a known CRS.")
    west, south, east, north = transform_bounds(dataset.crs, "EPSG:4326", *dataset.bounds, densify_pts=21)
    if (not all(map(math.isfinite, (west, south, east, north)))
            or west < -180 - 1e-8 or east > 180 + 1e-8 or west >= east or south >= north):
        raise RasterPreviewError("The raster footprint does not fit in a single map image.")
    left, bottom, right, top = transform_bounds(dataset.crs, _DISPLAY_CRS, *dataset.bounds, densify_pts=21)
    if not all(map(math.isfinite, (left, bottom, right, top))):
        raise RasterPreviewError("The raster footprint is outside the display projection.")
    left, bottom = max(left, -_WORLD_LIMIT), max(bottom, -_WORLD_LIMIT)
    right, top = min(right, _WORLD_LIMIT), min(top, _WORLD_LIMIT)
    span_x, span_y = right - left, top - bottom
    if span_x <= 0 or span_y <= 0:
        raise RasterPreviewError("The raster footprint is outside the display projection.")
    long_edge = min(max_size, max(dataset.width, dataset.height))
    longest_span = max(span_x, span_y)
    width = max(1, round(long_edge * span_x / longest_span))
    height = max(1, round(long_edge * span_y / longest_span))
    affine = from_bounds(left, bottom, right, top, width, height)
    lons, lats = transform(_DISPLAY_CRS, "EPSG:4326", [left, right], [top, bottom])
    west, east = (max(-180, min(180, lon)) for lon in lons)
    north, south = lats
    return _DisplayGrid(affine, width, height, ((west, north), (east, north), (east, south), (west, south)))


def _masked_band_vrt(dataset: DatasetReader, band: int) -> bytes:
    # Expose the actual GDAL band mask as alpha. An implicit warp alpha alone
    # can miss an internal mask on a TIFF that also declares a NoData value.
    root = ET.Element("VRTDataset", rasterXSize=str(dataset.width), rasterYSize=str(dataset.height))
    ET.SubElement(root, "SRS").text = dataset.crs.to_wkt()
    ET.SubElement(root, "GeoTransform").text = ",".join(map(str, dataset.transform.to_gdal()))
    for index, dtype, source_index in ((1, "Float64", str(band)), (2, "Byte", f"mask,{band}")):
        virtual_band = ET.SubElement(root, "VRTRasterBand", dataType=dtype, band=str(index))
        if index == 2:
            ET.SubElement(virtual_band, "ColorInterp").text = "Alpha"
        source = ET.SubElement(virtual_band, "SimpleSource")
        ET.SubElement(source, "SourceFilename", relativeToVRT="0").text = dataset.name
        ET.SubElement(source, "SourceBand").text = source_index
    return ET.tostring(root, encoding="utf-8")


def _warp_band(dataset: DatasetReader, band: int, grid: _DisplayGrid) -> tuple[np.ndarray, np.ndarray]:
    scale, offset = dataset.scales[band - 1], dataset.offsets[band - 1]
    if not math.isfinite(scale) or not math.isfinite(offset):
        raise RasterPreviewError("The selected raster band has an invalid scale or offset.")
    with MemoryFile(_masked_band_vrt(dataset, band), ext=".vrt") as memory, memory.open() as virtual:
        with WarpedVRT(
            virtual, crs=_DISPLAY_CRS, transform=grid.transform, width=grid.width, height=grid.height,
            resampling=Resampling.nearest, src_alpha=2, dst_alpha=2, dtype="float64", warp_mem_limit=32,
        ) as warped:
            values, alpha = warped.read()
    with np.errstate(over="ignore", invalid="ignore"):
        values = values * scale + offset
    return values, (alpha > 0) & np.isfinite(values)


def _continuous_color(
    rgba: np.ndarray, valid: np.ndarray, values: np.ndarray, ramp: tuple[RampStop, ...],
) -> Stretch | None:
    if not values.size:
        return None
    minimum, maximum = float(values.min()), float(values.max())
    if maximum == minimum:
        normalized = np.full(values.shape, 0.5)
    elif math.isfinite(maximum - minimum):
        normalized = (values - minimum) / (maximum - minimum)
    else:
        normalized = (values / 2 - minimum / 2) / (maximum / 2 - minimum / 2)
    positions = [stop.position for stop in ramp]
    for channel in range(4):
        rgba[channel, valid] = np.rint(np.interp(normalized, positions, [stop.rgba[channel] for stop in ramp]))
    return Stretch(minimum=minimum, maximum=maximum)


def _class_color(value: int, palette: Palette) -> _Color:
    if palette == "grayscale":
        shade = (value * 67) % 255
        return shade, shade, shade, 255
    if value == 0:
        return 236, 240, 238, 255
    red, green, blue = colorsys.hsv_to_rgb((value * 0.61803398875) % 1, 0.62, 0.80)
    return round(red * 255), round(green * 255), round(blue * 255), 255


def _categorical_color(
    rgba: np.ndarray, valid: np.ndarray, values: np.ndarray, palette: Palette,
) -> tuple[ClassColor, ...]:
    if not values.size:
        return ()
    if ((values < 0) | (values > 254) | (values != np.trunc(values))).any():
        raise RasterPreviewError("The classification raster contains invalid category values.")
    codes, indexes = np.unique(values.astype("uint8"), return_inverse=True)
    legend = tuple(ClassColor(value=int(code), rgba=_class_color(int(code), palette)) for code in codes)
    rgba[:, valid] = np.asarray([entry.rgba for entry in legend], dtype="uint8")[indexes].T
    return legend


def _encode_png(rgba: np.ndarray, grid: _DisplayGrid) -> bytes:
    with warnings.catch_warnings(), MemoryFile() as memory:
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with memory.open(driver="PNG", width=grid.width, height=grid.height, count=4, dtype="uint8") as output:
            output.write(rgba)
        return memory.read()


def render_raster_preview(
    artifact: Artifact, dataset: DatasetReader, *, palette: Palette, max_size: int, band: int, image_url: str,
) -> RasterPreview:
    """Project one real band, record display metadata, and encode an RGBA PNG."""
    if type(max_size) is not int or not 1 <= max_size <= MAX_PREVIEW_SIZE:
        raise RasterPreviewError("The raster preview size is outside the supported range.")
    if type(band) is not int or not 1 <= band <= dataset.count or np.dtype(dataset.dtypes[band - 1]).kind not in "iuf":
        raise RasterPreviewError("The raster preview requires an available real-valued band.")
    try:
        grid = _display_grid(dataset, max_size)
        values, valid = _warp_band(dataset, band, grid)
        rgba = np.zeros((4, grid.height, grid.width), dtype="uint8")
        samples = values[valid]
        categorical = artifact.type == "raster_classification"
        ramp = () if categorical else _RAMPS[palette]
        stretch = None if categorical else _continuous_color(rgba, valid, samples, ramp)
        classes = _categorical_color(rgba, valid, samples, palette) if categorical else ()
        epsg = dataset.crs.to_epsg(confidence_threshold=100)
        descriptor = RasterPreviewDescriptor(
            artifact_id=artifact.id, source_fingerprint=artifact.fingerprint, image_url=image_url,
            coordinates=grid.coordinates, width=grid.width, height=grid.height, band=band, palette=palette,
            color_mode="categorical" if categorical else "continuous",
            source_crs=f"EPSG:{epsg}" if epsg is not None else dataset.crs.to_wkt(),
            source_resolution=dataset.res, display_resolution=(grid.transform.a, -grid.transform.e),
            ramp=ramp, stretch=stretch, classes=classes,
        )
        return RasterPreview(descriptor, _encode_png(rgba, grid))
    except RasterPreviewError:
        raise
    except (OSError, RasterioError, ValueError, OverflowError):
        raise RasterPreviewError("Could not render the registered raster in the display projection.") from None
