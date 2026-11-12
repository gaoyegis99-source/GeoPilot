import subprocess
import sys
from pathlib import Path

import numpy as np
import pyogrio
import rasterio


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/smoke/python_gis_smoke.py"


def run_smoke(output_dir):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--output-dir", str(output_dir)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_real_gpkg_and_geotiff_round_trip(tmp_path):
    output_dir = tmp_path / "python GIS smoke"
    process = run_smoke(output_dir)

    assert process.returncode == 0, process.stderr
    assert str(output_dir) in process.stdout

    expected_points = {
        "P1": (2, (500010, 3200030)),
        "P2": (5, (500050, 3200010)),
    }
    expected_pixels = np.array(
        [
            [[100, 102, 104], [106, -9999, 110]],
            [[1, 2, -9999], [4, 5, 6]],
        ],
        dtype="int16",
    )
    expected_masks = np.where(expected_pixels == -9999, 0, 255)

    for role in ("input", "output"):
        gpkg = output_dir / f"{role}.gpkg"
        assert pyogrio.list_layers(gpkg).tolist() == [["checkpoints", "Point"]]
        info = pyogrio.read_info(gpkg, layer="checkpoints")
        assert info["driver"] == "GPKG"
        assert info["fields"].tolist() == ["sample_id", "weight"]
        data = pyogrio.read_dataframe(gpkg, layer="checkpoints")
        assert data.crs.to_epsg() == 32650
        assert len(data) == 2
        assert {
            row.sample_id: (row.weight, (row.geometry.x, row.geometry.y))
            for row in data.itertuples(index=False)
        } == expected_points

        with rasterio.open(output_dir / f"{role}.tif") as raster:
            assert raster.driver == "GTiff"
            assert raster.crs.to_epsg() == 32650
            assert (raster.width, raster.height, raster.count) == (3, 2, 2)
            assert tuple(raster.transform)[:6] == (20, 0, 500000, 0, -20, 3200040)
            assert raster.res == (20, 20)
            assert raster.nodatavals == (-9999, -9999)
            assert raster.dtypes == ("int16", "int16")
            assert raster.descriptions == ("elevation", "uncertainty")
            assert raster.units == ("m", "m")
            np.testing.assert_array_equal(raster.read(), expected_pixels)
            np.testing.assert_array_equal(raster.read_masks(), expected_masks)

    log = (output_dir / "smoke.log").read_text(encoding="utf-8")
    for name in ("input.gpkg", "output.gpkg", "input.tif", "output.tif"):
        assert name in log
    assert "PASS" in log


def test_refuses_existing_output_directory(tmp_path):
    existing = tmp_path / "previous-run"
    existing.mkdir()
    previous_output = existing / "output.gpkg"
    previous_output.write_bytes(b"previous output")

    process = run_smoke(existing)

    assert process.returncode != 0
    assert "output directory" in process.stderr
    assert str(existing) in process.stderr
    assert previous_output.read_bytes() == b"previous output"
    assert list(existing.iterdir()) == [previous_output]
