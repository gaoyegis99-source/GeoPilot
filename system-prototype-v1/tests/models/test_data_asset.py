import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from geopilot.domain.data_asset import (
    AssetAvailability,
    AssetMetadata,
    DataAsset,
    DataAssetRef,
    FileSource,
    RasterMetadata,
    SourceType,
    TableMetadata,
    VectorMetadata,
)


@pytest.fixture
def vector_asset(tmp_path):
    return {
        "id": "asset-roads",
        "version": 2,
        "source_type": "local_file",
        "source": {
            "path": str(tmp_path / "roads.gpkg"),
            "layer": "roads",
            "modified_ns": 1760000000000,
        },
        "metadata": {
            "kind": "vector",
            "format": "GPKG",
            "crs": "EPSG:32650",
            "bounds": [500000, 4400000, 500500, 4400800],
            "issues": [],
            "geometry_type": "LineString",
            "feature_count": 3,
            "fields": [{"name": "road_name", "dtype": "str", "missing_count": 1}],
        },
        "fingerprint": "sha256:roads",
        "availability": "AVAILABLE",
        "created_at": "2026-11-11T08:45:00+08:00",
    }


@pytest.fixture
def raster_metadata():
    return {
        "kind": "raster",
        "format": "GTiff",
        "crs": "EPSG:32650",
        "bounds": [500000, 4400040, 500040, 4400070],
        "issues": [],
        "width": 4,
        "height": 3,
        "band_count": 5,
        "transform": [10, 0, 500000, 0, -10, 4400070],
        "resolution": [10, 10],
        "nodata": [None, "nan", "inf", "-inf", 65535],
        "band_dtypes": ["float32", "float32", "float32", "float32", "uint16"],
        "band_names": [None, "green", "blue", "nir", "quality"],
        "band_units": [None, None, None, None, "DN"],
    }


def test_vector_asset_ref_and_local_json_round_trip(vector_asset):
    asset = DataAsset.model_validate(vector_asset)
    assert asset.source_type is SourceType.LOCAL_FILE
    assert asset.availability is AssetAvailability.AVAILABLE
    assert isinstance(asset.source, FileSource)
    assert asset.source.path == Path(vector_asset["source"]["path"])
    assert isinstance(asset.metadata, VectorMetadata)
    assert asset.ref == DataAssetRef(asset_id="asset-roads", asset_version=2)
    assert asset.ref.model_dump(mode="json") == {"asset_id": "asset-roads", "asset_version": 2}
    assert asset.created_at == datetime.fromisoformat(vector_asset["created_at"])
    assert json.loads(asset.model_dump_json()) == vector_asset
    assert DataAsset.model_validate_json(asset.model_dump_json()) == asset
    assert DataAsset.model_validate(asset.model_dump(mode="json")) == asset


@pytest.mark.parametrize("version", [0, -1, True, 1.0, 1.5, "1", None])
def test_versions_are_strict_positive_integers(vector_asset, version):
    with pytest.raises(ValidationError):
        DataAssetRef(asset_id="asset-roads", asset_version=version)
    vector_asset["version"] = version
    with pytest.raises(ValidationError):
        DataAsset.model_validate(vector_asset)


@pytest.mark.parametrize("field", ["id", "source", "metadata", "fingerprint"])
def test_core_asset_fields_are_required(vector_asset, field):
    del vector_asset[field]
    with pytest.raises(ValidationError):
        DataAsset.model_validate(vector_asset)


@pytest.mark.parametrize(
    "path", ["roads.gpkg", "https://example.com/roads.gpkg", "file:///tmp/roads.gpkg", "/tmp/bad\x00.gpkg"]
)
def test_file_source_requires_an_absolute_local_path(path):
    with pytest.raises(ValidationError):
        FileSource(path=path)


@pytest.mark.parametrize(
    "field, value",
    [
        ("source_type", "remote"),
        ("source_type", b"local_file"),
        ("availability", "available"),
        ("id", ""),
        ("fingerprint", 123),
    ],
)
def test_asset_rejects_invalid_enums_and_strings(vector_asset, field, value):
    vector_asset[field] = value
    with pytest.raises(ValidationError):
        DataAsset.model_validate(vector_asset)


