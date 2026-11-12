"""Return a terrain raster to the original analysis grid and polygon coverage."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import rasterio
import shapely
from geopandas import GeoDataFrame
from pyproj import CRS
from rasterio.io import DatasetReader, DatasetWriter
from rasterio.transform import Affine
from rasterio.windows import Window, bounds as window_bounds, transform as window_transform

from geopilot.data.fingerprint import capture_file_state
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.data_asset import RasterMetadata
from geopilot.domain.step_io import StepFileRef
from geopilot.gis.raster.dem_prepare import _windows_record
from geopilot.gis.raster.extent_expansion import ExpandedWindow, expand_analysis_window
from geopilot.gis.raster.window_io import _check_destination, _local_source
from geopilot.gis.tools.vector_step_files import read_vector


_Path = str | os.PathLike[str]
_BLOCK_SIZE = 256


def read_analysis_polygons(reference: dict, grid_crs: str) -> GeoDataFrame:
    frame, _ = read_vector(reference, role="analysis_extent")
    if not frame.crs.to_2d().equals(CRS.from_user_input(grid_crs).to_2d(), ignore_axis_order=True):
        raise ValueError("The original analysis extent must use the DEM horizontal CRS")
    geometry = frame.geometry
    if (
        frame.empty or not geometry.geom_type.isin(("Polygon", "MultiPolygon")).all()
        or geometry.is_empty.any() or not geometry.is_valid.all()
    ):
        raise ValueError("The analysis extent requires valid nonempty Polygon or MultiPolygon geometry")
    return frame


def analysis_scope(frame: GeoDataFrame) -> shapely.Geometry:
    scope = shapely.union_all(frame.geometry.to_numpy())
    shapely.prepare(scope)
    return scope


def _checked_analysis(windows: dict, analysis_extent: StepFileRef) -> tuple[RasterMetadata, ExpandedWindow, shapely.Geometry]:
    if not isinstance(windows, dict) or not isinstance(windows.get("source"), dict):
        raise ValueError("Expected a DEM windows record with its source grid")
    source = windows["source"]
    if set(source) != {"path", "metadata_fingerprint", "grid"}:
        raise ValueError("Expected a DEM windows record with its source grid")
    if StepFileRef.model_validate(windows.get("analysis_extent")) != analysis_extent:
        raise ValueError("The original analysis extent reference differs from the windows record")
    grid = RasterMetadata.model_validate(source["grid"])
    if grid.crs is None:
        raise ValueError("The DEM windows record must contain a known CRS")
    frame = read_analysis_polygons(analysis_extent.model_dump(mode="json"), grid.crs)

    radius = windows["radius_pixels"]
    expanded = expand_analysis_window(grid, tuple(frame.total_bounds), radius)
    expected = _windows_record({**source, "grid": grid.model_dump(mode="json")}, analysis_extent, radius, expanded)
    if json.dumps(windows, sort_keys=True) != json.dumps(expected, sort_keys=True):
        raise ValueError("The DEM windows record does not match the original analysis extent and source grid")
    return grid, expanded, analysis_scope(frame)


def _check_terrain_grid(dataset: DatasetReader, grid: RasterMetadata, expanded: Window) -> None:
    if dataset.crs is None or not CRS.from_user_input(dataset.crs).equals(
        CRS.from_user_input(grid.crs), ignore_axis_order=True,
    ):
        raise ValueError("The expanded terrain CRS does not match the recorded DEM")
    affine = window_transform(expanded, Affine(*grid.transform))
    if (dataset.width, dataset.height) != (expanded.width, expanded.height) or dataset.transform != affine:
        raise ValueError("The expanded terrain grid does not match the recorded neighborhood window")
    if dataset.count != 1:
        raise ValueError("The expanded terrain must contain one value band")


def pixel_centers_covered(scope: shapely.Geometry, grid_affine: Affine, window: Window) -> np.ndarray:
    """Test the original north-up grid's centers, including polygon boundaries."""
    columns = window.col_off + np.arange(window.width)
    rows = window.row_off + np.arange(window.height)
    x = grid_affine.c + grid_affine.a * (columns + 0.5)
    y = grid_affine.f + grid_affine.e * (rows + 0.5)
    return shapely.intersects_xy(scope, x[np.newaxis, :], y[:, np.newaxis])


