"""Read vector step inputs and publish new GeoPackage layers."""

import os
from contextlib import ExitStack
from pathlib import Path

import pyogrio
from geopandas import GeoDataFrame
from pyogrio.errors import DataLayerError, DataSourceError
from pyproj import CRS

from geopilot.data.errors import SourceReadError
from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.vector_reader import local_ogr_path, read_layer_metadata
from geopilot.domain.data_asset import VectorMetadata
from geopilot.execution.output_staging import temporary_output_directory


def read_vector(reference: dict, *, role: str, require_source_id: bool = False) -> tuple[GeoDataFrame, str]:
    layer = reference.get("layer")
    try:
        path = local_ogr_path(reference["path"])
        if path.suffix.lower() == ".gpkg":
            layer, metadata = read_geopackage_metadata(path, layer)
        else:
            metadata = read_layer_metadata(path, layer)
        if not isinstance(metadata, VectorMetadata):
            raise ValueError(f"{role} requires a spatial vector layer")
        if metadata.crs is None:
            raise ValueError(f"{role}.crs requires a known CRS")
        frame = pyogrio.read_dataframe(path, layer=layer)
    except (SourceReadError, DataLayerError, DataSourceError) as error:
        raise ValueError(f"Could not read {role}. {error}") from error
    if (
        not isinstance(frame, GeoDataFrame) or len(frame) != metadata.feature_count or frame.crs is None
        or not frame.crs.equals(CRS.from_user_input(metadata.crs), ignore_axis_order=True)
    ):
        raise ValueError(f"{role} differs from its inspected vector metadata")

    if require_source_id:
        field = "source_feature_id"
        if field not in frame:
            raise ValueError(f"{role} requires {field}")
        ids = frame[field]
        if (ids.isna() | ids.astype(str).str.strip().eq("")).any():
            raise ValueError(f"{role}.{field} requires nonempty values")
    return frame, metadata.geometry_type


def _layer_options(frame: GeoDataFrame) -> dict[str, str]:
    used = {name.casefold() for name in frame.columns}
    options = {"FID": "geopilot_fid", "GEOMETRY_NAME": "geopilot_geometry"}
    for option, name in options.items():
        while name.casefold() in used:
            name += "_"
        options[option] = name
    return options


def write_vector_outputs(outputs: dict, layers: dict[str, tuple[GeoDataFrame, str]]) -> None:
    """Publish all outputs after writing them, preserving any pre-existing files."""
    with ExitStack() as staging:
        staged = []
        for role, (frame, geometry_type) in layers.items():
            reference = outputs[role]
            target = Path(reference["path"])
            if target.suffix.lower() != ".gpkg":
                raise ValueError(f"{role} output requires a .gpkg path")
            directory = staging.enter_context(temporary_output_directory(target, prefix=".vector-step-"))
            temporary = Path(directory) / "layer.gpkg"
            pyogrio.write_dataframe(
                frame, temporary, layer=reference.get("layer") or role, driver="GPKG", geometry_type=geometry_type,
                promote_to_multi=False, layer_options=_layer_options(frame),
            )
            staged.append((temporary, target))

        with ExitStack() as rollback:
            for temporary, target in staged:
                os.link(temporary, target)
                rollback.callback(target.unlink, missing_ok=True)
            rollback.pop_all()
