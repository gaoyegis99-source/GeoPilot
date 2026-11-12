"""Persist asset versions through a caller-owned SQLite transaction."""

import json
import sqlite3

from geopilot.domain.data_asset import DataAsset, DataAssetRef


def _asset_from_row(row: sqlite3.Row) -> DataAsset:
    return DataAsset.model_validate({
        "id": row["id"],
        "version": row["version"],
        "source_type": row["source_type"],
        "source": json.loads(row["source_json"]),
        "metadata": json.loads(row["metadata_json"]),
        "fingerprint": row["fingerprint"],
        "availability": row["availability"],
        "created_at": row["created_at"],
    })


def latest_asset_for_source(connection: sqlite3.Connection, source_key: str) -> DataAsset | None:
    row = connection.execute(
        "SELECT * FROM data_asset WHERE source_key = ? ORDER BY version DESC LIMIT 1",
        (source_key,),
    ).fetchone()
    return None if row is None else _asset_from_row(row)


def asset_for_ref(connection: sqlite3.Connection, ref: DataAssetRef) -> DataAsset | None:
    row = connection.execute(
        "SELECT * FROM data_asset WHERE id = ? AND version = ?",
        (ref.asset_id, ref.asset_version),
    ).fetchone()
    return None if row is None else _asset_from_row(row)


def list_assets(connection: sqlite3.Connection) -> tuple[DataAsset, ...]:
    rows = connection.execute("SELECT * FROM data_asset ORDER BY created_at DESC, id, version DESC")
    return tuple(_asset_from_row(row) for row in rows)


def insert_asset(connection: sqlite3.Connection, source_key: str, asset: DataAsset) -> None:
    record = asset.model_dump(mode="json", exclude={"source", "metadata"})
    record.update(
        source_key=source_key,
        source_json=asset.source.model_dump_json(),
        metadata_json=asset.metadata.model_dump_json(),
    )
    connection.execute(
        """
        INSERT INTO data_asset (
            id, version, source_key, source_type, source_json, fingerprint,
            metadata_json, availability, created_at
        ) VALUES (
            :id, :version, :source_key, :source_type, :source_json, :fingerprint,
            :metadata_json, :availability, :created_at
        )
        """,
        record,
    )
