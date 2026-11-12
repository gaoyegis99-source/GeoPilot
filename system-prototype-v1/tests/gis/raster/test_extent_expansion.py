import math
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine
from rasterio.windows import Window

from geopilot.data.raster_reader import read_raster_metadata
from geopilot.gis.raster.extent_expansion import expand_analysis_window
from geopilot.gis.raster.window_io import read_raster_window


DEM = Path(__file__).resolve().parents[2] / "fixtures" / "raster" / "dem-plane.tif"
ANALYSIS = (500020.0, 4400020.0, 500050.0, 4400050.0)
SOURCE_BOUNDS = (500000.0, 4400000.0, 500070.0, 4400070.0)


@pytest.fixture
def dem_grid():
    return read_raster_metadata(DEM)


def _write_grid(path, transform):
    with rasterio.open(
        path, "w", driver="GTiff", width=8, height=8, count=1,
        dtype="float32", crs="EPSG:32650", transform=transform, nodata=-9999,
    ) as dataset:
        dataset.write(np.arange(64, dtype="float32").reshape(8, 8), 1)


def test_golden_dem_expands_by_one_pixel_with_a_valid_neighborhood(dem_grid):
    observed = expand_analysis_window(dem_grid, ANALYSIS, radius_pixels=1)

    assert observed.requested_bounds == ANALYSIS
    assert observed.analysis_window == Window(2, 2, 3, 3)
    assert observed.analysis_bounds == ANALYSIS
    assert observed.expanded_window == Window(1, 1, 5, 5)
    assert observed.expanded_bounds == (500010.0, 4400010.0, 500060.0, 4400060.0)

    neighborhood = read_raster_window(DEM, observed.expanded_window)
    assert neighborhood.data.count() == 25
    assert neighborhood.transform == Affine(10, 0, 500010, 0, -10, 4400060)
    np.testing.assert_array_equal(neighborhood.data.data[0], [[110, 120, 130, 140, 150]] * 5)


def test_off_grid_extent_rounds_outward_for_rectangular_pixels(tmp_path):
    path = tmp_path / "rectangular-grid.tif"
    _write_grid(path, Affine(6, 0, 500000, 0, -14, 4400070))
    requested = (500014.0, 4399997.0, 500027.0, 4400038.0)

    result = expand_analysis_window(read_raster_metadata(path), requested, radius_pixels=2)

    assert result.requested_bounds == requested
    assert result.analysis_window == Window(2, 2, 3, 4)
    assert result.analysis_bounds == (500012.0, 4399986.0, 500030.0, 4400042.0)
    assert result.expanded_window == Window(0, 0, 7, 8)
    assert result.expanded_bounds == (500000.0, 4399958.0, 500042.0, 4400070.0)
    with rasterio.open(path) as dataset:
        assert dataset.window_bounds(result.analysis_window) == result.analysis_bounds
        assert dataset.window_bounds(result.expanded_window) == result.expanded_bounds


def test_aligned_decimal_pixel_edges_do_not_gain_an_extra_pixel(tmp_path):
    path = tmp_path / "decimal-grid.tif"
    _write_grid(path, Affine(0.1, 0, 0.1, 0, -0.2, 0.7))
    analysis = Window(1, 2, 1, 3)
    with rasterio.open(path) as dataset:
        requested = dataset.window_bounds(analysis)
        expected_expanded = dataset.window_bounds(Window(0, 1, 3, 5))

    result = expand_analysis_window(read_raster_metadata(path), requested, radius_pixels=1)

    assert result.analysis_window == analysis
    assert result.analysis_bounds == requested
    assert result.expanded_window == Window(0, 1, 3, 5)
    assert result.expanded_bounds == expected_expanded


def test_nonzero_overlap_next_to_an_edge_is_never_rounded_away(dem_grid):
    left, bottom, right, top = ANALYSIS
    requested = (
        math.nextafter(left, -math.inf), math.nextafter(bottom, -math.inf),
        math.nextafter(right, math.inf), math.nextafter(top, math.inf),
    )

    result = expand_analysis_window(dem_grid, requested, radius_pixels=1)

    assert result.requested_bounds == requested
    assert result.analysis_window == Window(1, 1, 5, 5)
    assert result.expanded_window == Window(0, 0, 7, 7)
    assert result.expanded_bounds == SOURCE_BOUNDS


def test_radius_zero_accepts_the_complete_source_grid(dem_grid):
    result = expand_analysis_window(dem_grid, SOURCE_BOUNDS, radius_pixels=0)

    assert result.analysis_window == result.expanded_window == Window(0, 0, 7, 7)
    assert result.analysis_bounds == result.expanded_bounds == SOURCE_BOUNDS


@pytest.mark.parametrize("bounds", [
    (500000.0, 4400020.0, 500050.0, 4400050.0),
    (500020.0, 4400000.0, 500050.0, 4400050.0),
    (500020.0, 4400020.0, 500070.0, 4400050.0),
    (500020.0, 4400020.0, 500050.0, 4400070.0),
])
def test_refuses_insufficient_neighborhood_on_every_source_side(dem_grid, bounds):
    with pytest.raises(ValueError, match="neighborhood"):
        expand_analysis_window(dem_grid, bounds, radius_pixels=1)


def test_refuses_a_larger_kernel_when_source_only_covers_a_smaller_one(dem_grid):
    assert expand_analysis_window(dem_grid, ANALYSIS, radius_pixels=2).expanded_bounds == SOURCE_BOUNDS
    with pytest.raises(ValueError, match="3-pixel neighborhood"):
        expand_analysis_window(dem_grid, ANALYSIS, radius_pixels=3)


def test_refuses_analysis_extent_beyond_source_even_for_radius_zero(dem_grid):
    west, south, east, north = SOURCE_BOUNDS
    with pytest.raises(ValueError, match="[Aa]nalysis bounds"):
        expand_analysis_window(dem_grid, (math.nextafter(west, -math.inf), south, east, north), radius_pixels=0)


@pytest.mark.parametrize("bounds", [
    (500020, 4400020, 500020, 4400050),
    (500020, 4400050, 500050, 4400020),
    (float("nan"), 4400020, 500050, 4400050),
    (500020, 4400020, 500050, float("inf")),
])
def test_refuses_nonfinite_or_empty_bounds(dem_grid, bounds):
    with pytest.raises(ValueError, match="[Aa]nalysis bounds"):
        expand_analysis_window(dem_grid, bounds, radius_pixels=1)


@pytest.mark.parametrize("radius", [-1, 1.5, 1.0, True])
def test_refuses_noninteger_or_negative_kernel_radius(dem_grid, radius):
    with pytest.raises(ValueError, match="[Rr]adius"):
        expand_analysis_window(dem_grid, ANALYSIS, radius_pixels=radius)


@pytest.mark.parametrize("transform", [
    Affine(10, 2, 500000, 1, -10, 4400070),
    Affine(10, 0, 500000, 0, 10, 4400000),
    Affine(-10, 0, 500080, 0, -10, 4400070),
])
def test_refuses_rotated_sheared_and_reversed_affines(tmp_path, transform):
    path = tmp_path / "unsupported-grid.tif"
    _write_grid(path, transform)

    with pytest.raises(ValueError, match="north-up"):
        expand_analysis_window(read_raster_metadata(path), ANALYSIS, radius_pixels=1)
