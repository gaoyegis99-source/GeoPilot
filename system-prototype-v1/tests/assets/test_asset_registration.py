import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import numpy as np
import pyogrio
import pytest
import rasterio
from rasterio.transform import from_origin

from geopilot.data import assets as asset_service
from geopilot.data.assets import InputChangedError, register_asset
from geopilot.data.errors import SourceReadError
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.data_asset import AssetAvailability, DataAsset, SourceType
from geopilot.persistence.schema import initialize_database, open_database


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RASTER = FIXTURES / "raster" / "two-band.tif"
PACKAGE = FIXTURES / "vector" / "road-buffer.gpkg"


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    initialize_database(path)
    return path


def _asset_rows(database_path):
    with open_database(database_path) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM data_asset")]


def test_raster_registration_persists_metadata_and_reuses_identity(database_path, tmp_path, monkeypatch):
    source = tmp_path / "two-band.tif"
    shutil.copyfile(RASTER, source)
    original = source.read_bytes()
    fingerprint = fingerprint_file(source)
    monkeypatch.chdir(tmp_path)

    first = register_asset(database_path, source.name)
    assert isinstance(first, DataAsset)
    assert first.id and first.version == 1
    assert first.source_type == SourceType.LOCAL_FILE
    assert first.availability == AssetAvailability.AVAILABLE
    assert first.source.path == source.resolve()
    assert first.source.layer is None
    assert first.source.modified_ns == fingerprint.modified_ns
    assert first.fingerprint == fingerprint.digest
    assert first.metadata.kind == "raster"
    assert first.metadata.format == "GTiff"
    assert first.metadata.crs == "EPSG:32650"
    assert (first.metadata.width, first.metadata.height, first.metadata.band_count) == (4, 3, 2)
    assert register_asset(database_path, source) == first

    record, = _asset_rows(database_path)
    assert (record["id"], record["version"], record["source_type"], record["availability"]) == (
        first.id, 1, "local_file", "AVAILABLE",
    )
    assert record["source_key"]
    assert record["fingerprint"] == fingerprint.digest
    assert json.loads(record["source_json"]) == first.source.model_dump(mode="json")
    assert json.loads(record["metadata_json"]) == first.metadata.model_dump(mode="json")
    assert source.read_bytes() == original
    assert source.stat().st_mtime_ns == fingerprint.modified_ns


def test_geopackage_layers_have_distinct_identity(database_path, tmp_path):
    source = tmp_path / "roads.gpkg"
    shutil.copyfile(PACKAGE, source)
    original = fingerprint_file(source)

    roads = register_asset(database_path, source, layer="roads")
    buildings = register_asset(database_path, source, layer="targets")
    assert roads.id != buildings.id
    assert roads.version == buildings.version == 1
    assert roads.source.path == buildings.source.path == source.resolve()
    assert roads.source.layer == "roads"
    assert buildings.source.layer == "targets"
    assert roads.fingerprint == buildings.fingerprint == original.digest
    assert (roads.metadata.geometry_type, roads.metadata.feature_count) == ("LineString", 3)
    assert (buildings.metadata.geometry_type, buildings.metadata.feature_count) == ("Point", 9)
    assert register_asset(database_path, source, layer="roads") == roads
    records = _asset_rows(database_path)
    assert len(records) == len({row["source_key"] for row in records}) == 2
    assert fingerprint_file(source) == original


def test_inferred_geopackage_layer_matches_explicit_registration(database_path, tmp_path):
    source = tmp_path / "single-layer.gpkg"
    roads = pyogrio.read_dataframe(PACKAGE, layer="roads")
    pyogrio.write_dataframe(roads, source, layer="only_layer", driver="GPKG")

    inferred = register_asset(database_path, source)
    assert inferred.source.layer == "only_layer"
    assert register_asset(database_path, source, layer="only_layer") == inferred
    assert len(_asset_rows(database_path)) == 1


@pytest.mark.parametrize(
    ("case", "layer", "field"),
    [
        ("missing", None, "source.path"),
        ("corrupt", None, "source.path"),
        ("ambiguous", None, "source.layer"),
        ("unknown_layer", "absent", "source.layer"),
        ("raster_layer", "roads", "source.layer"),
    ],
)
def test_failed_reads_leave_no_record(database_path, tmp_path, case, layer, field):
    if case in {"ambiguous", "unknown_layer"}:
        source = tmp_path / "roads.gpkg"
        shutil.copyfile(PACKAGE, source)
    else:
        source = tmp_path / "data.tif"
        if case == "corrupt":
            source.write_bytes(b"not a GeoTIFF")
        elif case == "raster_layer":
            shutil.copyfile(RASTER, source)

    with pytest.raises(SourceReadError) as error:
        register_asset(database_path, source, layer=layer)
    assert error.value.code == "SOURCE_UNREADABLE"
    assert error.value.field == field
    assert _asset_rows(database_path) == []


