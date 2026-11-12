"""Align two continuous NDVI rasters on the first raster's pixel grid."""

import json
import math
import os
import warnings
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import rasterio
import shapely
from pyproj import CRS
from rasterio.enums import MaskFlags, Resampling
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import DatasetReader, DatasetWriter
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from geopilot.data.errors import SourceReadError
from geopilot.data.fingerprint import capture_file_state
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.data.vector_reader import local_ogr_path
from geopilot.domain.data_asset import RasterMetadata
from geopilot.execution.output_staging import temporary_output_directory
from geopilot.gis.raster.clip_back import analysis_scope
from geopilot.gis.raster.window_io import _check_destination, _local_source
from geopilot.gis.tools.vector_step_files import read_vector
from geopilot.gis.validation.crs_validator import CRSValidationError, inspect_crs, validate_crs_operation
from geopilot.remote_sensing.indices import NDVI_MAXIMUM, NDVI_MINIMUM


ALIGNMENT_DOMAIN = "GEOPILOT_NDVI_ALIGNMENT"
ALIGNED_NDVI_BANDS = ("first_ndvi", "second_ndvi")
_BLOCK_SIZE = 256
_WARP_TOLERANCE_PIXELS = 1e-6
_Path = str | os.PathLike[str]
_Resampling = Literal["nearest", "bilinear"]
_Bounds = tuple[float, float, float, float]
_Range = tuple[float, float] | None


class AlignmentInputError(ValueError):
    code = "NDVI_ALIGNMENT_INPUT_INVALID"

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class AlignmentGrid:
    crs: str
    transform: tuple[float, float, float, float, float, float]
    resolution: tuple[float, float]
    width: int
    height: int
    bounds: _Bounds
    nodata: int | float | str | None


@dataclass(frozen=True, slots=True)
class AlignmentSummary:
    first_grid: AlignmentGrid
    second_grid: AlignmentGrid
    target_grid: AlignmentGrid
    resampling: _Resampling
    warped: bool
    analysis_bounds: _Bounds
    analysis_pixels: int
    first_valid_pixels: int
    second_valid_pixels: int
    common_valid_pixels: int
    first_range: _Range
    second_range: _Range


@dataclass(frozen=True, slots=True)
class AlignmentResult:
    metadata: RasterMetadata
    summary: AlignmentSummary


def _open_referenced_tiff(path: Path, *, role: str) -> DatasetReader:
    with warnings.catch_warnings():
        warnings.simplefilter("error", NotGeoreferencedWarning)
        try:
            return rasterio.open(path, driver="GTiff")
        except NotGeoreferencedWarning as error:
            raise AlignmentInputError(f"{role}.transform", "The raster requires a stored affine geotransform") from error


def _inspect_ndvi(dataset: DatasetReader, path: Path, *, role: str) -> RasterMetadata:
    if (
        dataset.dtypes != ("float64",) or dataset.descriptions != ("ndvi",) or dataset.units != ("1",)
        or dataset.scales != (1.0,) or dataset.offsets != (0.0,)
    ):
        raise AlignmentInputError(role, "Expected a single Float64 ndvi band with unit 1 and identity scaling")
    transform = dataset.transform
    if transform.is_identity and (dataset.gcps[0] or dataset.rpcs is not None):
        raise AlignmentInputError(f"{role}.transform", "The NDVI raster requires an affine grid")
    if not all(map(math.isfinite, transform)) or transform.is_degenerate:
        raise AlignmentInputError(f"{role}.transform", "The NDVI affine transform must be finite and invertible")
    metadata = read_raster_metadata(path)
    try:
        inspect_crs(metadata, field=f"{role}.crs")
    except CRSValidationError as error:
        raise AlignmentInputError(error.field, error.message) from error
    return metadata


def _grid(metadata: RasterMetadata) -> AlignmentGrid:
    if metadata.crs is None or metadata.bounds is None:
        raise ValueError("Alignment requires referenced raster bounds")
    return AlignmentGrid(
        crs=metadata.crs, transform=metadata.transform, resolution=metadata.resolution,
        width=metadata.width, height=metadata.height, bounds=metadata.bounds, nodata=metadata.nodata[0],
    )


