"""Independent analytic checks for the P13-01 DEM and optical fixtures."""

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import Affine
from rasterio.windows import Window

from geopilot.data.fingerprint import capture_file_state


PHASES = ("increase", "decrease", "stable", "first_cloud", "second_cloud", "first_fill")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_file(directory, name):
    require(isinstance(name, str) and Path(name).name == name and name not in {"", ".", ".."},
            f"Expected one local filename, got {name!r}")
    path = directory / name
    require(path.is_file() and not path.is_symlink() and len(capture_file_state(path)) == 1,
            f"Expected self-contained regular file: {path}")
    return path


@dataclass(frozen=True)
class Grid:
    crs: str
    shape: tuple[int, int]
    affine: Affine

    def window(self, window):
        return Grid(self.crs, (int(window.height), int(window.width)), self.affine * Affine.translation(
            window.col_off, window.row_off,
        ))


@dataclass(frozen=True)
class Fixture:
    family: str
    tier: str
    directory: Path
    manifest: dict
    files: dict[str, Path]
    expected: dict[str, dict]
    manifest_sha256: str

    @property
    def reference(self):
        return self.manifest["reference"]

    def grid(self, role):
        names = {"first_product": "landsat", "second_product": "sentinel", "native_quality_reference": "native_scl"}
        record = self.reference if self.family == "raster" else self.reference["grids"][names[role]]
        return Grid(self.reference["crs"], tuple(record["shape"]), Affine(*record["transform"]))

    def snapshot(self):
        require(sha256(source_file(self.directory, "manifest.json")) == self.manifest_sha256, "Fixture manifest changed")
        require({item.name for item in self.directory.iterdir()} == {"manifest.json", *(path.name for path in self.files.values())},
                "Fixture directory contains undeclared members")
        verified = {}
        for role, entry in self.expected.items():
            path = source_file(self.directory, entry["path"])
            before = capture_file_state(path)
            observed = {"path": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
            require(observed == {key: entry[key] for key in observed} and before == capture_file_state(path),
                    f"Fixture fingerprint differs for {role}: {path}")
            verified[role] = observed
        return verified


def load_fixture(directory, family, tier):
    require(not directory.is_symlink(), f"Fixture directory is a symlink: {directory}")
    directory = directory.resolve(strict=True)
    path = source_file(directory, "manifest.json")
    data = path.read_bytes()
    manifest = json.loads(data)
    require((manifest["version"], manifest["family"], manifest["tier"]) == (1, family, tier),
            "Fixture version, family or tier differs")
    roles = {"dem", "analysis_extent"} if family == "raster" else {
        "first_product", "second_product", "analysis_extent", "native_quality_reference",
    }
    expected = {entry["role"]: entry for entry in manifest["files"]}
    files = {role: source_file(directory, entry["path"]) for role, entry in expected.items()}
    require(expected.keys() == roles and len(manifest["files"]) == len(roles)
            and len(set(files.values())) == len(roles), "Fixture has missing or duplicate roles or paths")
    return Fixture(family, tier, directory, manifest, files, expected, hashlib.sha256(data).hexdigest())


def axes(window):
    return (
        np.arange(int(window.row_off), int(window.row_off + window.height))[:, None],
        np.arange(int(window.col_off), int(window.col_off + window.width))[None, :],
    )


def dem_values(window, *, period, side, offset=0, terrain=None):
    rows, cols = axes(window)
    rows, cols = rows + offset, cols + offset
    if terrain is None:
        holes = (rows % period == period // 2) & (cols % period == period // 2)
        return np.broadcast_to(100 + 10 * cols, holes.shape), ~holes
    holes = (np.abs(rows % period - period // 2) <= 1) & (np.abs(cols % period - period // 2) <= 1)
    inside = (rows > 0) & (rows < side - 1) & (cols > 0) & (cols < side - 1)
    return np.full(holes.shape, terrain), inside & ~holes


def optical_values(window, *, role, landsat_side, stage, offset, first_pixel, second_pixel, scl_pixel):
    rows, cols = axes(window)
    if role == "first_product":
        phase = np.broadcast_to(cols % 6, (int(window.height), int(window.width)))
        clear = (phase != 3) & (phase != 5)
        red = 10000 * 0.0000275 - 0.2
        nir = np.where(phase == 1, 20000, 10000) * 0.0000275 - 0.2
    else:
        x = (cols + 0.5) * second_pixel
        y = (rows + 0.5) * second_pixel
        coarse_cols = np.floor((x - offset) / first_pixel)
        coarse_rows = np.floor((y - offset) / first_pixel)
        scl_center = (np.floor(x / scl_pixel) + 0.5) * scl_pixel
        scl_phase = np.floor((scl_center - offset) / first_pixel) % 6
        clear = (coarse_rows >= 0) & (coarse_rows < landsat_side) & (coarse_cols >= 0) & (
            coarse_cols < landsat_side) & (scl_phase != 4)
        red = (2250 - 1000) / 10000
        nir = (np.where(coarse_cols % 6 == 2, 2250, 4750) - 1000) / 10000
    if stage == "quality":
        return clear.astype("uint8"), np.ones(clear.shape, dtype=bool)
    return np.broadcast_to((nir - red) / (nir + red), clear.shape), clear


def aligned_values(window, stage, **geometry):
    first, first_valid = optical_values(window, role="first_product", stage="ndvi", **geometry)
    first_pixel = geometry["first_pixel"]
    second_pixel = geometry["second_pixel"]
    shift = geometry["offset"]
    row = math.floor(((window.row_off + 0.5) * first_pixel + shift) / second_pixel)
    col = math.floor(((window.col_off + 0.5) * first_pixel + shift) / second_pixel)
    ratio = first_pixel // second_pixel
    second, second_valid = optical_values(
        Window(col, row, (window.width - 1) * ratio + 1, (window.height - 1) * ratio + 1),
        role="second_product", stage="ndvi", **geometry,
    )
    second, second_valid = second[::ratio, ::ratio], second_valid[::ratio, ::ratio]
    common = first_valid & second_valid
    if stage == "aligned":
        return np.stack((first, second)), np.stack((common, common))
    if stage == "change":
        return second - first, common
    return np.where(second - first < -0.1, 1, np.where(second - first > 0.1, 3, 2)), common


def raster_info(dataset, path):
    nodata = ["nan" if item is not None and math.isnan(item) else item for item in dataset.nodatavals]
    return {
        "width": dataset.width, "height": dataset.height, "bands": dataset.count,
        "crs": dataset.crs.to_string(), "transform": list(dataset.transform[:6]), "dtypes": list(dataset.dtypes),
        "band_names": list(dataset.descriptions), "band_units": list(dataset.units), "nodata": nodata,
        "block_shapes": [list(block) for block in dataset.block_shapes],
        "compression": None if dataset.compression is None else dataset.compression.value,
        "mask_flags": [[flag.name for flag in band] for band in dataset.mask_flag_enums],
        "logical_bytes": dataset.width * dataset.height * sum(np.dtype(dtype).itemsize for dtype in dataset.dtypes),
        "file_bytes": path.stat().st_size,
    }


def raster_description(path):
    with rasterio.open(path) as dataset:
        return raster_info(dataset, path)


def compare_pixels(actual, expected, valid, tolerance, name):
    if expected.ndim == 2:
        expected, valid = expected[None], valid[None]
    require(actual.shape == expected.shape and np.array_equal(~np.ma.getmaskarray(actual), valid),
            f"{name} mask or tile shape differs from the independent reference")
    errors = np.abs(actual.data[valid].astype("float64") - expected[valid])
    worst = float(np.max(errors, initial=0))
    require(worst <= tolerance, f"{name} pixel error {worst} exceeds {tolerance}")
    return worst


def verify_raster(path, grid, *, dtypes, nodata, expected, tolerance=0, count_ones=False):
    counts = np.zeros(len(dtypes), dtype="int64")
    ones = np.zeros(len(dtypes), dtype="int64")
    worst = 0.0
    with rasterio.open(path) as dataset:
        info = raster_info(dataset, path)
        require(dataset.driver == "GTiff" and (info["crs"], dataset.shape, dataset.transform) ==
                (grid.crs, grid.shape, grid.affine) and info["dtypes"] == list(dtypes)
                and info["nodata"] == list(nodata), f"Grid, NoData or dtypes differ: {path}")
        for _, window in dataset.block_windows(1):
            actual = dataset.read(window=window, masked=True)
            values, valid = expected(window)
            worst = max(worst, compare_pixels(actual, values, valid, tolerance, str(path)))
            counts += (~np.ma.getmaskarray(actual)).sum(axis=(1, 2))
            if count_ones:
                ones += np.count_nonzero((actual.data == 1) & ~np.ma.getmaskarray(actual), axis=(1, 2))
    cells = math.prod(grid.shape)
    report = {
        **info, "valid_pixels_by_band": counts.tolist(), "masked_pixels_by_band": (cells - counts).tolist(),
        "maximum_absolute_error": worst, "absolute_tolerance": tolerance,
    }
    if count_ones:
        report["value_one_pixels_by_band"] = ones.tolist()
    return report


def _projected_rectangles(transformer, rectangles, *, step):
    """Integrate the union's exterior edges and hole rings; shared cell edges cancel."""
    lengths = np.abs(rectangles[:, 2:] - rectangles[:, :2]).max(axis=0)
    positions_x = np.arange(max(16, math.ceil(lengths[0] / step))) / max(16, math.ceil(lengths[0] / step))
    positions_y = np.arange(max(16, math.ceil(lengths[1] / step))) / max(16, math.ceil(lengths[1] / step))
    p = np.r_[positions_x, np.ones_like(positions_y), 1 - positions_x, np.zeros_like(positions_y)]
    q = np.r_[np.zeros_like(positions_x), positions_y, np.ones_like(positions_x), 1 - positions_y]
    areas = []
    for start in range(0, len(rectangles), 128):
        x0, y0, x1, y1 = rectangles[start:start + 128].T
        x = x0[:, None] + (x1 - x0)[:, None] * p
        y = y0[:, None] + (y1 - y0)[:, None] * q
        x, y = transformer.transform(x, y, errcheck=True)
        x, y = x - x[:, :1], y - y[:, :1]
        areas.extend(np.abs(np.sum(x * np.roll(y, -1, axis=1) - y * np.roll(x, -1, axis=1), axis=1)) / 2)
    return np.asarray(areas)


def _rectangles(grid, col, row, width, height):
    col, row, width, height = np.broadcast_arrays(col, row, width, height)
    x0, y0 = grid.affine * (col.ravel(), row.ravel())
    x1, y1 = grid.affine * ((col + width).ravel(), (row + height).ravel())
    return np.column_stack((x0, y0, x1, y1))


def area_reference(grid, period=None):
    transformer = Transformer.from_crs(grid.crs, "EPSG:6933", always_xy=True)
    side = grid.shape[1]
    if period is None:
        columns = np.arange(side)
        phase = columns % 6
        stripes = _rectangles(grid, columns, 0, 1, grid.shape[0])
        groups = {class_id: stripes[phase == index] for class_id, index in ((1, 1), (2, 2), (3, 0))}

        def integrate(step):
            return {code: float(_projected_rectangles(transformer, rectangles, step=step).sum())
                    for code, rectangles in groups.items()}
    else:
        columns = np.arange(period // 2, side, period)
        rows = np.arange(period // 2, grid.shape[0], period)
        holes = _rectangles(grid, columns[None, :] - 1, rows[:, None] - 1, 3, 3)
        exterior = _rectangles(grid, 1, 1, side - 2, grid.shape[0] - 2)

        def integrate(step):
            outer, = _projected_rectangles(transformer, exterior, step=step)
            return {7: float(outer - _projected_rectangles(transformer, holes, step=step).sum())}
    coarse, refined = integrate(50), integrate(25)
    for code, area in refined.items():
        require(math.isclose(area, coarse[code], rel_tol=1e-8, abs_tol=0.02), "Independent area integration failed to converge")
    return refined
