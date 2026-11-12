"""Build WGS84 and projected road layers from equatorial reference coordinates."""

import argparse
from math import degrees
from pathlib import Path
from tempfile import TemporaryDirectory

import geopandas as gpd
import pyogrio
from shapely.geometry import LineString, Point, Polygon, box


WGS84_SEMI_MAJOR_M = 6_378_137.0
WGS84_INVERSE_FLATTENING = 298.257223563
ORIGIN_LONGITUDE = 3.0
DISTANCE_M = 500
UTM_CRS = "EPSG:32631"
LOCAL_CRS = "EPSG:3395"
ROLES = ("analysis_extent", "roads", "targets", "zones")
_FLATTENING = 1 / WGS84_INVERSE_FLATTENING
_MERIDIONAL_RADIUS_AT_EQUATOR_M = WGS84_SEMI_MAJOR_M * (1 - _FLATTENING) ** 2


def _point(east_m, north_m):
    # Equatorial eastings are exact ellipsoidal arc lengths. For these latitudes,
    # linearizing meridional arc length at the equator changes northings by < 1 µm.
    return (
        ORIGIN_LONGITUDE + degrees(east_m / WGS84_SEMI_MAJOR_M),
        degrees(north_m / _MERIDIONAL_RADIUS_AT_EQUATOR_M),
    )


def _frame(features):
    return gpd.GeoDataFrame(
        {"source_feature_id": list(features)}, geometry=list(features.values()), crs="EPSG:4326",
    )


def _layers():
    return {
        "analysis_extent": _frame({"extent": box(*_point(-600, -300), *_point(1600, 700))}),
        "roads": _frame({
            "road-main": LineString([_point(0, 0), _point(1000, 0)]),
            "road-near": LineString([_point(700, 1000), _point(1000, 1000)]),
            "road-far": LineString([_point(1300, 1350), _point(1600, 1350)]),
        }),
        "targets": _frame({
            name: Point(_point(x, y)) for name, x, y in (
                ("point-cap", -500, 0), ("point-cap-inside", -499, 0), ("point-cap-outside", -501, 0),
                ("point-west", 100, -100), ("point-east", 900, 100), ("point-shared-zone", 500, 0),
                ("point-near-road", 800, 620), ("point-outside-aoi", 800, 900), ("point-far-road", 1400, 680),
            )
        }),
        "zones": _frame({
            # The shared edge has a vertex at the target, which stays on the edge after projection.
            "zone-west": Polygon([_point(-600, -300), _point(500, -300), _point(500, 0),
                                  _point(500, 700), _point(-600, 700)]),
            "zone-east": Polygon([_point(500, -300), _point(1600, -300), _point(1600, 700),
                                  _point(500, 700), _point(500, 0)]),
        }),
    }


def write_road_workflow_fixture(destination: Path) -> Path:
    """Write a new GeoPackage with three representations of each layer."""
    destination = Path(destination)
    if destination.suffix.lower() != ".gpkg":
        raise ValueError("The road workflow fixture requires a .gpkg output path")
    with TemporaryDirectory(prefix=".geodesic-road-fixture-", dir=destination.parent) as directory:
        staged = Path(directory) / destination.name
        for role, frame in _layers().items():
            pyogrio.write_dataframe(frame, staged, layer=role, driver="GPKG")
            pyogrio.write_dataframe(frame.to_crs(UTM_CRS), staged, layer=f"{role}_utm", driver="GPKG")
            # The AOI crosses the equator, so its local metre comparison uses ellipsoidal Mercator.
            pyogrio.write_dataframe(frame.to_crs(LOCAL_CRS), staged, layer=f"{role}_mercator", driver="GPKG")
        destination.hardlink_to(staged)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    print(write_road_workflow_fixture(parser.parse_args().destination))