def _copy_covered_pixels(
    dataset: DatasetReader, output: DatasetWriter, crop: Window, scope: shapely.Geometry, *,
    grid_affine: Affine, analysis: Window,
) -> None:
    for _, tile in output.block_windows(1):
        source_tile = Window(crop.col_off + tile.col_off, crop.row_off + tile.row_off, tile.width, tile.height)
        samples = dataset.read(1, window=source_tile, masked=True)
        # Use original-grid indexes so shifting the affine's origin cannot
        # round a center to the other side of an exact polygon boundary.
        original_tile = Window(analysis.col_off + tile.col_off, analysis.row_off + tile.row_off, tile.width, tile.height)
        covered = pixel_centers_covered(scope, grid_affine, original_tile)
        valid = covered & ~np.ma.getmaskarray(samples) & np.isfinite(samples.data)
        values = samples.data.copy()
        if dataset.nodata is not None:
            values[~valid] = dataset.nodata
        output.write(values, 1, window=tile)
        output.write_mask(valid.astype(np.uint8) * 255, window=tile)


def clip_terrain_to_analysis_extent(
    source: _Path, destination: _Path, *, windows: dict, analysis_extent: StepFileRef,
) -> RasterMetadata:
    """Copy the original analysis cells and mask centers outside the analysis polygons."""
    path = _local_source(source)
    target = Path(destination).expanduser()
    _check_destination(target)
    terrain_state = capture_file_state(path)
    extent_state = capture_file_state(analysis_extent.path)
    grid, extent_windows, scope = _checked_analysis(windows, analysis_extent)
    analysis, expanded = extent_windows.analysis_window, extent_windows.expanded_window
    crop = Window(analysis.col_off - expanded.col_off, analysis.row_off - expanded.row_off, analysis.width, analysis.height)
    grid_affine = Affine(*grid.transform)
    affine = window_transform(analysis, grid_affine)
    source_metadata = read_raster_metadata(path)

    with rasterio.open(path, driver="GTiff") as dataset:
        _check_terrain_grid(dataset, grid, expanded)
        with TemporaryDirectory(prefix=".terrain-clip-", dir=target.parent, ignore_cleanup_errors=True) as directory:
            staged = Path(directory) / "terrain.tif"
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
                staged, "w", driver="GTiff", width=crop.width, height=crop.height, count=1, dtype=dataset.dtypes[0],
                crs=dataset.crs, transform=affine, nodata=dataset.nodata, tiled=True,
                blockxsize=_BLOCK_SIZE, blockysize=_BLOCK_SIZE, compress="deflate", BIGTIFF="IF_SAFER",
            ) as output:
                output.units = dataset.units
                output.descriptions = dataset.descriptions
                output.scales = dataset.scales
                output.offsets = dataset.offsets
                output.update_tags(**{key: value for key, value in dataset.tags().items() if not key.startswith("STATISTICS_")})
                _copy_covered_pixels(dataset, output, crop, scope, grid_affine=grid_affine, analysis=analysis)
            metadata = read_raster_metadata(staged)
            expected = source_metadata.model_copy(update={
                "width": crop.width, "height": crop.height, "transform": tuple(affine)[:6],
                "bounds": window_bounds(Window(0, 0, crop.width, crop.height), affine),
            })
            if metadata != expected or any(file != staged for file in staged.parent.iterdir()):
                raise ValueError("The clipped terrain grid or metadata differs from the recorded analysis window")
            if capture_file_state(path) != terrain_state or capture_file_state(analysis_extent.path) != extent_state:
                raise ValueError("A terrain clip input changed while copying its cells")
            os.link(staged, target)
    return metadata
