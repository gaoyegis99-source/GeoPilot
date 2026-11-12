import json
from pathlib import Path
from shutil import copyfile

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.io import DatasetReader
from rasterio.shutil import copy as copy_raster
from rasterio.transform import Affine, from_origin

from geopilot.data.errors import SourceReadError
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.data_asset import RasterMetadata


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "raster"


@pytest.mark.parametrize(
    ("filename", "width", "height", "bounds", "dtypes", "names", "units", "nodata"),
    [
        ("dem-plane.tif", 7, 7, (500000, 4400000, 500070, 4400070),
         ("float32",), ("elevation",), ("m",), (-9999,)),
        ("two-band.tif", 4, 3, (500000, 4400040, 500040, 4400070),
         ("uint16", "uint16"), ("red", "nir"), ("DN", "DN"), (65535, 65535)),
    ],
)
def test_reads_golden_geotiff_without_loading_pixels(
    monkeypatch, filename, width, height, bounds, dtypes, names, units, nodata,
):
    path = FIXTURES / filename
    original_open = rasterio.open
    opened = []

    def track_open(*args, **kwargs):
        dataset = original_open(*args, **kwargs)
        opened.append(dataset)
        return dataset

    def reject_pixel_access(*args, **kwargs):
        pytest.fail("Metadata extraction attempted pixel access")

    monkeypatch.setattr(rasterio, "open", track_open)
    for name in ("read", "read_masks", "dataset_mask", "sample", "statistics", "stats"):
        if hasattr(DatasetReader, name):
            monkeypatch.setattr(DatasetReader, name, reject_pixel_access)

    metadata = read_raster_metadata(path)

    assert isinstance(metadata, RasterMetadata)
    assert metadata.kind == "raster" and metadata.format == "GTiff"
    assert metadata.crs == "EPSG:32650" and metadata.issues == ()
    assert (metadata.width, metadata.height, metadata.band_count) == (width, height, len(dtypes))
    assert metadata.transform == (10, 0, 500000, 0, -10, 4400070)
    assert metadata.resolution == (10, 10)
    assert metadata.bounds == bounds
    assert metadata.band_dtypes == dtypes
    assert metadata.band_names == names
    assert metadata.band_units == units
    assert metadata.nodata == nodata
    assert len(opened) == 1 and opened[0].mode == "r" and opened[0].closed


def test_reads_real_cog_by_driver_with_non_tiff_extension(tmp_path):
    source = FIXTURES / "two-band.tif"
    cog = tmp_path / "products.cog"
    copy_raster(source, cog, driver="COG", BLOCKSIZE=128)
    with rasterio.open(cog) as actual:
        assert actual.driver == "GTiff"
        assert actual.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"

    assert read_raster_metadata(cog) == read_raster_metadata(source)


def test_retains_custom_crs_as_wkt(tmp_path):
    path = tmp_path / "custom-crs.tif"
    projection = "+proj=laea +lon_0=117.001 +lat_0=40.123 +ellps=WGS84 +units=m +no_defs"
    with rasterio.open(
        path, "w", driver="GTiff", width=2, height=2, count=1, dtype="uint8",
        transform=from_origin(-10, 10, 10, 10), crs=projection,
    ) as dataset:
        dataset.write(np.zeros((2, 2), dtype="uint8"), indexes=1)

    metadata = read_raster_metadata(path)
    assert metadata.crs is not None and not metadata.crs.startswith("EPSG:")
    transform = Transformer.from_crs("EPSG:4326", metadata.crs, always_xy=True)
    assert transform.transform(117.001, 40.123) == pytest.approx((0, 0), abs=1e-6, rel=0)


@pytest.mark.parametrize("filename", ["http:local.tif", "tile!day.tif"])
def test_special_local_names_reach_rasterio_as_absolute_paths(tmp_path, monkeypatch, filename):
    path = tmp_path / filename
    copyfile(FIXTURES / "dem-plane.tif", path)
    real_open = rasterio.open

    def open_local(candidate, *args, **kwargs):
        assert isinstance(candidate, Path) and candidate == path.resolve()
        return real_open(candidate, *args, **kwargs)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rasterio, "open", open_local)
    assert read_raster_metadata(filename).band_names == ("elevation",)


