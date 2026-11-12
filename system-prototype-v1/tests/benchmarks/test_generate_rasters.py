import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pyogrio
import pytest
import rasterio
from rasterio.transform import from_origin

from geopilot.gis.raster.terrain import write_slope_raster
from geopilot.remote_sensing.alignment import align_ndvi
from geopilot.remote_sensing.change import write_ndvi_change
from geopilot.remote_sensing.classification import classify_ndvi_change
from geopilot.remote_sensing.config_registry import load_sensor_registry
from geopilot.remote_sensing.indices import write_ndvi
from geopilot.remote_sensing.quality_mask import write_quality_mask
from geopilot.remote_sensing.scaling import scale_product_reflectance


ROOT = Path(__file__).resolve().parents[2]
GENERATORS = ("raster", "remote_sensing")


def cli(family, output, tier="S"):
    return subprocess.run(
        [sys.executable, str(ROOT / f"scripts/bench/generate_{family}.py"),
         "--tier", tier, "--output", str(output), "--root", str(ROOT)],
        capture_output=True, text=True, check=False,
    )


def _check_files(output, document):
    assert {path.name for path in output.iterdir()} == {"manifest.json", *(entry["path"] for entry in document["files"])}
    for entry in document["files"]:
        path = output / entry["path"]
        with path.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == entry["sha256"]
        assert path.stat().st_size == entry["bytes"]


def generate(family, output):
    result = cli(family, output)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document == json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert (document["version"], document["family"], document["tier"]) == (1, family, "S")
    _check_files(output, document)
    return document


def test_dem_and_real_horn_slope_match_independent_reference(tmp_path):
    output = tmp_path / "dem-input"
    document = generate("raster", output)
    reference = document["reference"]
    dem_path = output / "dem.tif"
    with rasterio.open(dem_path) as dataset:
        assert dataset.crs.to_epsg() == 32650
        assert dataset.transform == from_origin(500000, 4500000, 10, 10)
        assert dataset.shape == (512, 512) and dataset.nodata == -9999
        values = dataset.read(1, masked=True)
        assert (values[0, 0], values[0, 511], int(values.count())) == (100, 5210, 512**2 - 16)
        assert np.all(values.mask[64::128, 64::128])
        assert (values[64, 63], values[64, 65]) == (730, 750)
    extent = pyogrio.read_info(output / "analysis-extent.gpkg", layer="analysis_extent")
    assert extent["total_bounds"] == (500010.0, 4494890.0, 505110.0, 4499990.0)

    slope_path = tmp_path / "slope.tif"
    write_slope_raster(dem_path, slope_path, band=1, z_factor=1.0, slope_unit="degree")
    with rasterio.open(slope_path) as dataset:
        slope = dataset.read(1, masked=True)
        assert slope.count() == 510**2 - 9 * 16 == reference["valid_terrain_pixels"]
        assert np.all(slope.compressed() == 45)
        assert slope[1, 1] == slope[511 - 1, 511 - 1] == 45
        assert np.all(slope.mask[63:66, 63:66])
        assert slope[66, 64] == 45
        assert np.all(slope.mask[0]) and np.all(slope.mask[-1])
    _check_files(output, document)


def test_native_products_run_real_masks_alignment_and_change(tmp_path):
    output = tmp_path / "remote-input"
    document = generate("remote_sensing", output)
    reference = document["reference"]
    with rasterio.open(output / "sentinel-scl-20m.tif") as native, rasterio.open(output / "second-sentinel.tif") as packed:
        assert native.transform == from_origin(499993, 5100007, 20, 20)
        assert packed.transform == from_origin(499993, 5100007, 10, 10)
        assert native.shape == (289, 289) and packed.shape == (578, 578)
        assert packed.crs.to_epsg() == native.crs.to_epsg() == 32632
        np.testing.assert_array_equal(packed.read(1), native.read(1).repeat(2, axis=0).repeat(2, axis=1))
        assert packed.read_masks(1)[0, 0] == 0
        assert packed.read_masks(1)[1, 1] == 255

    registry = load_sensor_registry(ROOT / "config/sensors")
    ndvi = []
    for label, filename in (("first", "first-landsat.tif"), ("second", "second-sentinel.tif")):
        reflectance = tmp_path / f"{label}-reflectance.tif"
        quality = tmp_path / f"{label}-quality.tif"
        index = tmp_path / f"{label}-ndvi.tif"
        source = output / filename
        scale_product_reflectance(source, reflectance, registry=registry)
        mask = write_quality_mask(source, quality, registry=registry)
        if label == "first":
            assert mask.valid_pixels == 4 * 192**2 // 6
        else:
            assert mask.unknown_class_pixels == {"quality": 0}
        write_ndvi(reflectance, quality, index)
        ndvi.append(index)

    aligned = tmp_path / "aligned.tif"
    align_ndvi(
        *ndvi, aligned, resampling="nearest", analysis_extent=output / "analysis-extent.gpkg",
        analysis_layer="analysis_extent",
    )
    change_path = tmp_path / "change.tif"
    change = write_ndvi_change(aligned, change_path)
    assert change.summary.common_valid_pixels == 192**2 // 2 == reference["common_valid_pixels"]
    assert change.summary.positive_pixels == change.summary.negative_pixels == change.summary.zero_pixels == 6144
    with rasterio.open(change_path) as dataset:
        assert dataset.transform == from_origin(500000, 5100000, 30, 30)
        assert dataset.crs.to_epsg() == 32632
        pixels = dataset.read(1, masked=True)
        np.testing.assert_allclose(pixels[0, :3], (0.5, 0.5 - 11 / 17, 0), rtol=0, atol=1e-12)
        assert np.all(pixels.mask[0, 3:6])

    classified = classify_ndvi_change(change_path, tmp_path / "classes.tif", threshold=0.1, area_crs="EPSG:6933")
    assert {row["class_name"]: row["pixel_count"] for row in classified.statistics["classes"]} == reference["change_counts"]
    assert classified.statistics["total_area_m2"] > 0
    _check_files(output, document)


@pytest.mark.parametrize("family", GENERATORS)
def test_regeneration_is_byte_identical(family, tmp_path):
    first = generate(family, tmp_path / "one")
    second = generate(family, tmp_path / "two")
    assert first == second


@pytest.mark.parametrize("family", GENERATORS)
@pytest.mark.parametrize("symbolic", [False, True])
def test_existing_output_is_refused_without_overwrite(family, symbolic, tmp_path):
    target = tmp_path / "output"
    existing = tmp_path / "existing"
    if symbolic:
        existing.mkdir()
        target.symlink_to(existing, target_is_directory=True)
    else:
        existing = target
        existing.mkdir()
    marker = existing / "preserve.txt"
    marker.write_bytes(b"caller-owned")
    result = cli(family, target)
    assert result.returncode == 1 and "File exists" in result.stderr
    assert not result.stdout
    assert marker.read_bytes() == b"caller-owned"
    assert list(existing.iterdir()) == [marker]


def test_unknown_tier_does_not_create_output(tmp_path):
    output = tmp_path / "unknown"
    result = cli("raster", output, "XL")
    assert result.returncode == 2 and "invalid choice" in result.stderr
    assert not output.exists()