def test_missing_crs_remains_a_readable_asset_with_issue(database_path, tmp_path):
    source = tmp_path / "unknown-crs.tif"
    with rasterio.open(
        source, "w", driver="GTiff", width=2, height=2, count=1, dtype="uint8",
        transform=from_origin(500000, 4500000, 10, 10),
    ) as raster:
        raster.write(np.array([[2, 4], [6, 8]], dtype="uint8"), 1)

    asset = register_asset(database_path, source)
    assert asset.version == 1
    assert asset.availability == AssetAvailability.AVAILABLE
    assert asset.metadata.crs is None
    assert "CRS_UNKNOWN" in asset.metadata.issues
    record, = _asset_rows(database_path)
    assert record["availability"] == "AVAILABLE"
    assert "CRS_UNKNOWN" in json.loads(record["metadata_json"])["issues"]


@pytest.mark.parametrize("same_size_and_mtime", [False, True], ids=["different-layout", "same-size-and-mtime"])
def test_source_replaced_during_metadata_read_leaves_no_record(
    database_path, tmp_path, monkeypatch, same_size_and_mtime,
):
    source = tmp_path / "data.tif"
    replacement = tmp_path / "replacement.tif"
    shutil.copyfile(RASTER, source)
    if same_size_and_mtime:
        shutil.copyfile(RASTER, replacement)
        with rasterio.open(replacement, "r+") as raster:
            pixels = raster.read(1)
            pixels[0, 0] += 1
            raster.write(pixels, 1)
        original_stat = source.stat()
        os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        assert fingerprint_file(source, strategy="metadata") == fingerprint_file(replacement, strategy="metadata")
        assert fingerprint_file(source).digest != fingerprint_file(replacement).digest
    else:
        shutil.copyfile(FIXTURES / "raster" / "dem-plane.tif", replacement)
    actual_open = rasterio.open

    @contextmanager
    def open_then_replace(path, *args, **kwargs):
        with actual_open(path, *args, **kwargs) as raster:
            yield raster
        if Path(path) == source and replacement.exists():
            replacement.replace(source)

    monkeypatch.setattr(rasterio, "open", open_then_replace)
    with pytest.raises(InputChangedError) as error:
        register_asset(database_path, source)
    assert error.value.code == "INPUT_CHANGED"
    assert not replacement.exists()
    assert _asset_rows(database_path) == []


def test_same_observed_file_state_cannot_hide_pam_rewrite(database_path, tmp_path, monkeypatch):
    source = tmp_path / "data.tif"
    shutil.copyfile(RASTER, source)
    pam = tmp_path / "data.tif.aux.xml"
    original_pam = "<PAMDataset><GeoTransform>500000,10,0,4400070,0,-10</GeoTransform></PAMDataset>"
    pam.write_text(original_pam, encoding="utf-8")
    original_stat = pam.stat()
    original_content = fingerprint_file(source)
    original_state = asset_service.capture_file_state(source)
    actual_open = rasterio.open

    @contextmanager
    def open_then_rewrite(path, *args, **kwargs):
        with actual_open(path, *args, **kwargs) as raster:
            assert raster.transform.c == 500000
            yield raster
        if Path(path) == source:
            pam.write_text(original_pam.replace("500000", "501000"), encoding="utf-8")
            os.utime(pam, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    # Simulate a filesystem clock where ctime also has the same sampled value.
    monkeypatch.setattr(asset_service, "capture_file_state", lambda path: original_state)
    monkeypatch.setattr(rasterio, "open", open_then_rewrite)
    with pytest.raises(InputChangedError) as error:
        register_asset(database_path, source)

    assert error.value.code == "INPUT_CHANGED"
    rewritten_stat = pam.stat()
    assert (rewritten_stat.st_ino, rewritten_stat.st_size, rewritten_stat.st_mtime_ns) == (
        original_stat.st_ino, original_stat.st_size, original_stat.st_mtime_ns,
    )
    assert fingerprint_file(source).digest != original_content.digest
    with actual_open(source) as raster:
        assert raster.transform.c == 501000
    assert _asset_rows(database_path) == []


def test_concurrent_registration_of_the_same_source_creates_one_row(database_path, tmp_path):
    source = tmp_path / "concurrent.tif"
    shutil.copyfile(RASTER, source)
    workers = 4
    gate = Barrier(workers, timeout=5)

    def register():
        gate.wait()
        return register_asset(database_path, source)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(register) for _ in range(workers)]
        assets = [future.result(timeout=10) for future in futures]
    assert all(asset == assets[0] for asset in assets)
    assert assets[0].version == 1
    assert len(_asset_rows(database_path)) == 1
