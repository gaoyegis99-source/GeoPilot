from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest
from shapely.geometry import LineString

from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.gis.measurement.coverage import MeasurementDecision, MeasurementPolicyError
from geopilot.gis.measurement.distance import validate_distance


ROADS = Path(__file__).resolve().parents[1] / "fixtures/vector/road-buffer.gpkg"


def _write_road(tmp_path, crs, coordinates):
    path = tmp_path / "roads.gpkg"
    roads = gpd.GeoDataFrame(geometry=[LineString(coordinates)], crs=4326).to_crs(crs)
    pyogrio.write_dataframe(roads, path, layer="roads")
    _, metadata = read_geopackage_metadata(path, layer="roads")
    return metadata


@pytest.mark.parametrize(("crs", "message"), [(4326, "angular"), (3857, "Web Mercator")])
def test_geographic_and_web_mercator_roads_reject_planar_distance(tmp_path, crs, message):
    metadata = _write_road(tmp_path, crs, [(117, 40), (117.01, 40)])

    with pytest.raises(MeasurementPolicyError, match=message) as caught:
        validate_distance(metadata, field="inputs.roads.crs")

    assert caught.value.code == "MEASUREMENT_POLICY_REJECTED"
    assert caught.value.field == "inputs.roads.crs"
    assert caught.value.observed == f"EPSG:{crs}"


def test_local_projected_roads_return_measured_crs_and_unit():
    _, metadata = read_geopackage_metadata(ROADS, layer="roads")

    assert validate_distance(metadata) == MeasurementDecision("local_projected", "EPSG:32650", "meter")


def test_projected_roads_outside_utm_zone_reject_planar_distance(tmp_path):
    metadata = _write_road(tmp_path, 32650, [(123, 40), (123.01, 40)])

    with pytest.raises(MeasurementPolicyError, match="outside the area of use"):
        validate_distance(metadata)


def test_metre_projection_with_high_scale_distortion_is_rejected(tmp_path):
    metadata = _write_road(tmp_path, 3395, [(117, 60), (117.01, 60)])

    with pytest.raises(MeasurementPolicyError, match="linear distortion") as caught:
        validate_distance(metadata)

    assert caught.value.observed > 0.9


def test_geodesic_is_explicitly_unavailable():
    _, metadata = read_geopackage_metadata(ROADS, layer="roads")

    with pytest.raises(MeasurementPolicyError, match="Geodesic distance is unavailable"):
        validate_distance(metadata, method="geodesic")
