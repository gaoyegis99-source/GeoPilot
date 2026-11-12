"""Compare fixed-density projected area on three original classification inputs."""

from collections import Counter, defaultdict
import json
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import Window

from geopilot.gis.raster.area_stats import _projected_areas


BUNDLE = Path("benchmarks/raster/area-fixtures")
SEGMENTS = 8
CASES = {"dem-m": (48, 48), "dem-l": (4144, 4144), "remote-l": (0, 0)}


def exterior_edges(rows, columns, classes):
    edges = defaultdict(Counter)
    for row, col, label in zip(rows.tolist(), columns.tolist(), classes.tolist(), strict=True):
        ring = ((row, col), (row, col + 1), (row + 1, col + 1), (row + 1, col))
        for start, end in zip(ring, (*ring[1:], ring[0]), strict=True):
            edge = tuple(sorted((start, end)))
            edges[label][edge] += 1 if start < end else -1
    return {label: {edge: sign for edge, sign in values.items() if sign} for label, values in edges.items()}


def signed_projected_edges(edges, affine, transformer):
    values = np.asarray(list(edges), dtype=np.float64)
    along = np.linspace(0, 1, SEGMENTS + 1)
    coordinates = values[:, 0, :, None] + (values[:, 1] - values[:, 0])[:, :, None] * along
    r, c = coordinates[:, 0], coordinates[:, 1]
    x, y = transformer.transform(*(affine * (c, r)), errcheck=True)
    x, y = x - x.flat[0], y - y.flat[0]
    directed = (x[:, :-1] * y[:, 1:] - x[:, 1:] * y[:, :-1]).sum(axis=1) * 0.5
    return float(abs(np.dot(directed, list(edges.values()))))


for case, (xoff, yoff) in CASES.items():
    with rasterio.open(BUNDLE / (case + ".tif")) as raster:
        window = Window(xoff, yoff, 32, 32)
        classification = raster.read(1, window=window, masked=True)
        transform = raster.window_transform(window)
        projector = Transformer.from_crs(
            raster.crs, "EPSG:6933", always_xy=True, allow_ballpark=False, only_best=True,
        )
    rows, cols = np.nonzero(~np.ma.getmaskarray(classification) & (classification.data != 0))
    classes = classification.data[rows, cols]
    current = _projected_areas(rows, cols, transform, projector, SEGMENTS)
    group_edges = exterior_edges(rows, cols, classes)
    errors = {
        label: abs(signed_projected_edges(edges, transform, projector) - float(current[classes == label].sum()))
        for label, edges in group_edges.items()
    }
    assert all(error <= 1e-5 for error in errors.values())
    print(json.dumps({
        "case": case, "window_offset": [xoff, yoff], "shape": [32, 32],
        "selected_cells": rows.size,
        "original_points_at_eight_segments": rows.size * 4 * SEGMENTS,
        "projected_boundary_points_at_eight_segments": sum(len(edges) for edges in group_edges.values()) * (SEGMENTS + 1),
        "absolute_area_error_by_class_m2": errors,
    }), flush=True)

# Estimate work at the same fixed densification with 256x256 tile boundaries
# deliberately counted on both sides. This does not run the area calculation.
for case in CASES:
    cells = exposed = 0
    with rasterio.open(BUNDLE / (case + ".tif")) as raster:
        for _, window in raster.block_windows(1):
            block = raster.read(1, window=window, masked=True)
            active = ~np.ma.getmaskarray(block) & (block.data != 0)
            labels = np.where(active, block.data, 0)
            cells += np.count_nonzero(active)
            exposed += np.count_nonzero(active[[0, -1], :]) + np.count_nonzero(active[:, [0, -1]])
            for before, after, valid_before, valid_after in (
                (labels[:-1, :], labels[1:, :], active[:-1, :], active[1:, :]),
                (labels[:, :-1], labels[:, 1:], active[:, :-1], active[:, 1:]),
            ):
                different = before != after
                exposed += np.count_nonzero(different & valid_before) + np.count_nonzero(different & valid_after)
    print(json.dumps({
        "case": case, "tile_shape": [256, 256], "selected_cells": int(cells),
        "original_points_at_eight_segments": int(cells * 4 * SEGMENTS),
        "projected_boundary_points_at_eight_segments": int(exposed * (SEGMENTS + 1)),
    }), flush=True)