def test_rejects_gdal_virtual_path_before_opening(monkeypatch):
    def reject_open(*args, **kwargs):
        pytest.fail("A GDAL virtual path reached Rasterio")

    monkeypatch.setattr(rasterio, "open", reject_open)
    with pytest.raises(SourceReadError, match="GDAL virtual filesystem"):
        read_raster_metadata("/vsizip/local.zip/tile.tif")


@pytest.mark.parametrize(("stored_nodata", "expected_nodata"), [
    (None, None), (float("nan"), "nan"), (float("inf"), "inf"), (float("-inf"), "-inf"),
])
def test_unknown_crs_and_nonfinite_nodata_have_explicit_json_values(
    tmp_path, stored_nodata, expected_nodata,
):
    path = tmp_path / "unreferenced.tif"
    with rasterio.open(
        path, "w", driver="GTiff", width=2, height=1, count=1, dtype="float32",
        transform=from_origin(100, 200, 5, 10), nodata=stored_nodata,
    ) as dataset:
        dataset.write(np.array([[1, 2]], dtype="float32"), indexes=1)

    metadata = read_raster_metadata(path)
    payload = json.loads(metadata.model_dump_json())

    assert metadata.crs is None and metadata.issues == ("CRS_UNKNOWN",)
    assert metadata.band_names == (None,) and metadata.band_units == (None,)
    assert metadata.bounds == (100, 190, 110, 200)
    assert metadata.nodata == (expected_nodata,)
    assert payload["crs"] is None and payload["nodata"] == [expected_nodata]


@pytest.mark.parametrize(("transform", "bounds", "resolution"), [
    (Affine(-10, 0, 500000, 0, 20, 4400000), (499980, 4400000, 500000, 4400060), (10, 20)),
    (Affine(3, 4, 500000, 4, -3, 4400000), (500000, 4399991, 500018, 4400008), (5, 5)),
])
def test_bounds_and_resolution_preserve_flipped_or_rotated_affine(tmp_path, transform, bounds, resolution):
    path = tmp_path / "grid.tif"
    with rasterio.open(
        path, "w", driver="GTiff", width=2, height=3, count=1, dtype="uint8",
        transform=transform, crs="EPSG:32650",
    ) as dataset:
        dataset.write(np.zeros((3, 2), dtype="uint8"), indexes=1)

    metadata = read_raster_metadata(path)
    assert metadata.transform == tuple(transform)[:6]
    assert metadata.bounds == bounds
    assert metadata.resolution == resolution


def test_rejects_non_tiff_driver_even_with_tiff_extension(tmp_path):
    path = tmp_path / "renamed.tif"
    with rasterio.open(
        path, "w", driver="PNG", width=2, height=2, count=1, dtype="uint8",
        transform=from_origin(100, 200, 5, 10),
    ) as dataset:
        dataset.write(np.zeros((2, 2), dtype="uint8"), indexes=1)
    with rasterio.open(path) as dataset:
        assert dataset.driver == "PNG"

    with pytest.raises(SourceReadError) as error:
        read_raster_metadata(path)
    assert (error.value.code, error.value.phase, error.value.field) == ("SOURCE_UNREADABLE", "data", "source.path")


@pytest.mark.parametrize("kind", ["absent", "directory", "corrupt"])
def test_reports_invalid_local_source_as_structured_error(tmp_path, kind):
    path = tmp_path / "input.tif"
    if kind == "directory":
        path.mkdir()
    if kind == "corrupt":
        path.write_bytes(b"not a TIFF file")

    with pytest.raises(SourceReadError) as error:
        read_raster_metadata(path)
    assert error.value.code == "SOURCE_UNREADABLE"
    assert error.value.field == "source.path"
    assert path.name in str(error.value)
