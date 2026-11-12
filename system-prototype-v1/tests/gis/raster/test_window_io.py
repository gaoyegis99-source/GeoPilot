from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.enums import ColorInterp
from rasterio.shutil import copy as copy_dataset
from rasterio.transform import Affine
from rasterio.windows import Window

from geopilot.data.raster_reader import read_raster_metadata
from geopilot.gis.raster.window_io import copy_raster_window, read_raster_window


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "raster"
DEM = FIXTURES / "dem-plane.tif"
SPECTRAL = FIXTURES / "two-band.tif"
GRID = Affine(10, 0, 500000, 0, -10, 4400070)
SPECTRAL_PIXELS = np.array([
    [[111, 112, 65535, 114], [121, 122, 123, 124]],
    [[211, 212, 213, 214], [65535, 222, 223, 224]],
], dtype="uint16")


def test_reads_dem_analysis_window_on_original_grid():
    block = read_raster_window(DEM, Window(2.0, 2.0, 3.0, 3.0))

    assert block.data.shape == (1, 3, 3)
    assert block.data.dtype == np.dtype("float32")
    np.testing.assert_array_equal(block.data.data, [[[120, 130, 140]] * 3])
    assert not np.ma.getmaskarray(block.data).any()
    assert block.crs.to_epsg() == 32650
    assert block.transform == Affine(10, 0, 500020, 0, -10, 4400050)
    assert block.nodata == (-9999.0,)


def test_reads_requested_bands_in_order_with_independent_masks():
    block = read_raster_window(SPECTRAL, Window(0, 1, 4, 2), bands=(2, 1))

    assert block.data.dtype == np.dtype("uint16")
    np.testing.assert_array_equal(block.data.data, SPECTRAL_PIXELS[::-1])
    np.testing.assert_array_equal(np.ma.getmaskarray(block.data), SPECTRAL_PIXELS[::-1] == 65535)
    assert block.nodata == (65535.0, 65535.0)


@pytest.mark.parametrize("window", [
    Window(-1, 0, 1, 1),
    Window(0, -1, 1, 1),
    Window(0, 0, 0, 1),
    Window(0, 0, 1, 0),
    Window(6, 0, 2, 1),
    Window(0, 6, 1, 2),
    Window(0.5, 0, 1, 1),
    Window(0, 0, 1, 1.25),
    Window(float("inf"), 0, 1, 1),
    Window(0, 0, float("nan"), 1),
    Window(True, 0, 1, 1),
])
def test_refuses_invalid_windows_without_creating_an_output(window, tmp_path):
    output = tmp_path / "crop.tif"

    with pytest.raises(ValueError, match="[Ww]indow"):
        read_raster_window(DEM, window)
    with pytest.raises(ValueError, match="[Ww]indow"):
        copy_raster_window(DEM, output, window)

    assert not output.exists()


@pytest.mark.parametrize("bands", [(), (0,), (2,), (1.5,), (True,)])
def test_refuses_invalid_band_selection(bands):
    with pytest.raises(ValueError, match="[Bb]and"):
        read_raster_window(DEM, Window(0, 0, 1, 1), bands=bands)


def test_copies_golden_pixels_band_metadata_and_nodata(tmp_path):
    output = tmp_path / "crop.tif"
    source_bytes = SPECTRAL.read_bytes()

    metadata = copy_raster_window(SPECTRAL, output, Window(0, 1, 4, 2))

    assert metadata == read_raster_metadata(output)
    assert metadata.format == "GTiff"
    assert metadata.crs == "EPSG:32650"
    assert (metadata.width, metadata.height) == (4, 2)
    assert metadata.transform == (10, 0, 500000, 0, -10, 4400060)
    assert metadata.bounds == (500000, 4400040, 500040, 4400060)
    assert metadata.resolution == (10, 10)
    assert metadata.nodata == (65535.0, 65535.0)
    assert metadata.band_names == ("red", "nir")
    assert metadata.band_units == ("DN", "DN")
    assert metadata.band_dtypes == ("uint16", "uint16")
    with rasterio.open(output) as result:
        np.testing.assert_array_equal(result.read(), SPECTRAL_PIXELS)
        np.testing.assert_array_equal(np.ma.getmaskarray(result.read(masked=True)), SPECTRAL_PIXELS == 65535)
    assert SPECTRAL.read_bytes() == source_bytes


def test_reads_and_copies_a_real_cog(tmp_path):
    source = tmp_path / "source-cog.tif"
    output = tmp_path / "from-cog.tif"
    copy_dataset(SPECTRAL, source, driver="COG", BLOCKSIZE=128)
    window = Window(0, 1, 4, 2)

    with rasterio.open(source) as dataset:
        assert dataset.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
    observed = read_raster_window(source, window)
    np.testing.assert_array_equal(observed.data.data, SPECTRAL_PIXELS)
    np.testing.assert_array_equal(np.ma.getmaskarray(observed.data), SPECTRAL_PIXELS == 65535)

    metadata = copy_raster_window(source, output, window)
    assert metadata.transform == tuple(observed.transform)[:6]
    with rasterio.open(output) as result:
        np.testing.assert_array_equal(result.read(), SPECTRAL_PIXELS)
        np.testing.assert_array_equal(result.read_masks() == 0, SPECTRAL_PIXELS == 65535)


