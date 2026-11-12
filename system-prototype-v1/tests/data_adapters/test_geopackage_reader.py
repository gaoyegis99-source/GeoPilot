import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
from shapely.geometry import Point

from geopilot.data.errors import SourceReadError
from geopilot.data.geopackage_reader import list_geopackage_layers, read_geopackage_metadata
from geopilot.domain.data_asset import TableMetadata, VectorMetadata


ROAD_SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "vector" / "road-buffer.gpkg"


def test_list_and_read_layers_of_the_golden_geopackage():
    before = hashlib.sha256(ROAD_SAMPLE.read_bytes()).hexdigest()
    layers = list_geopackage_layers(ROAD_SAMPLE)
    assert len(layers) == 4
    assert dict(layers) == {
        "analysis_extent": "Polygon",
        "roads": "LineString",
        "targets": "Point",
        "zones": "Polygon",
    }
    for name, geometry_type, feature_count in (
        ("analysis_extent", "Polygon", 1),
        ("roads", "LineString", 3),
        ("targets", "Point", 9),
        ("zones", "Polygon", 2),
    ):
        selected, metadata = read_geopackage_metadata(ROAD_SAMPLE, name)
        assert selected == name
        assert isinstance(metadata, VectorMetadata)
        assert metadata.format == "GPKG"
        assert metadata.crs == "EPSG:32650"
        assert metadata.geometry_type == geometry_type
        assert metadata.feature_count == feature_count
        assert [(field.name, field.dtype, field.missing_count) for field in metadata.fields] == [
            ("source_feature_id", "object", 0)
        ]
        if name == "roads":
            assert metadata.bounds == (500150, 4401500, 501850, 4402650)
    assert hashlib.sha256(ROAD_SAMPLE.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("requested_layer", [None, "absent", "ROADS", "", 1])
def test_ambiguous_or_missing_layer_has_a_structured_error(requested_layer):
    with pytest.raises(SourceReadError) as caught:
        read_geopackage_metadata(ROAD_SAMPLE, requested_layer)
    assert caught.value.code == "SOURCE_UNREADABLE"
    assert caught.value.phase == "data"
    assert caught.value.field == "source.layer"
    if requested_layer is None:
        assert "roads" in str(caught.value)


def test_single_spatial_layer_is_inferred_and_keeps_unknown_crs(tmp_path):
    path = tmp_path / "single.gpkg"
    points = gpd.GeoDataFrame({"label": ["p1"]}, geometry=[Point(5, 6)])
    with pytest.warns(UserWarning, match="crs.*was not provided"):
        pyogrio.write_dataframe(points, path, layer="places", driver="GPKG")

    selected, metadata = read_geopackage_metadata(path)
    assert selected == "places"
    assert isinstance(metadata, VectorMetadata)
    assert metadata.geometry_type == "Point"
    assert metadata.feature_count == 1
    assert metadata.crs is None
    assert "CRS_UNKNOWN" in metadata.issues


def test_gpkg_table_preserves_nonspatial_schema_and_null_counts(tmp_path):
    path = tmp_path / "inventory.gpkg"
    table = pd.DataFrame({"name": ["a", None], "units": [1, 2]})
    pyogrio.write_dataframe(table, path, layer="inventory", driver="GPKG")

    assert list(list_geopackage_layers(path)) == [("inventory", None)]
    selected, metadata = read_geopackage_metadata(path)
    assert selected == "inventory"
    assert isinstance(metadata, TableMetadata)
    assert (metadata.format, metadata.row_count, metadata.crs, metadata.bounds) == (
        "GPKG", 2, None, None,
    )
    assert [(field.name, field.dtype, field.missing_count) for field in metadata.fields] == [
        ("name", "object", 1), ("units", "int64", 0),
    ]


@pytest.mark.parametrize("reader", [list_geopackage_layers, read_geopackage_metadata])
def test_non_geopackage_driver_is_rejected_despite_gpkg_suffix(tmp_path, reader):
    fake = tmp_path / "roads.gpkg"
    fake.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    assert pyogrio.read_info(fake, layer="roads")["driver"] == "GeoJSON"

    with pytest.raises(SourceReadError) as caught:
        reader(fake)
    assert caught.value.field == "source.path"


def test_plain_sqlite_header_does_not_make_a_geopackage(tmp_path):
    path = tmp_path / "ordinary.gpkg"
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("CREATE TABLE inventory (name TEXT)")
        database.execute("INSERT INTO inventory VALUES ('sample')")
    assert path.read_bytes().startswith(b"SQLite format 3\x00")

    with pytest.warns(RuntimeWarning, match="bad application_id"), pytest.raises(SourceReadError) as caught:
        list_geopackage_layers(path)
    assert caught.value.field == "source.path"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permits colons in local filenames")
def test_relative_filename_with_uri_prefix_is_opened_as_a_local_file(tmp_path, monkeypatch):
    path = tmp_path / "http:roads.gpkg"
    path.write_bytes(ROAD_SAMPLE.read_bytes())
    monkeypatch.chdir(tmp_path)

    name, metadata = read_geopackage_metadata(Path(path.name), "roads")
    assert (name, metadata.geometry_type, metadata.feature_count) == ("roads", "LineString", 3)


def test_gdal_archive_separator_cannot_redirect_a_local_path(tmp_path):
    path = tmp_path / "archive!roads.gpkg"
    path.write_bytes(ROAD_SAMPLE.read_bytes())
    with pytest.raises(SourceReadError) as caught:
        list_geopackage_layers(path)
    assert caught.value.field == "source.path"


@pytest.mark.parametrize("bad_path", ["missing", "directory", "corrupt", "remote"])
def test_unreadable_and_nonlocal_paths_are_structured(tmp_path, bad_path):
    path = tmp_path / f"{bad_path}.gpkg"
    if bad_path == "directory":
        path.mkdir()
    elif bad_path == "corrupt":
        path.write_bytes(b"not SQLite or GeoPackage")
    elif bad_path == "remote":
        path = "https://example.com/roads.gpkg"

    with pytest.raises(SourceReadError) as caught:
        list_geopackage_layers(path)
    assert caught.value.field == "source.path"