def _read_scope(path: Path, layer: str | None, crs: str) -> shapely.Geometry:
    try:
        frame, _ = read_vector({"path": os.fspath(path), "layer": layer}, role="analysis_extent")
        geometry = frame.geometry
        if (
            frame.empty or not geometry.geom_type.isin(("Polygon", "MultiPolygon")).all()
            or geometry.is_empty.any() or not geometry.is_valid.all()
        ):
            raise ValueError("Expected valid nonempty Polygon or MultiPolygon geometry")
        validate_crs_operation(frame.crs, crs, operation="reproject")
        if not frame.crs.equals(CRS.from_user_input(crs), ignore_axis_order=True):
            frame = frame.to_crs(crs)
            if not frame.geometry.is_valid.all():
                raise ValueError("The reprojected analysis extent must contain valid polygons")
        return analysis_scope(frame)
    except (ValueError, RuntimeError) as error:
        raise AlignmentInputError("analysis_extent", str(error)) from error


def _read_values(dataset: DatasetReader | WarpedVRT, tile: Window) -> tuple[np.ndarray, np.ndarray]:
    samples = dataset.read(1, window=tile, masked=True)
    values = samples.data
    valid = ~np.ma.getmaskarray(samples) & np.isfinite(values) & (values >= NDVI_MINIMUM) & (values <= NDVI_MAXIMUM)
    nodata = dataset.nodata
    if nodata is not None and math.isfinite(nodata):
        valid &= values != nodata
    return values, valid


def _create_tiff(path: Path, grid: DatasetReader, *, count: int) -> DatasetWriter:
    return rasterio.open(
        path, "w", driver="GTiff", width=grid.width, height=grid.height, count=count, dtype="float64",
        crs=grid.crs, transform=grid.transform, nodata=np.nan, tiled=True,
        blockxsize=_BLOCK_SIZE, blockysize=_BLOCK_SIZE, compress="deflate", BIGTIFF="IF_SAFER",
    )


def _clean_for_warp(source: DatasetReader, path: Path) -> None:
    # Serialize the actual band mask, numeric NoData, and range exclusions into
    # one NaN sentinel before the warp kernel can use any of those samples.
    with _create_tiff(path, source, count=1) as clean:
        for _, tile in clean.block_windows(1):
            values, valid = _read_values(source, tile)
            clean.write(np.where(valid, values, np.nan), 1, window=tile)


def _extend_range(bounds: _Range, values: np.ndarray) -> tuple[float, float]:
    minimum, maximum = float(values.min()), float(values.max())
    if bounds is None:
        return minimum, maximum
    return min(bounds[0], minimum), max(bounds[1], maximum)


def _write_blocks(
    first: DatasetReader, second: DatasetReader | WarpedVRT, output: DatasetWriter, scope: shapely.Geometry | None,
) -> tuple[int, int, int, int, _Range, _Range]:
    analysis_pixels = first_valid_pixels = second_valid_pixels = common_valid_pixels = 0
    first_range = second_range = None
    for _, tile in output.block_windows(1):
        first_values, first_valid = _read_values(first, tile)
        second_values, second_valid = _read_values(second, tile)
        if scope is None:
            analysis_pixels += first_values.size
        else:
            columns = tile.col_off + np.arange(tile.width)[None, :] + 0.5
            rows = tile.row_off + np.arange(tile.height)[:, None] + 0.5
            x, y = first.transform * (columns, rows)
            covered = shapely.intersects_xy(scope, x, y)
            analysis_pixels += int(np.count_nonzero(covered))
            first_valid &= covered
            second_valid &= covered

        common = first_valid & second_valid
        first_valid_pixels += int(np.count_nonzero(first_valid))
        second_valid_pixels += int(np.count_nonzero(second_valid))
        common_count = int(np.count_nonzero(common))
        common_valid_pixels += common_count
        if common_count:
            first_range = _extend_range(first_range, first_values[common])
            second_range = _extend_range(second_range, second_values[common])
        output.write(np.where(common, first_values, np.nan), 1, window=tile)
        output.write(np.where(common, second_values, np.nan), 2, window=tile)
        output.write_mask(common, window=tile)
    return analysis_pixels, first_valid_pixels, second_valid_pixels, common_valid_pixels, first_range, second_range


