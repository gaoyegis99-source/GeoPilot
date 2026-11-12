import os
import shutil
import subprocess
import sys
from pathlib import Path

import pyogrio
import pytest
import rasterio

from geopilot.data.assets import InputChangedError, register_asset
from geopilot.data.errors import SourceReadError
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.resolver import resolve_asset
from geopilot.domain.data_asset import DataAsset, DataAssetRef
from geopilot.persistence.schema import initialize_database, open_database


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SOURCE_TREE = Path(__file__).resolve().parents[2] / "src"
RASTER = FIXTURES / "raster" / "two-band.tif"
PACKAGE = FIXTURES / "vector" / "road-buffer.gpkg"


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    initialize_database(path)
    return path


def _run_python(cwd, script, *args):
    result = subprocess.run(
        [sys.executable, "-c", script, *(os.fspath(arg) for arg in args)],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(SOURCE_TREE), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return DataAsset.model_validate_json(result.stdout)


@pytest.mark.parametrize(("fixture", "layer"), [(RASTER, ""), (PACKAGE, "roads")], ids=["tiff", "gpkg"])
def test_exact_asset_ref_reopens_after_registration_process_exits(database_path, tmp_path, fixture, layer):
    source = tmp_path / fixture.name
    shutil.copyfile(fixture, source)
    original = fingerprint_file(source)

    registered = _run_python(
        tmp_path,
        """
import sys
from geopilot.data.assets import register_asset

database, source, layer = sys.argv[1:]
print(register_asset(database, source, layer=layer or None).model_dump_json())
""",
        database_path, source, layer,
    )
    assert registered.version == 1
    assert registered.source.layer == (layer or None)
    assert registered.metadata.crs == "EPSG:32650"
    if layer:
        assert registered.metadata.feature_count == 3
    else:
        assert (registered.metadata.width, registered.metadata.height, registered.metadata.band_count) == (4, 3, 2)

    reopened = _run_python(
        tmp_path,
        """
import sys
from geopilot.data.resolver import resolve_asset
from geopilot.domain.data_asset import DataAssetRef

database, reference_json = sys.argv[1:]
print(resolve_asset(database, DataAssetRef.model_validate_json(reference_json)).model_dump_json())
""",
        database_path, registered.ref.model_dump_json(),
    )
    assert reopened == registered
    assert reopened.source.path == source.resolve()
    assert reopened.fingerprint == original.digest
    assert fingerprint_file(source) == original


def test_historical_ref_requires_matching_bytes_and_is_never_rebound_to_latest(database_path, tmp_path):
    source = tmp_path / RASTER.name
    shutil.copyfile(RASTER, source)
    first = register_asset(database_path, source)
    with rasterio.open(source, "r+") as dataset:
        band = dataset.read(1)
        band[0, 0] += 1
        dataset.write(band, 1)
    second = register_asset(database_path, source)
    assert (second.id, second.version) == (first.id, 2)
    assert first.metadata == second.metadata
    assert first.fingerprint != second.fingerprint

    current = fingerprint_file(source)
    with pytest.raises(InputChangedError) as error:
        resolve_asset(database_path, first.ref)
    assert error.value.code == "INPUT_CHANGED"
    assert resolve_asset(database_path, second.ref) == second
    assert fingerprint_file(source) == current

    shutil.copyfile(RASTER, source)
    restored = fingerprint_file(source)
    assert resolve_asset(database_path, first.ref) == first
    with pytest.raises(InputChangedError):
        resolve_asset(database_path, second.ref)
    assert fingerprint_file(source) == restored
    with open_database(database_path) as connection:
        rows = connection.execute("SELECT id, version, fingerprint FROM data_asset ORDER BY version").fetchall()
    assert [tuple(row) for row in rows] == [
        (first.id, 1, first.fingerprint),
        (second.id, 2, second.fingerprint),
    ]


@pytest.mark.parametrize("missing", ["id", "version"])
def test_unknown_exact_asset_ref_raises_lookup_error(database_path, tmp_path, missing):
    source = tmp_path / RASTER.name
    shutil.copyfile(RASTER, source)
    asset = register_asset(database_path, source)
    reference = (
        DataAssetRef(asset_id="absent", asset_version=1)
        if missing == "id"
        else DataAssetRef(asset_id=asset.id, asset_version=asset.version + 1)
    )
    with pytest.raises(LookupError):
        resolve_asset(database_path, reference)


def test_missing_file_returns_structured_source_error(database_path, tmp_path):
    source = tmp_path / RASTER.name
    shutil.copyfile(RASTER, source)
    asset = register_asset(database_path, source)
    source.rename(tmp_path / "moved.tif")

    with pytest.raises(SourceReadError) as error:
        resolve_asset(database_path, asset.ref)
    assert error.value.code == "SOURCE_UNREADABLE"
    assert error.value.field == "source.path"
    assert str(source) in error.value.message


def test_missing_geopackage_layer_returns_layer_error(database_path, tmp_path):
    source = tmp_path / PACKAGE.name
    shutil.copyfile(PACKAGE, source)
    asset = register_asset(database_path, source, layer="roads")

    replacement = tmp_path / "replacement.gpkg"
    targets = pyogrio.read_dataframe(PACKAGE, layer="targets")
    pyogrio.write_dataframe(targets, replacement, layer="targets", driver="GPKG")
    replacement.replace(source)
    current = fingerprint_file(source)

    with pytest.raises(SourceReadError) as error:
        resolve_asset(database_path, asset.ref)
    assert error.value.code == "SOURCE_UNREADABLE"
    assert error.value.field == "source.layer"
    assert "roads" in error.value.message
    assert fingerprint_file(source) == current