def test_preserves_internal_mask_and_nan_nodata_on_rotated_grid(tmp_path):
    source = tmp_path / "rotated-dem.tif"
    output = tmp_path / "rotated-crop.tif"
    values = np.arange(35, dtype="float32").reshape(5, 7)
    values[2, 2] = np.nan
    valid = np.full(values.shape, 255, dtype="uint8")
    valid[2, 2] = valid[1, 3] = 0
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        with rasterio.open(
            source, "w", driver="GTiff", width=7, height=5, count=1, dtype="float32",
            crs="EPSG:32650", transform=Affine(10, 2, 500000, 1, -15, 4400070), nodata=np.nan,
        ) as dem:
            dem.write(values, 1)
            dem.write_mask(valid)
            dem.units = ("m",)
            dem.descriptions = ("elevation",)
            dem.scales = (2.0,)
            dem.offsets = (-3.0,)
            dem.update_tags(AREA_OR_POINT="Point")

    window = Window(1, 1, 4, 3)
    read = read_raster_window(source, window)
    metadata = copy_raster_window(source, output, window)

    expected_transform = Affine(10, 2, 500012, 1, -15, 4400056)
    assert read.transform == expected_transform
    assert np.isnan(read.nodata[0])
    np.testing.assert_array_equal(np.ma.getmaskarray(read.data)[0], valid[1:4, 1:5] == 0)
    assert metadata.transform == tuple(expected_transform)[:6]
    assert metadata.nodata == ("nan",)
    with rasterio.open(output) as cropped:
        assert cropped.files == [str(output)]
        assert np.isnan(cropped.nodata)
        assert cropped.scales == (2.0,)
        assert cropped.offsets == (-3.0,)
        assert cropped.units == ("m",)
        assert cropped.descriptions == ("elevation",)
        assert cropped.tags()["AREA_OR_POINT"] == "Point"
        np.testing.assert_array_equal(cropped.read(1), values[1:4, 1:5])
        np.testing.assert_array_equal(cropped.read_masks(1), valid[1:4, 1:5])


def test_preserves_alpha_band_and_its_effective_mask(tmp_path):
    source = tmp_path / "rgba.tif"
    output = tmp_path / "rgba-crop.tif"
    rgba = np.full((4, 2, 4), 55, dtype="uint8")
    rgba[3] = [[255, 0, 127, 255], [255, 127, 255, 0]]
    colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
    with rasterio.open(
        source, "w", driver="GTiff", width=4, height=2, count=4, dtype="uint8",
        crs="EPSG:32650", transform=GRID, photometric="RGB",
    ) as raster:
        raster.colorinterp = colorinterp
        raster.write(rgba)

    copy_raster_window(source, output, Window(1, 0, 3, 2))

    with rasterio.open(output) as result:
        assert result.colorinterp == colorinterp
        assert result.nodata is None
        np.testing.assert_array_equal(result.read(), rgba[:, :, 1:])
        np.testing.assert_array_equal(result.read_masks(1), rgba[3, :, 1:])
        assert (result.read_masks(4) == 255).all()


def test_copy_reads_bounded_windows_and_writes_all_real_pixels(tmp_path, monkeypatch):
    source = tmp_path / "large-input.tif"
    output = tmp_path / "large-crop.tif"
    values = np.arange(641 * 617, dtype="uint32").reshape(617, 641)
    valid = np.where(values % 97 == 0, 0, 255).astype("uint8")
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        source, "w", driver="GTiff", width=641, height=617, count=1, dtype="uint32",
        crs="EPSG:32650", transform=GRID, tiled=True, blockxsize=128, blockysize=128,
    ) as raster:
        raster.write(values, 1)
        raster.write_mask(valid)

    original_open = rasterio.open
    reads = []
    mask_reads = []

    def record(read, windows):
        def tracked_read(*args, **kwargs):
            windows.append(kwargs.get("window"))
            return read(*args, **kwargs)

        return tracked_read

    @contextmanager
    def track_reads(path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as dataset:
            if Path(path) == source:
                dataset.read = record(dataset.read, reads)
                dataset.read_masks = record(dataset.read_masks, mask_reads)
            yield dataset

    with monkeypatch.context() as patch:
        patch.setattr(rasterio, "open", track_reads)
        copy_raster_window(source, output, Window(13, 19, 520, 515))

    assert len(reads) > 1
    assert all(window and window.width <= 256 and window.height <= 256 for window in reads)
    assert sum(window.width * window.height for window in reads) == 520 * 515
    assert mask_reads == reads
    with rasterio.open(output) as result:
        assert result.block_shapes == [(256, 256)]
        np.testing.assert_array_equal(result.read(1), values[19:534, 13:533])
        np.testing.assert_array_equal(result.read_masks(1), valid[19:534, 13:533])


def test_refuses_existing_destination_including_source_and_dangling_link(tmp_path):
    output = tmp_path / "crop.tif"
    dangling = tmp_path / "alias.tif"
    absent = tmp_path / "must-stay-absent.tif"
    dangling.symlink_to(absent)
    window = Window(1, 1, 1, 1)
    source_bytes = DEM.read_bytes()
    copy_raster_window(DEM, output, window)
    output_bytes = output.read_bytes()

    for path in (output, DEM, dangling):
        with pytest.raises(FileExistsError):
            copy_raster_window(DEM, path, window)

    assert output.read_bytes() == output_bytes
    assert DEM.read_bytes() == source_bytes
    assert dangling.is_symlink() and not absent.exists()


def test_refuses_an_existing_companion_that_could_override_the_new_raster(tmp_path):
    output = tmp_path / "crop.tif"
    companion = tmp_path / "crop.tif.aux.xml"
    companion.write_text("<PAMDataset/>", encoding="utf-8")

    with pytest.raises(FileExistsError, match="companion"):
        copy_raster_window(DEM, output, Window(0, 0, 1, 1))

    assert companion.read_text(encoding="utf-8") == "<PAMDataset/>"
    assert not output.exists()