def align_ndvi(
    first: _Path, second: _Path, destination: _Path, *, resampling: _Resampling,
    analysis_extent: str | Path | None = None, analysis_layer: str | None = None,
) -> AlignmentResult:
    """Write two NDVI bands and their common valid mask on the full first grid."""
    if not isinstance(resampling, str) or resampling not in ("nearest", "bilinear"):
        raise AlignmentInputError("resampling", "Expected confirmed nearest or bilinear resampling")
    if analysis_layer is not None and (
        analysis_extent is None or not isinstance(analysis_layer, str) or not analysis_layer.strip()
    ):
        raise AlignmentInputError("analysis_layer", "An analysis layer requires a local analysis extent and a layer name")
    target = Path(destination).expanduser()
    _check_destination(target)
    first_path, second_path = _local_source(first), _local_source(second)
    extent_path = None
    if analysis_extent is not None:
        try:
            extent_path = local_ogr_path(Path(analysis_extent).expanduser())
        except (SourceReadError, TypeError, ValueError) as error:
            raise AlignmentInputError("analysis_extent", str(error)) from error
    inputs = {"first": first_path, "second": second_path}
    if extent_path is not None:
        inputs["analysis_extent"] = extent_path
    states = {role: capture_file_state(path) for role, path in inputs.items()}

    with ExitStack() as stack:
        first_data = stack.enter_context(_open_referenced_tiff(first_path, role="first"))
        second_data = stack.enter_context(_open_referenced_tiff(second_path, role="second"))
        first_metadata = _inspect_ndvi(first_data, first_path, role="first")
        second_metadata = _inspect_ndvi(second_data, second_path, role="second")
        first_grid, second_grid = _grid(first_metadata), _grid(second_metadata)
        scope = _read_scope(extent_path, analysis_layer, first_grid.crs) if extent_path is not None else None
        expected = first_metadata.model_copy(update={
            "band_count": 2, "band_dtypes": ("float64", "float64"), "band_names": ALIGNED_NDVI_BANDS,
            "band_units": ("1", "1"), "nodata": ("nan", "nan"),
        })
        directory = Path(stack.enter_context(temporary_output_directory(target, prefix=".ndvi-alignment-")))
        staged = directory / "output" / "aligned.tif"
        staged.parent.mkdir()
        warped = (first_data.crs, first_data.transform, first_data.shape) != (
            second_data.crs, second_data.transform, second_data.shape,
        )
        aligned_second = second_data
        if warped:
            clean_path = directory / "second.tif"
            _clean_for_warp(second_data, clean_path)
            clean = stack.enter_context(_open_referenced_tiff(clean_path, role="second"))
            aligned_second = stack.enter_context(WarpedVRT(
                clean, crs=first_data.crs, transform=first_data.transform, width=first_data.width, height=first_data.height,
                resampling=Resampling[resampling], src_nodata=np.nan, nodata=np.nan, dtype="float64",
                tolerance=_WARP_TOLERANCE_PIXELS, warp_mem_limit=32,
            ))

        with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), _create_tiff(staged, first_data, count=2) as output:
            output.descriptions = ALIGNED_NDVI_BANDS
            output.units = ("1", "1")
            if area_or_point := first_data.tags().get("AREA_OR_POINT"):
                output.update_tags(AREA_OR_POINT=area_or_point)
            analysis, first_valid, second_valid, common, first_range, second_range = _write_blocks(first_data, aligned_second, output, scope)
            summary = AlignmentSummary(
                first_grid=first_grid, second_grid=second_grid, target_grid=_grid(expected), resampling=resampling,
                warped=warped, analysis_bounds=first_grid.bounds if scope is None else scope.bounds, analysis_pixels=analysis,
                first_valid_pixels=first_valid, second_valid_pixels=second_valid, common_valid_pixels=common,
                first_range=first_range, second_range=second_range,
            )
            output.update_tags(ns=ALIGNMENT_DOMAIN, SUMMARY=json.dumps(asdict(summary), allow_nan=False))

        with _open_referenced_tiff(staged, role="destination") as reopened:
            internal_mask = all(MaskFlags.per_dataset in flags for flags in reopened.mask_flag_enums)
        metadata = read_raster_metadata(staged)
        if metadata != expected or not internal_mask or any(path != staged for path in staged.parent.iterdir()):
            raise AlignmentInputError("destination", "Alignment requires a single same-grid GeoTIFF with an internal common mask")
        for role, path in inputs.items():
            if capture_file_state(path) != states[role]:
                raise AlignmentInputError(role, "An alignment input changed during processing")
        _check_destination(target)
        os.link(staged, target)
    return AlignmentResult(metadata, summary)
