import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.windows import Window


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "raster"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))


def assert_raster(path, expected):
    pixels = np.asarray(expected["pixels"])
    expected_mask = pixels == expected["nodata"]
    with rasterio.open(path) as dataset:
        assert dataset.driver == "GTiff"
        assert dataset.crs.to_epsg() == expected["epsg"]
        assert dataset.crs.linear_units == "metre"
        assert tuple(dataset.transform)[:6] == tuple(expected["transform"])
        assert dataset.res == tuple(expected["resolution_m"])
        assert (dataset.width, dataset.height) == tuple(expected["size"])
        assert dataset.count == len(expected["band_names"])
        assert dataset.dtypes == (expected["dtype"],) * dataset.count
        assert dataset.nodatavals == (expected["nodata"],) * dataset.count
        assert dataset.descriptions == tuple(expected["band_names"])
        assert dataset.units == tuple(expected["band_units"])
        observed = dataset.read(masked=True)
        np.testing.assert_array_equal(np.ma.getmaskarray(observed), expected_mask)
        np.testing.assert_array_equal(observed.compressed(), pixels[~expected_mask])
        assert int((~expected_mask.any(axis=0)).sum()) == expected["common_valid_pixels"]


@pytest.mark.parametrize("filename", EXPECTED)
def test_shipped_raster_matches_handwritten_reference(filename):
    assert_raster(FIXTURES / filename, EXPECTED[filename])


def test_rasters_can_be_regenerated_in_a_new_directory(tmp_path):
    subprocess.run(
        [sys.executable, str(FIXTURES / "generate.py"), str(tmp_path)],
        check=True,
        timeout=30,
    )
    for filename, expected in EXPECTED.items():
        assert_raster(tmp_path / filename, expected)


def test_dem_has_valid_halo_for_every_analysis_window_pixel():
    reference = EXPECTED["dem-plane.tif"]
    window = reference["analysis_window"]
    halo = reference["neighborhood_radius_pixels"]
    with rasterio.open(FIXTURES / "dem-plane.tif") as dem:
        neighborhood = dem.read(1, window=Window(
            window["col_offset"] - halo,
            window["row_offset"] - halo,
            window["width"] + 2 * halo,
            window["height"] + 2 * halo,
        ), masked=True)
        assert neighborhood.shape == (5, 5)
        assert not np.ma.getmaskarray(neighborhood).any()
        np.testing.assert_array_equal(neighborhood[0], [110, 120, 130, 140, 150])
        np.testing.assert_array_equal(neighborhood[-1], [110, 120, 130, 140, 150])
