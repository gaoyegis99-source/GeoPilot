"""Serialize a real local vector layer for MapLibre without altering the source."""

from pathlib import Path

import pyogrio
from fastapi import HTTPException, Response
from geopandas import GeoDataFrame
from pyogrio.errors import DataLayerError, DataSourceError
from pyproj.exceptions import ProjError
from shapely.errors import GEOSException


def vector_preview(path: Path, layer: str | None) -> Response:
    try:
        frame = pyogrio.read_dataframe(path, layer=layer)
        if not isinstance(frame, GeoDataFrame) or frame.crs is None:
            raise ValueError("A vector preview requires geometry and a known CRS.")
        document = frame.to_crs("EPSG:4326").to_json(na="null", drop_id=True, allow_nan=False)
    except (OSError, ValueError, TypeError, DataSourceError, DataLayerError, ProjError, GEOSException) as error:
        raise HTTPException(status_code=422, detail={
            "code": "PREVIEW_FAILED", "message": str(error), "field": "preview",
        }) from error
    return Response(content=document, media_type="application/geo+json")
