from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import geopandas as gpd
import pyogrio
import pytest
from pyproj import CRS
from shapely.geometry import box

from geopilot.data.assets import InputChangedError
from geopilot.data.errors import SourceReadError
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.gis.measurement.coverage import MeasurementDecision, MeasurementPolicyError
from geopilot.gis.measurement.projection import ReprojectionResult, reproject_vector, select_measurement_crs
from geopilot.gis.validation.crs_validator import CRSValidationError


ROADS = Path(__file__).resolve().parents[1] / "fixtures" / "vector" / "road-buffer.gpkg"


def _write_roads(path, crs):
    original = pyogrio.read_dataframe(ROADS, layer="roads")
    pyogrio.write_dataframe(original.to_crs(crs), path, layer="roads", driver="GPKG")
    return original


def _write_parcel(path, bounds):
    frame = gpd.GeoDataFrame({"fid": ["parcel-A"]}, geometry=[box(*bounds)], crs="EPSG:4326")
    pyogrio.write_dataframe(frame, path, driver="GeoJSON")


def test_select_and_reproject_real_roads_to_unique_utm(tmp_path):
    source = tmp_path / "geographic.gpkg"
    destination = tmp_path / "utm.gpkg"
    original = _write_roads(source, 4326)
    _, source_metadata = read_geopackage_metadata(source)
    before = fingerprint_file(source)
    assert source_metadata.crs == "EPSG:4326"

    decision = select_measurement_crs(source_metadata, method="local_projected")
    assert decision == MeasurementDecision(method="local_projected", crs="EPSG:32650", unit="meter")
    result = reproject_vector(source, destination, method="local_projected")
    assert result == ReprojectionResult(path=destination, layer="projected", decision=decision)

    reopened = pyogrio.read_dataframe(destination, layer=result.layer)
    assert reopened.crs.to_epsg() == 32650
    assert list(reopened.source_feature_id) == list(original.source_feature_id)
    assert reopened.geom_type.tolist() == ["LineString"] * 3
    # The fixture started at exact UTM coordinates. Only floating-point reprojection error is allowed.
    assert reopened.geometry.hausdorff_distance(original.geometry).max() < 1e-6
    assert fingerprint_file(source) == before
    assert read_geopackage_metadata(source)[1].crs == "EPSG:4326"


def test_web_mercator_is_a_valid_reprojection_source_but_not_measurement_target(tmp_path):
    source = tmp_path / "display.gpkg"
    destination = tmp_path / "local.gpkg"
    original = _write_roads(source, 3857)
    before = fingerprint_file(source)
    _, metadata = read_geopackage_metadata(source)
    assert metadata.crs == "EPSG:3857"

    result = reproject_vector(
        source, destination, method="local_projected", target_crs=CRS.from_epsg(32650),
        output_layer="roads_local",
    )
    reopened = pyogrio.read_dataframe(destination, layer="roads_local")
    assert result.decision.crs == "EPSG:32650" and result.layer == "roads_local"
    assert reopened.crs.to_epsg() == 32650
    assert reopened.geometry.hausdorff_distance(original.geometry).max() < 1e-6

    refused = tmp_path / "refused.gpkg"
    with pytest.raises(MeasurementPolicyError):
        reproject_vector(source, refused, method="local_projected", target_crs="EPSG:3857")
    assert not refused.exists()
    assert fingerprint_file(source) == before


def test_area_projection_preserves_polygon_and_named_fid_attribute(tmp_path):
    source = tmp_path / "parcel.geojson"
    destination = tmp_path / "equal-area.gpkg"
    _write_parcel(source, (116, 39, 116.1, 39.1))
    before = fingerprint_file(source)

    result = reproject_vector(source, destination, method="equal_area")
    assert result.decision == MeasurementDecision(method="equal_area", crs="EPSG:6933", unit="square_meter")
    reopened = pyogrio.read_dataframe(destination, layer=result.layer)
    assert reopened.crs.to_epsg() == 6933
    assert reopened.geometry.iloc[0].is_valid and reopened.geom_type.tolist() == ["Polygon"]
    assert list(reopened["fid"]) == ["parcel-A"]
    assert fingerprint_file(source) == before


