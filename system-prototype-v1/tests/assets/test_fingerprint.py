import hashlib
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import geopandas
import numpy as np
import pyogrio
import pytest
import rasterio
from shapely.geometry import Point

from geopilot.data.fingerprint import FileFingerprint, fingerprint_file


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_content_hashes_the_entire_file_and_remains_stable(tmp_path):
    source = tmp_path / "sample.bin"
    content = bytes(range(256)) * 10_000 + b"last bytes"
    source.write_bytes(content)

    first = fingerprint_file(source)
    expected = hashlib.sha256(b"geopilot-file-content-v1\0main:2560010\0" + content).hexdigest()
    assert isinstance(first, FileFingerprint)
    assert first.digest == expected
    assert first.size_bytes == len(content)
    assert first.modified_ns == source.stat().st_mtime_ns
    assert fingerprint_file(source) == first
    assert source.read_bytes() == content


def test_timestamp_only_and_same_metadata_content_changes_are_distinguished(tmp_path):
    source = tmp_path / "sample.geojson"
    source.write_bytes(b"one")
    content = fingerprint_file(source)
    metadata = fingerprint_file(source, strategy="metadata")
    before_touch = source.stat()
    os.utime(source, ns=(before_touch.st_atime_ns, before_touch.st_mtime_ns + 2_000_000_000))
    touched = source.stat()

    assert fingerprint_file(source).digest == content.digest
    assert fingerprint_file(source).modified_ns == touched.st_mtime_ns
    assert fingerprint_file(source, strategy="metadata").digest != metadata.digest

    touched_metadata = fingerprint_file(source, strategy="metadata")
    source.write_bytes(b"two")
    os.utime(source, ns=(touched.st_atime_ns, touched.st_mtime_ns))
    assert fingerprint_file(source).digest != content.digest
    assert fingerprint_file(source, strategy="metadata") == touched_metadata


def test_shapefile_sidecars_are_included_and_path_does_not_affect_content(tmp_path):
    source = tmp_path / "places.shp"
    points = geopandas.GeoDataFrame({"place": ["station"]}, geometry=[Point(500000, 4500000)], crs=32650)
    pyogrio.write_dataframe(points, source, driver="ESRI Shapefile")
    original_members = [source.with_suffix(suffix) for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg")]
    assert all(path.is_file() for path in original_members)

    first = fingerprint_file(source)
    assert first.size_bytes == sum(path.stat().st_size for path in original_members)
    assert first.modified_ns == max(path.stat().st_mtime_ns for path in original_members)

    backup = tmp_path / "backup"
    backup.mkdir()
    for member in reversed(original_members):
        shutil.copyfile(member, backup / f"renamed{member.suffix}")
    assert fingerprint_file(backup / "renamed.shp").digest == first.digest

    (tmp_path / "other.dbf").write_bytes(b"unrelated")
    assert fingerprint_file(source).digest == first.digest
    encoding = source.with_suffix(".cpg")
    encoding.write_text("CP1252", encoding="ascii")
    assert fingerprint_file(source).digest != first.digest


def test_uppercase_companion_and_file_boundaries_affect_content_digest(tmp_path):
    source = tmp_path / "roads.SHP"
    companion = tmp_path / "roads.DBF"
    source.write_bytes(b"ab")
    companion.write_bytes(b"c")
    first = fingerprint_file(source)
    source.write_bytes(b"a")
    companion.write_bytes(b"bc")
    second = fingerprint_file(source)
    assert second.size_bytes == first.size_bytes == 3
    assert second.digest != first.digest


@pytest.mark.parametrize("suffix", [".tif", ".cog"])
def test_geotiff_external_mask_and_pam_sidecars_change_identity(tmp_path, suffix):
    source = tmp_path / f"dem{suffix}"
    shutil.copyfile(FIXTURES / "raster" / "dem-plane.tif", source)
    data_only = fingerprint_file(source)
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=False), rasterio.open(source, "r+") as raster:
        mask = np.full((raster.height, raster.width), 255, dtype="uint8")
        mask[0, 0] = 0
        raster.write_mask(mask)
    external_mask = tmp_path / f"{source.name}.msk"
    assert external_mask.is_file()
    with_mask = fingerprint_file(source)
    assert with_mask.digest != data_only.digest
    assert with_mask.size_bytes == source.stat().st_size + external_mask.stat().st_size

    pam = tmp_path / f"{source.name}.aux.xml"
    pam.write_text(
        "<PAMDataset><GeoTransform>501000,20,0,4401000,0,-20</GeoTransform></PAMDataset>",
        encoding="utf-8",
    )
    with rasterio.open(source) as raster:
        assert raster.transform == rasterio.Affine(20, 0, 501000, 0, -20, 4401000)
    assert fingerprint_file(source).digest != with_mask.digest


def test_geopackage_wal_edits_are_detected_before_checkpoint(tmp_path):
    source = tmp_path / "roads.gpkg"
    shutil.copyfile(FIXTURES / "vector" / "road-buffer.gpkg", source)
    with closing(sqlite3.connect(source)) as database:
        assert database.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        database.execute("PRAGMA wal_autocheckpoint=0")
        database.execute("UPDATE gpkg_contents SET description='first' WHERE table_name='roads'")
        database.commit()
        wal = tmp_path / "roads.gpkg-wal"
        assert wal.is_file()
        main_bytes = source.read_bytes()
        first = fingerprint_file(source)
        assert first.size_bytes == source.stat().st_size + wal.stat().st_size

        database.execute("UPDATE gpkg_contents SET description='next' WHERE table_name='roads'")
        database.commit()
        assert source.read_bytes() == main_bytes
        assert fingerprint_file(source).digest != first.digest


@pytest.mark.parametrize("change", ["replace_primary", "append_primary", "add_sidecar"])
def test_changed_source_group_is_rejected_instead_of_returning_a_mixed_digest(tmp_path, monkeypatch, change):
    source = tmp_path / "source.shp"
    source.write_bytes(b"original")
    original_stat = source.stat()
    replacement = tmp_path / "replacement.shp"
    replacement.write_bytes(b"changed!")
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    original_open = os.open

    def open_then_change(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if Path(path) == source:
            if change == "replace_primary":
                os.replace(replacement, source)
            elif change == "append_primary":
                with source.open("ab") as target:
                    target.write(b"added")
            else:
                source.with_suffix(".prj").write_text("new companion", encoding="utf-8")
        return fd

    monkeypatch.setattr(os, "open", open_then_change)
    with pytest.raises(OSError) as error:
        fingerprint_file(source)
    assert Path(error.value.filename) == source
    assert "changed" in str(error.value).lower()


def test_missing_or_nonregular_source_has_a_locatable_error(tmp_path):
    missing = tmp_path / "missing.tif"
    with pytest.raises(FileNotFoundError) as error:
        fingerprint_file(missing)
    assert Path(error.value.filename) == missing

    directory = tmp_path / "directory.tif"
    directory.mkdir()
    with pytest.raises(OSError) as error:
        fingerprint_file(directory)
    assert Path(error.value.filename) == directory


@pytest.mark.parametrize("source", ["https://example.invalid/file.tif", "/vsicurl/https://example.invalid/file.tif", "//server/share/file.tif"])
def test_remote_source_syntax_is_rejected(source):
    with pytest.raises(OSError, match="local") as error:
        fingerprint_file(source)
    assert error.value.filename == source


def test_unknown_strategy_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="strategy"):
        fingerprint_file(tmp_path / "source", strategy="unknown")
