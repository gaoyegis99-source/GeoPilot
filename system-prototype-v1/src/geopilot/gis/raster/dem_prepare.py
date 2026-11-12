"""Derive and validate the two pixel windows used to prepare a local DEM."""

import json
from os import PathLike, fspath
from pathlib import Path

from pyproj import CRS
from rasterio.transform import Affine
from rasterio.windows import Window, bounds as window_bounds, transform as window_transform

from geopilot.data.fingerprint import capture_file_state, fingerprint_file
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.data_asset import RasterMetadata
from geopilot.domain.step_io import StepFileRef
from geopilot.gis.raster.extent_expansion import ExpandedWindow, expand_analysis_window
from geopilot.gis.raster.window_io import copy_raster_window
from geopilot.gis.tools.vector_step_files import read_vector
from geopilot.gis.validation.crs_validator import inspect_crs


_Path = str | PathLike[str]


def _source_record(path: str, grid: RasterMetadata) -> dict:
    return {
        "path": path,
        "metadata_fingerprint": fingerprint_file(path, strategy="metadata").digest,
        "grid": grid.model_dump(mode="json"),
    }


def _window_record(window: Window) -> dict[str, int]:
    return {
        "col_offset": int(window.col_off),
        "row_offset": int(window.row_off),
        "width": int(window.width),
        "height": int(window.height),
    }


def _windows_record(source: dict, extent: StepFileRef, radius: int, windows: ExpandedWindow) -> dict:
    return {
        "source": source,
        "analysis_extent": extent.model_dump(mode="json"),
        "radius_pixels": radius,
        "requested_bounds": list(windows.requested_bounds),
        "analysis_window": _window_record(windows.analysis_window),
        "analysis_bounds": list(windows.analysis_bounds),
        "expanded_window": _window_record(windows.expanded_window),
        "expanded_bounds": list(windows.expanded_bounds),
    }


def plan_dem_windows(source: _Path, analysis_extent: StepFileRef, radius_pixels: int) -> dict:
    """Read a polygon extent and derive its two windows on the staged DEM grid."""
    path = fspath(source)
    before = capture_file_state(path)
    grid = read_raster_metadata(path)
    dem_crs = inspect_crs(grid, field="inputs.dem.crs").crs
    extent, _ = read_vector(analysis_extent.model_dump(mode="json"), role="analysis_extent")
    if not extent.crs.to_2d().equals(dem_crs.to_2d(), ignore_axis_order=True):
        raise ValueError("The DEM and analysis extent must use the same horizontal CRS")
    geometry = extent.geometry
    if extent.empty or not geometry.geom_type.isin(("Polygon", "MultiPolygon")).all() or not geometry.is_valid.all():
        raise ValueError("The analysis extent requires valid Polygon or MultiPolygon geometry")
    if geometry.is_empty.any():
        raise ValueError("The analysis extent contains an empty geometry")

    windows = expand_analysis_window(grid, tuple(extent.total_bounds), radius_pixels)
    record = _windows_record(_source_record(path, grid), analysis_extent, int(radius_pixels), windows)
    if capture_file_state(path) != before:
        raise ValueError("The staged DEM changed during window planning")
    return record


def _checked_windows(path: str, grid: RasterMetadata, record: dict) -> ExpandedWindow:
    if not isinstance(record, dict):
        raise ValueError("Expected a DEM windows JSON object")
    source = record.get("source")
    if not isinstance(source, dict) or source.get("path") != path:
        raise ValueError("The windows record does not refer to this staged DEM")
    expected_source = _source_record(path, grid)
    if source != expected_source:
        raise ValueError("The staged DEM changed after window planning")

    extent = StepFileRef.model_validate(record.get("analysis_extent"))
    radius = record["radius_pixels"]
    windows = expand_analysis_window(grid, record["requested_bounds"], radius)
    expected = _windows_record(expected_source, extent, radius, windows)
    # JSON keeps boolean, integer, and floating-point values distinct in this record.
    if json.dumps(record, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True, allow_nan=False):
        raise ValueError("The DEM windows record does not match the derived windows")
    return windows


def _verify_crop(grid: RasterMetadata, cropped: RasterMetadata, window: Window) -> None:
    if cropped.crs is None or grid.crs is None or not CRS.from_user_input(cropped.crs).equals(
        CRS.from_user_input(grid.crs), ignore_axis_order=True,
    ):
        raise ValueError("The prepared DEM CRS differs from the source DEM")
    affine = window_transform(window, Affine(*grid.transform))
    expected = grid.model_copy(update={
        "crs": cropped.crs,
        "width": window.width,
        "height": window.height,
        "transform": tuple(affine)[:6],
        "bounds": window_bounds(Window(0, 0, window.width, window.height), affine),
    })
    if cropped != expected:
        raise ValueError("The prepared DEM grid or band metadata differs from the requested source window")


def prepare_dem(source: _Path, destination: _Path, record: dict) -> RasterMetadata:
    """Copy the recorded neighborhood, then verify the actual destination grid."""
    path = fspath(source)
    before = capture_file_state(path)
    grid = read_raster_metadata(path)
    window = _checked_windows(path, grid, record).expanded_window
    target = Path(destination).expanduser()
    copy_raster_window(path, target, window)
    try:
        cropped = read_raster_metadata(target)
        if capture_file_state(path) != before:
            raise ValueError("The staged DEM changed during window copying")
        _verify_crop(grid, cropped, window)
    except BaseException as error:
        try:
            target.unlink(missing_ok=True)
        except OSError as cleanup:
            error.add_note(f"Could not remove the unverified DEM crop: {cleanup}")
        raise
    return cropped
