"""Synthetic product metadata and real small GeoTIFF/GeoPackage fixed samples."""

import runpy
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box


CRS = "EPSG:32632"
FIRST_GRID = from_origin(500000, 5100000, 30, 30)
SECOND_GRID = from_origin(499993, 5100007, 10, 10)
AREA_CRS = "EPSG:6933"
LAYER = "analysis_extent"
_FACTORIES = runpy.run_path(str(Path(__file__).parents[1] / "remote_sensing" / "product_fixtures.py"))


def write_mixed_products(directory, *, anomaly=False, unknown_role=None):
    """Write one paired native-grid sample and an AOI with a hole in first-grid cell (1, 3)."""
    directory.mkdir(parents=True, exist_ok=True)
    first = directory / "first-landsat.tif"
    second = directory / "second-sentinel.tif"
    aoi = directory / "analysis-extent.gpkg"

    landsat = np.zeros((4, 3, 5), dtype="uint16")
    landsat[1, :2] = 10000
    landsat[1, 0, 1:4] = 20000
    landsat[3] = 10000
    landsat[0, 0, 3] = 1 << 3  # Cloud in first date, still valid in second date.
    landsat_xml = (_FACTORIES["build_landsat_xml"](collection="03" if unknown_role == "first_product" else "02")
                   .replace("20241101", "20240701").replace("20241103", "20240703"))
    _FACTORIES["write_stack"](
        first, profile=_FACTORIES["LANDSAT"], xml=landsat_xml, codes=_FACTORIES["LANDSAT_CODES"],
        pixel_m=30, crs=CRS, values=landsat,
    )

    sentinel = np.empty((3, 10, 16), dtype="uint16")
    native_scl = np.full((5, 8), 4, dtype="uint16")
    native_scl[1, 7] = 9  # One native 20 m cloud cell covering first-grid center (0, 4).
    sentinel[0] = native_scl.repeat(2, axis=0).repeat(2, axis=1)
    sentinel[1].fill(4750)  # Calibrated NIR 0.375.
    sentinel[2].fill(2250)  # Calibrated red 0.125, hence NDVI 0.5.
    sentinel[1, 2, 8] = 2250  # NDVI 0 at first-grid cell (0, 2).
    sentinel[1, 5, 2], sentinel[2, 5, 2] = 1200, 800  # Exact zero denominator.
    sentinel[1, 5, 5], sentinel[2, 5, 5] = 1300, 800  # NDVI 5 when the input mask permits it.
    sentinel[2, 5, 8] = 0  # Original DN fill at first-grid cell (1, 2).
    sentinel[1, 5, 14] = 2250  # Valid unchanged cell (1, 4).
    sentinel_mask = np.full((10, 16), 255, dtype="uint8")
    if not anomaly:
        sentinel_mask[5, 5] = 0
    baseline = "05.12" if unknown_role == "second_product" else "05.11"
    sentinel_xml = _FACTORIES["build_sentinel_xml"](baseline=baseline).replace("N05.11", f"N{baseline}")
    _FACTORIES["write_stack"](
        second, profile=_FACTORIES["SENTINEL"], xml=sentinel_xml, codes=_FACTORIES["SENTINEL_CODES"],
        pixel_m=10, crs=CRS, values=sentinel, scl_resampling="nearest", shared_mask=sentinel_mask,
    )
    with rasterio.open(second, "r+") as dataset:
        dataset.transform = SECOND_GRID

    boundary = box(500000, 5099910, 500150, 5100000)
    hole = box(500091, 5099941, 500119, 5099969)
    geometry = Polygon(boundary.exterior.coords, [hole.exterior.coords])
    pyogrio.write_dataframe(gpd.GeoDataFrame(geometry=[geometry], crs=CRS), aoi, layer=LAYER, driver="GPKG")
    return {"first_product": (first, None), "second_product": (second, None), "analysis_extent": (aoi, LAYER)}