@pytest.mark.parametrize(
    "bounds,method,target",
    [
        ((119.9, 39, 120.1, 39.1), "local_projected", None),
        ((116, 39, 116.1, 39.1), "local_projected", "EPSG:32649"),
        ((0, 87, 0.1, 87.1), "equal_area", None),
        ((116, 39, 116.1, 39.1), "equal_area", "EPSG:32650"),
    ],
    ids=["no-unique-utm", "wrong-utm-zone", "outside-equal-area-coverage", "not-equal-area"],
)
def test_unsuitable_target_or_coverage_creates_no_file(tmp_path, bounds, method, target):
    source = tmp_path / "parcel.geojson"
    destination = tmp_path / "refused.gpkg"
    _write_parcel(source, bounds)
    before = fingerprint_file(source)

    with pytest.raises(MeasurementPolicyError) as caught:
        reproject_vector(source, destination, method=method, target_crs=target)
    assert caught.value.code == "MEASUREMENT_POLICY_REJECTED"
    assert caught.value.observed is not None
    assert not destination.exists()
    assert fingerprint_file(source) == before


def test_missing_crs_is_not_assigned_by_the_reprojection_tool(tmp_path):
    source = tmp_path / "unknown.gpkg"
    destination = tmp_path / "refused.gpkg"
    frame = gpd.GeoDataFrame(geometry=[box(500000, 4400000, 500100, 4400100)])
    with pytest.warns(UserWarning, match="crs.*was not provided"):
        pyogrio.write_dataframe(frame, source, layer="parcel", driver="GPKG")
    before = fingerprint_file(source)

    with pytest.raises(CRSValidationError) as caught:
        reproject_vector(source, destination, method="local_projected", target_crs="EPSG:32650")
    assert caught.value.code == "CRS_UNKNOWN"
    assert not destination.exists()
    assert fingerprint_file(source) == before


def test_existing_destination_and_alias_are_preserved(tmp_path):
    existing = tmp_path / "existing.gpkg"
    existing.write_bytes(b"keep this output")
    alias = tmp_path / "alias.gpkg"
    alias.symlink_to(ROADS)
    before = fingerprint_file(ROADS)

    for destination in (existing, alias, ROADS):
        with pytest.raises(FileExistsError):
            reproject_vector(ROADS, destination, method="local_projected", source_layer="roads")
    assert alias.is_symlink()
    assert existing.read_bytes() == b"keep this output"
    assert fingerprint_file(ROADS) == before


def test_multilayer_source_requires_explicit_layer(tmp_path):
    destination = tmp_path / "refused.gpkg"
    with pytest.raises(SourceReadError) as caught:
        reproject_vector(ROADS, destination, method="local_projected")
    assert caught.value.field == "source.layer"
    assert not destination.exists()


def test_two_reprojections_cannot_replace_one_published_output(tmp_path):
    destination = tmp_path / "shared.gpkg"
    gate = Barrier(2, timeout=5)

    def run():
        gate.wait()
        try:
            return reproject_vector(ROADS, destination, method="local_projected", source_layer="roads")
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]

    assert sum(isinstance(result, ReprojectionResult) for result in results) == 1
    assert read_geopackage_metadata(destination)[1].feature_count == 3
    assert list(tmp_path.iterdir()) == [destination]


def test_source_replacement_after_real_geometry_read_prevents_publication(tmp_path, monkeypatch):
    source = tmp_path / "source.gpkg"
    replacement = tmp_path / "replacement.gpkg"
    destination = tmp_path / "refused.gpkg"
    roads = _write_roads(source, 4326).to_crs(4326)
    roads.loc[0, "source_feature_id"] = "externally changed"
    pyogrio.write_dataframe(roads, replacement, layer="roads", driver="GPKG")
    changed = fingerprint_file(replacement).digest
    read_dataframe = pyogrio.read_dataframe

    def read_then_replace(path, *args, **kwargs):
        frame = read_dataframe(path, *args, **kwargs)
        if Path(path) == source and kwargs.get("read_geometry", True) and replacement.exists():
            replacement.replace(source)
        return frame

    monkeypatch.setattr(pyogrio, "read_dataframe", read_then_replace)
    with pytest.raises(InputChangedError):
        reproject_vector(source, destination, method="local_projected")
    assert fingerprint_file(source).digest == changed
    assert list(tmp_path.iterdir()) == [source]
