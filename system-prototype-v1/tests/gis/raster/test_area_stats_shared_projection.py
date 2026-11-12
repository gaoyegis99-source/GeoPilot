import numpy as np
import pytest
from pyproj import Transformer
from pyproj.exceptions import ProjError
from rasterio.transform import Affine
from rasterio.windows import Window

from geopilot.gis.raster.area_stats import (
    _BatchLayout, _converged_areas, _projected_areas, _sum_projected_tile,
)


class _CountingProjection:
    def __init__(self):
        self._transformer = Transformer.from_crs(32650, 6933, always_xy=True, allow_ballpark=False, only_best=True)
        self.calls = []

    def transform(self, x, y, *, errcheck):
        self.calls.append(x.size)
        return self._transformer.transform(x, y, errcheck=errcheck)


def test_dense_batch_projects_each_dyadic_vertex_once_and_preserves_cell_areas():
    rows, cols = np.indices((16, 16))
    rows, cols = rows.ravel() + 240, cols.ravel() + 752
    affine = Affine(10, 2, 500000, 1, -10, 4500000)
    projector = _CountingProjection()

    areas, segments = _converged_areas(rows, cols, affine, projector)
    assert segments == 8
    assert projector.calls == [833, 1088, 2176]
    reference = _projected_areas(rows, cols, affine, _CountingProjection(), segments)
    np.testing.assert_array_equal(areas, reference)


def test_sparse_coarse_sheared_batch_keeps_high_density_and_ring_order():
    rows, cols = np.array([1, 0, 0, 1, 0]), np.array([2, 0, 2, 0, 1])
    affine = Affine(90000, 15000, 420000, 20000, -95000, 4550000)
    projector = _CountingProjection()

    areas, segments = _converged_areas(rows, cols, affine, projector)
    assert segments == 256 and len(projector.calls) == 8
    reference = _projected_areas(rows, cols, affine, _CountingProjection(), segments)
    np.testing.assert_array_equal(areas, reference)


@pytest.mark.parametrize("projection_error", [False, True])
def test_new_vertices_keep_projection_and_nonfinite_area_errors(projection_error):
    class _Projection:
        calls = 0

        def transform(self, x, y, *, errcheck):
            assert errcheck
            self.calls += 1
            at_eighth = np.remainder(x, 0.25) == 0.125
            if projection_error and at_eighth.any():
                raise ProjError("Projection rejected a new vertex")
            return np.where(at_eighth, np.nan, x), y

    projector = _Projection()
    expected = "Could not project" if projection_error else "no finite positive area"
    with pytest.raises(ValueError, match=expected):
        _converged_areas(np.array([0]), np.array([0]), Affine.identity(), projector)
    assert projector.calls == 3


def test_tile_reuses_only_consecutive_equivalent_layouts(monkeypatch):
    data = np.full((3, 256), 7, dtype=np.uint8)
    selected = np.ones(data.shape, dtype=bool)
    selected[-1, 0] = False
    tile = Window(512, 768, 256, 3)
    affine = Affine(10, 2, 500000, 1, -10, 4500000)
    rows, cols = np.nonzero(selected)
    rows += int(tile.row_off)
    cols += int(tile.col_off)
    expected = np.zeros(256)
    projector = _CountingProjection()
    for start in range(0, len(rows), 256):
        batch = slice(start, start + 256)
        areas, segments = _converged_areas(rows[batch], cols[batch], affine, projector)
        assert segments == 8
        expected += np.bincount(np.full(areas.size, 7), weights=areas, minlength=256)
    original = np.unique
    calls = []

    def count_unique(values, **options):
        calls.append(values.shape)
        return original(values, **options)

    monkeypatch.setattr(np, "unique", count_unique)
    result = np.zeros(256)
    assert _sum_projected_tile(data, selected, tile, affine, _CountingProjection(), result) == 8
    assert calls == [(256, 8), (256, 16), (256, 32), (255, 8), (255, 16), (255, 32)]
    np.testing.assert_array_equal(result, expected)


def test_layout_keeps_only_the_first_three_refinement_levels():
    rows, cols = np.array([1, 0, 0, 1, 0]), np.array([2, 0, 2, 0, 1])
    affine = Affine(90000, 15000, 420000, 20000, -95000, 4550000)
    layout = _BatchLayout(rows, cols)

    cached, cached_segments = _converged_areas(rows, cols, affine, _CountingProjection(), layout=layout)
    reference, reference_segments = _converged_areas(rows, cols, affine, _CountingProjection())

    assert cached_segments == reference_segments == 256
    assert set(layout._levels) == {2, 4, 8}
    np.testing.assert_array_equal(cached, reference)
