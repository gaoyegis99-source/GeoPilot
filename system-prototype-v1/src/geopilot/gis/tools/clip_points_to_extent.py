"""Retain points covered by the analysis extent and save their original features."""

import os
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

import geopandas as gpd
import pyogrio
import shapely
from pyproj import CRS

from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.vector_reader import local_ogr_path, read_layer_metadata
from geopilot.domain.data_asset import VectorMetadata
from geopilot.gis.tools.tool_command import run_tool_command


def _read_vector(reference: dict, *, role: str, kinds: set[str]) -> tuple[gpd.GeoDataFrame, str]:
    path = local_ogr_path(reference["path"])
    layer = reference.get("layer")
    if path.suffix.lower() == ".gpkg":
        layer, metadata = read_geopackage_metadata(path, layer)
    else:
        metadata = read_layer_metadata(path, layer)
    expected = ", ".join(sorted(kinds))
    if not isinstance(metadata, VectorMetadata) or metadata.geometry_type.removesuffix(" Z") not in kinds:
        raise ValueError(f"{role} requires a {expected} layer")
    if metadata.crs is None:
        raise ValueError(f"{role} CRS is unknown")

    frame = pyogrio.read_dataframe(path, layer=layer)
    if (
        not isinstance(frame, gpd.GeoDataFrame)
        or frame.crs is None
        or not frame.crs.equals(CRS.from_user_input(metadata.crs), ignore_axis_order=True)
        or len(frame) != metadata.feature_count
    ):
        raise ValueError(f"{role} layer differs from its inspected metadata")
    geometry = frame.geometry
    if not geometry.geom_type.isin(kinds).all() or geometry.is_empty.any() or not geometry.is_valid.all():
        raise ValueError(f"{role} contains empty, invalid, or unexpected geometries")
    return frame, metadata.geometry_type


def _write_outputs(points: gpd.GeoDataFrame, outputs: dict, geometry_type: str) -> None:
    used_names = {name.casefold() for name in points.columns}
    layer_options = {"FID": "geopilot_fid", "GEOMETRY_NAME": "geopilot_geometry"}
    for option, name in layer_options.items():
        while name.casefold() in used_names:
            name += "_"
        layer_options[option] = name

    with ExitStack() as staging:
        staged = []
        for role, reference in outputs.items():
            target = Path(reference["path"])
            if target.suffix.lower() != ".gpkg":
                raise ValueError(f"{role} output requires a .gpkg path")
            directory = staging.enter_context(TemporaryDirectory(prefix=".geopilot-clip-", dir=target.parent))
            temporary = Path(directory) / "points.gpkg"
            pyogrio.write_dataframe(
                points, temporary, driver="GPKG", layer=reference.get("layer") or role,
                geometry_type=geometry_type, layer_options=layer_options,
            )
            staged.append((temporary, target))

        # Publish only after both datasets were written, with exclusive file creation.
        with ExitStack() as rollback:
            for temporary, target in staged:
                os.link(temporary, target)
                rollback.callback(target.unlink)
            rollback.pop_all()


def clip_points(request: dict) -> dict:
    if request["parameters"]["output_scope"] != "clipped_to_analysis_extent":
        raise ValueError("output_scope must be clipped_to_analysis_extent")

    points, geometry_type = _read_vector(request["inputs"]["source"], role="source", kinds={"Point"})
    extent, _ = _read_vector(request["inputs"]["extent"], role="extent", kinds={"Polygon", "MultiPolygon"})
    if not points.crs.equals(extent.crs, ignore_axis_order=True):
        raise ValueError("source and extent must use the same CRS")
    if "source_feature_id" not in points:
        raise ValueError("source requires a source_feature_id field")
    identifiers = points["source_feature_id"]
    if identifiers.isna().any() or identifiers.astype(str).str.strip().eq("").any():
        raise ValueError("source_feature_id values must be nonempty")

    coverage = shapely.union_all(extent.geometry.to_numpy())
    shapely.prepare(coverage)
    selected = points.loc[shapely.covers(coverage, points.geometry.to_numpy())]
    outputs = request["output_targets"]
    _write_outputs(selected, outputs, geometry_type)
    print(f"Selected {len(selected)} of {len(points)} point(s) covered by the analysis extent")
    return outputs


if __name__ == "__main__":
    raise SystemExit(run_tool_command(clip_points, description=__doc__))
