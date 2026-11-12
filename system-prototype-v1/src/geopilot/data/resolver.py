"""Reopen and verify one recorded asset version using its exact reference."""

from pathlib import Path

from geopilot.data.assets import InputChangedError, inspect_local_source
from geopilot.domain.data_asset import DataAsset, DataAssetRef
from geopilot.persistence.asset_repository import asset_for_ref
from geopilot.persistence.schema import open_database


def resolve_asset(database_path: str | Path, ref: DataAssetRef) -> DataAsset:
    """Read the recorded version and verify its current local source."""
    with open_database(database_path) as connection:
        asset = asset_for_ref(connection, ref)
    if asset is None:
        raise LookupError(f"Asset {ref.asset_id!r} version {ref.asset_version} does not exist")

    inspected = inspect_local_source(asset.source.path, layer=asset.source.layer)
    if inspected.fingerprint != asset.fingerprint:
        raise InputChangedError(
            f"Source content differs from asset {asset.id!r} version {asset.version}: {asset.source.path}"
        )
    if inspected.source.layer != asset.source.layer or inspected.metadata != asset.metadata:
        raise InputChangedError(
            f"Source metadata differs from asset {asset.id!r} version {asset.version}: {asset.source.path}"
        )
    return asset