def test_postgis_snapshot_is_represented_as_local_file(vector_asset):
    vector_asset["source_type"] = "postgis_snapshot"
    vector_asset["availability"] = "UNAVAILABLE"
    asset = DataAsset.model_validate(vector_asset)
    assert asset.source_type is SourceType.POSTGIS_SNAPSHOT
    assert asset.availability is AssetAvailability.UNAVAILABLE
    assert asset.source.path.is_absolute()
    vector_asset["source"]["connection_alias"] = "db"
    with pytest.raises(ValidationError):
        DataAsset.model_validate(vector_asset)


@pytest.mark.parametrize("created_at", ["2026-11-11T08:45:00", datetime(2026, 11, 11), 1760000000, "1760000000"])
def test_created_at_requires_a_timestamp_with_timezone(vector_asset, created_at):
    vector_asset["created_at"] = created_at
    with pytest.raises(ValidationError):
        DataAsset.model_validate(vector_asset)


def test_unknown_vector_crs_is_explicit_and_never_inferred(vector_asset):
    vector_asset["metadata"].update(crs=None, bounds=None, issues=["CRS_UNKNOWN"])
    asset = DataAsset.model_validate(vector_asset)
    assert asset.model_dump(mode="json")["metadata"] == vector_asset["metadata"]
    vector_asset["metadata"]["issues"] = []
    with pytest.raises(ValidationError):
        DataAsset.model_validate(vector_asset)


def test_models_are_frozen_and_reject_extra_nested_data(vector_asset):
    asset = DataAsset.model_validate(vector_asset)
    with pytest.raises(ValidationError):
        asset.version = 3
    with pytest.raises(ValidationError):
        asset.metadata.fields[0].name = "changed"
    vector_asset["metadata"]["raw_features"] = []
    with pytest.raises(ValidationError):
        DataAsset.model_validate(vector_asset)


def test_raster_nodata_tokens_and_unknown_units_survive_json(raster_metadata):
    raster_metadata.update(crs=None, issues=["CRS_UNKNOWN"])
    adapter = TypeAdapter(AssetMetadata)
    metadata = adapter.validate_python(raster_metadata)
    assert isinstance(metadata, RasterMetadata)
    assert metadata.nodata == (None, "nan", "inf", "-inf", 65535)
    assert adapter.validate_json(adapter.dump_json(metadata)) == metadata
    assert json.loads(adapter.dump_json(metadata)) == raster_metadata


@pytest.mark.parametrize(
    "field, value",
    [
        ("width", False),
        ("height", 0),
        ("band_count", 4),
        ("bounds", [500100, 4400040, 500040, 4400070]),
        ("transform", [10, 0, 500000, 0, -10]),
        ("resolution", [0, 10]),
        ("resolution", [10, float("inf")]),
        ("nodata", [None, float("nan"), "inf", "-inf", 65535]),
        ("nodata", [None, "NaN", "inf", "-inf", 65535]),
        ("band_names", [None]),
        ("band_names", {"red", "green", "blue", "nir", "quality"}),
        ("band_units", [None]),
        ("band_dtypes", ["uint16"]),
    ],
)
def test_raster_grid_and_per_band_metadata_are_validated(raster_metadata, field, value):
    raster_metadata[field] = value
    with pytest.raises(ValidationError):
        RasterMetadata.model_validate(raster_metadata)


def test_table_metadata_preserves_driver_rows_and_fields_without_crs():
    adapter = TypeAdapter(AssetMetadata)
    source = {
        "kind": "table",
        "format": "CSV",
        "crs": None,
        "bounds": None,
        "issues": [],
        "row_count": 0,
        "fields": [{"name": "count", "dtype": "integer", "missing_count": None}],
    }
    table = adapter.validate_python(source)
    assert isinstance(table, TableMetadata)
    assert json.loads(adapter.dump_json(table)) == source
    with pytest.raises(ValidationError):
        adapter.validate_python({**source, "row_count": -1})
    with pytest.raises(ValidationError):
        adapter.validate_python({**source, "kind": "vector"})
