from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest
from shapely.geometry import Point

from geopilot.data.errors import SourceReadError
from geopilot.data.vector_reader import read_layer_metadata, read_vector_metadata


ROAD_PACKAGE = Path(__file__).resolve().parents[1] / "fixtures" / "vector" / "road-buffer.gpkg"


def test_geojson_reports_fields_bounds_and_missing_counts(tmp_path):
    path = tmp_path / "places.geojson"
    frame = gpd.GeoDataFrame(
        {"label": ["first", None], "value": [1, 2]},
        geometry=[Point(117, 40), Point(117.25, 40.5)],
        crs="EPSG:4326",
    )
    pyogrio.write_dataframe(frame, path, driver="GeoJSON")

    result = read_vector_metadata(path)
    assert (result.kind, result.format, result.geometry_type) == ("vector", "GeoJSON", "Point")
    assert result.crs == "EPSG:4326"
    assert result.bounds == (117, 40, 117.25, 40.5)
    assert result.feature_count == 2
    assert {field.name: field.missing_count for field in result.fields} == {"label": 1, "value": 0}
    assert result.issues == ()


def test_shapefile_without_prj_keeps_crs_unknown(tmp_path):
    path = tmp_path / "unknown.shp"
    frame = gpd.GeoDataFrame({"name": ["one"]}, geometry=[Point(500000, 4400000)])
    with pytest.warns(UserWarning, match="crs"):
        pyogrio.write_dataframe(frame, path, driver="ESRI Shapefile")

    result = read_vector_metadata(path)
    assert result.format == "ESRI Shapefile"
    assert result.feature_count == 1
    assert result.crs is None
    assert "CRS_UNKNOWN" in result.issues
    assert result.bounds == (500000, 4400000, 500000, 4400000)


def test_vector_metadata_handles_geometries_without_attributes(tmp_path):
    path = tmp_path / "only-points.geojson"
    frame = gpd.GeoDataFrame(geometry=[Point(117, 40)], crs="EPSG:4326")
    pyogrio.write_dataframe(frame, path, driver="GeoJSON")

    result = read_vector_metadata(path)
    assert result.feature_count == 1
    assert result.fields == ()


def test_shared_layer_reader_reopens_p0_road_fixture():
    result = read_layer_metadata(ROAD_PACKAGE, "roads")
    assert (result.kind, result.format, result.crs) == ("vector", "GPKG", "EPSG:32650")
    assert (result.geometry_type, result.feature_count) == ("LineString", 3)
    assert [field.name for field in result.fields] == ["source_feature_id"]
    assert result.fields[0].missing_count == 0


@pytest.mark.parametrize("exists", [False, True], ids=["missing", "unreadable"])
def test_source_failures_have_a_structured_error(tmp_path, exists):
    path = tmp_path / "unreadable.geojson"
    if exists:
        path.write_text("incomplete data", encoding="utf-8")

    with pytest.raises(SourceReadError) as result:
        read_vector_metadata(path)

    assert result.value.code == "SOURCE_UNREADABLE"
    assert result.value.phase == "data"
    assert result.value.field == "source.path"


def test_uri_like_basename_is_passed_as_a_local_absolute_file(tmp_path, monkeypatch):
    path = tmp_path / "https:places.geojson"
    frame = gpd.GeoDataFrame(geometry=[Point(117, 40)], crs="EPSG:4326")
    pyogrio.write_dataframe(frame, path, driver="GeoJSON")
    actual_read_info = pyogrio.read_info

    def verify_local_path(filename, **options):
        assert Path(filename) == path
        return actual_read_info(filename, **options)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pyogrio, "read_info", verify_local_path)
    assert read_vector_metadata(path.name).feature_count == 1


def test_virtual_archive_syntax_in_local_filename_is_rejected_before_opening(tmp_path, monkeypatch):
    path = tmp_path / "outside!places.geojson"
    path.touch()

    def unexpected_read(*args, **kwargs):
        pytest.fail("the ambiguous path must be rejected before it reaches GDAL")

    monkeypatch.setattr(pyogrio, "read_info", unexpected_read)
    with pytest.raises(SourceReadError) as result:
        read_vector_metadata(path)
    assert result.value.field == "source.path"
