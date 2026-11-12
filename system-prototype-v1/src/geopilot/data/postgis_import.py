"""Export a selected PostGIS layer as a fingerprinted local GeoPackage."""

import json
import math
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from uuid import uuid4

import pandas as pd
import pyogrio
import shapely
from geopandas import GeoDataFrame, GeoSeries
from pyogrio.util import vsi_path
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.errors import GEOSException

from geopilot.data.assets import InputChangedError, inspect_local_source
from geopilot.data.errors import SourceReadError
from geopilot.data.fingerprint import capture_file_state
from geopilot.data.vector_reader import local_ogr_path
from geopilot.domain.data_asset import AssetAvailability, DataAsset, SourceType, VectorMetadata
from geopilot.persistence.asset_repository import asset_for_ref, insert_asset, latest_asset_for_source
from geopilot.persistence.schema import open_database


_ORIGIN_METADATA = "GEOPILOT_POSTGIS_ORIGIN"
_DIALECT = {
    "int2": "Int16", "int4": "Int32", "int8": "Int64", "bool": "boolean", "float4": "float32",
    "float8": "float64", "text": "object", "varchar": "object", "bpchar": "object", "uuid": "object",
}
_GEOMETRY_TYPES = {
    name.casefold(): name for name in (
        "Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon", "GeometryCollection",
    )
}
_FilterValue = str | int | float | bool | None


def _identifier(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise SourceReadError("Provide a nonempty identifier.", field=field_name)


@dataclass(frozen=True, slots=True)
class PostGISSource:
    """The local alias and reproducible selection, independent of credentials."""

    connection_alias: str
    schema: str
    table: str
    geometry_column: str = "geom"
    fields: tuple[str, ...] | None = None
    filters: Mapping[str, _FilterValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        alias = self.connection_alias
        if (
            type(alias) is not str or not alias or not alias[0].isalnum()
            or any(not (char.isalnum() or char in "_.-") for char in alias)
        ):
            raise SourceReadError("Provide a local connection alias.", field="source.connection_alias")
        for name in ("schema", "table", "geometry_column"):
            _identifier(getattr(self, name), f"source.{name}")
        if self.fields is not None:
            if not isinstance(self.fields, (tuple, list)):
                raise SourceReadError("Provide an ordered list of selected fields.", field="source.fields")
            for name in self.fields:
                _identifier(name, "source.fields")
            if len(set(self.fields)) != len(self.fields) or self.geometry_column in self.fields:
                raise SourceReadError("Select distinct attribute fields.", field="source.fields")
            object.__setattr__(self, "fields", tuple(self.fields))
        if not isinstance(self.filters, Mapping):
            raise SourceReadError("Provide an object of equality filters.", field="source.filters")
        filters = dict(self.filters)
        for name, value in filters.items():
            _identifier(name, "source.filters")
            if (
                type(value) not in {type(None), str, int, float, bool}
                or (type(value) is float and not math.isfinite(value))
                or (type(value) is str and "\x00" in value)
            ):
                raise SourceReadError("Filters require finite scalar values or null.", field="source.filters")
        object.__setattr__(self, "filters", MappingProxyType(dict(sorted(filters.items()))))

    def _selection(self) -> dict:
        return {
            "connection_alias": self.connection_alias, "schema": self.schema, "table": self.table,
            "geometry_column": self.geometry_column, "fields": self.fields, "filters": dict(self.filters),
        }

    @property
    def source_key(self) -> str:
        return json.dumps(
            [SourceType.POSTGIS_SNAPSHOT.value, self._selection()],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )


@dataclass(frozen=True, slots=True)
class PostGISSnapshotOrigin:
    source: PostGISSource
    exported_at: datetime
    srid: int

    def _document(self) -> str:
        return json.dumps(
            {**self.source._selection(), "exported_at": self.exported_at.isoformat(), "srid": self.srid},
            ensure_ascii=False, sort_keys=True, allow_nan=False,
        )


@dataclass(frozen=True, slots=True)
class PostGISSnapshot:
    path: Path
    layer: str
    origin: PostGISSnapshotOrigin
    fingerprint: str


def _selection_columns(cursor, source: PostGISSource, extension: str) -> tuple[list[tuple[str, str]], int]:
    columns = cursor.execute(
        """
        SELECT a.attname, n_type.nspname, t.typname, a.atttypmod
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
        JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
        JOIN pg_catalog.pg_type t ON a.atttypid = t.oid
        JOIN pg_catalog.pg_namespace n_type ON t.typnamespace = n_type.oid
        WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r','p','v','m')
          AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (source.schema, source.table),
    ).fetchall()
    if not columns:
        raise SourceReadError("The selected PostGIS table or view is unavailable.", field="source.table")
    by_name = {name: (namespace, kind, modifier) for name, namespace, kind, modifier in columns}
    geometry = by_name.get(source.geometry_column)
    if geometry is None or geometry[:2] != (extension, "geometry"):
        raise SourceReadError("Select a PostGIS geometry column.", field="source.geometry_column")
    attributes = {
        name: (namespace, kind) for name, namespace, kind, _ in columns
        if (namespace, kind) not in {(extension, "geometry"), (extension, "geography")}
    }
    fields = tuple(attributes) if source.fields is None else source.fields
    if len({name.casefold() for name in fields}) != len(fields):
        raise SourceReadError("GeoPackage attribute names must be distinct ignoring case.", field="source.fields")
    selected = []
    for name in fields:
        attribute = attributes.get(name)
        if attribute is None or attribute[0] != "pg_catalog" or attribute[1] not in _DIALECT:
            raise SourceReadError(
                f"Choose a supported scalar PostGIS attribute. Field {name!r} is unavailable or unsupported.",
                field="source.fields",
            )
        selected.append((name, attribute[1]))
    for name in source.filters:
        attribute = attributes.get(name)
        if attribute is None or attribute[0] != "pg_catalog" or attribute[1] not in _DIALECT:
            raise SourceReadError("A filter refers to an unavailable or unsupported field.", field="source.filters")
    return selected, geometry[2]


def _shape_details(cursor, extension: str, modifier: int, ewkb: list) -> tuple[object, str, int, CRS | None]:
    from psycopg import sql

    declared_srid, declared_type = cursor.execute(
        sql.SQL("SELECT {}(%s), {}(%s)").format(
            sql.Identifier(extension, "postgis_typmod_srid"), sql.Identifier(extension, "postgis_typmod_type"),
        ),
        (modifier, modifier),
    ).fetchone()
    try:
        shapes = shapely.from_wkb(ewkb)
    except (GEOSException, TypeError, ValueError):
        raise SourceReadError("A selected geometry cannot be decoded as EWKB.", field="source.geometry_column") from None
    actual = [shape for shape in shapes if shape is not None]
    if any(shape.has_z or shape.has_m for shape in actual) or declared_type.casefold().endswith(("z", "m")):
        raise SourceReadError("The selected geometry column requires 2D features.", field="source.geometry_column")
    types = {shape.geom_type for shape in actual}
    srids = {int(shapely.get_srid(shape)) for shape in actual}
    if len(srids) > 1 or (declared_srid > 0 and srids and srids != {declared_srid}):
        raise SourceReadError("The selected layer contains inconsistent SRID values.", field="source.geometry_column")
    srid = next(iter(srids), max(declared_srid, 0))
    geometry_type = (
        next(iter(types)) if len(types) == 1
        else _GEOMETRY_TYPES.get(declared_type.casefold(), "Unknown") if not types else "Unknown"
    )
    crs = None
    if srid > 0:
        row = cursor.execute(
            sql.SQL("SELECT srtext FROM {} WHERE srid = %s").format(sql.Identifier(extension, "spatial_ref_sys")),
            (srid,),
        ).fetchone()
        if row and row[0]:
            try:
                crs = CRS.from_wkt(row[0])
            except CRSError:
                pass
    return shapes, geometry_type, srid, crs


def _fresh_column(preferred: str, used: set[str]) -> str:
    while preferred.casefold() in used:
        preferred += "_"
    return preferred


def _read_postgis(conninfo: str, source: PostGISSource) -> tuple[GeoDataFrame, str, PostGISSnapshotOrigin]:
    try:
        import psycopg
        from psycopg import sql
    except ImportError:
        raise SourceReadError("PostGIS import requires the optional Psycopg driver.", field="source.connection") from None
    try:
        connection = psycopg.connect(conninfo, connect_timeout=5, application_name="geopilot_postgis_snapshot")
    except (psycopg.Error, TypeError, ValueError):
        raise SourceReadError("Could not open the selected PostGIS connection.", field="source.connection") from None
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SET LOCAL search_path TO pg_catalog")
            exported_at = cursor.execute("SELECT transaction_timestamp()").fetchone()[0].astimezone(UTC)
            row = cursor.execute(
                """
                SELECT n.nspname FROM pg_catalog.pg_extension e
                JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'postgis'
                """
            ).fetchone()
            if row is None:
                raise SourceReadError("The selected database does not have PostGIS.", field="source.connection")
            extension = row[0]
            fields, modifier = _selection_columns(cursor, source, extension)
            geometry = sql.SQL("{}({})").format(
                sql.Identifier(extension, "st_asewkb"), sql.Identifier(source.geometry_column),
            )
            select = sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join([*(sql.Identifier(name) for name, _ in fields), geometry]),
                sql.Identifier(source.schema, source.table),
            )
            if source.filters:
                select += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(
                    sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(name)) for name in source.filters
                )
            rows = cursor.execute(select, tuple(source.filters.values())).fetchall()
            shapes, geometry_type, srid, crs = _shape_details(cursor, extension, modifier, [row[-1] for row in rows])
    except psycopg.Error:
        raise SourceReadError("Could not read the selected PostGIS layer or filters.", field="source.table") from None

    columns = {}
    for index, (name, kind) in enumerate(fields):
        values = [row[index] for row in rows]
        if kind.startswith("float") and any(value is not None and not math.isfinite(value) for value in values):
            raise SourceReadError("GeoPackage requires finite floating-point attributes.", field="source.fields")
        if kind == "uuid":
            values = [str(value) if value is not None else None for value in values]
        columns[name] = pd.array(values, dtype=_DIALECT[kind])
    geometry_name = _fresh_column("geopilot_geometry", {name.casefold() for name in columns})
    columns[geometry_name] = GeoSeries(shapes, crs=crs)
    return GeoDataFrame(columns, geometry=geometry_name, crs=crs), geometry_type, PostGISSnapshotOrigin(
        source, exported_at, srid,
    )


def read_postgis_snapshot_origin(path: str | Path) -> PostGISSnapshotOrigin:
    """Read the provenance embedded in the same GeoPackage as the vector data."""
    try:
        info = pyogrio.read_info(local_ogr_path(path))
        metadata = info["layer_metadata"]
        if info["driver"] != "GPKG" or metadata is None:
            raise ValueError("Expected GeoPackage snapshot provenance")
        document = json.loads(metadata[_ORIGIN_METADATA])
        exported_at = datetime.fromisoformat(document.pop("exported_at"))
        srid = document.pop("srid")
        if exported_at.utcoffset() is None or type(srid) is not int or srid < 0:
            raise ValueError("Invalid snapshot time or SRID")
        return PostGISSnapshotOrigin(PostGISSource(**document), exported_at.astimezone(UTC), srid)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        raise SourceReadError("Cannot read the PostGIS snapshot origin.", field="source.origin") from None


def _destination(path: str | Path, layer: str) -> Path:
    _identifier(layer, "source.layer")
    if layer.lower().startswith(("gpkg_", "sqlite_")):
        raise SourceReadError("Select a GeoPackage layer name outside the reserved namespaces.", field="source.layer")
    target = Path(path).expanduser()
    target = target.parent.resolve(strict=True) / target.name
    if target.suffix.lower() != ".gpkg" or target.as_posix().startswith("/vsi") or vsi_path(target) != str(target):
        raise SourceReadError("Choose a local GeoPackage snapshot destination.")
    _require_unused(target)
    return target


def _require_unused(path: Path) -> None:
    sidecars = ("-wal", "-shm", "-journal", ".aux.xml")
    for candidate in (path, *(path.with_name(path.name + suffix) for suffix in sidecars)):
        if candidate.exists() or candidate.is_symlink():
            raise FileExistsError(f"Snapshot destination already exists: {candidate}")


def _write_verified_snapshot(
    path: Path, layer: str, frame: GeoDataFrame, geometry_type: str, origin: PostGISSnapshotOrigin,
) -> str:
    used = {name.casefold() for name in frame.columns}
    try:
        pyogrio.write_dataframe(
            frame, path, layer=layer, driver="GPKG", geometry_type=geometry_type, promote_to_multi=False,
            layer_metadata={_ORIGIN_METADATA: origin._document()},
            layer_options={"FID": _fresh_column("geopilot_fid", used), "GEOMETRY_NAME": frame.geometry.name},
        )
        inspected = inspect_local_source(path, layer=layer)
        reopened = pyogrio.read_dataframe(path, layer=layer, columns=[])
    except (OSError, RuntimeError, ValueError):
        raise SourceReadError("Could not write and reopen the GeoPackage snapshot.") from None
    metadata = inspected.metadata
    if (
        not isinstance(metadata, VectorMetadata)
        or metadata.feature_count != len(frame)
        or metadata.geometry_type != geometry_type
    ):
        raise SourceReadError("The reopened GeoPackage does not match the selected feature count or geometry type.")
    same_crs = (
        metadata.crs is None if frame.crs is None
        else metadata.crs is not None and frame.crs.equals(CRS.from_user_input(metadata.crs), ignore_axis_order=True)
    )
    if not same_crs or reopened.geometry.to_wkb().tolist() != frame.geometry.to_wkb().tolist():
        raise SourceReadError("The reopened GeoPackage differs from the selected CRS or geometry.")
    if read_postgis_snapshot_origin(path) != origin or any(entry != path for entry in path.parent.iterdir()):
        raise SourceReadError("The GeoPackage did not close with its embedded snapshot origin.")
    return inspected.fingerprint


@contextmanager
def _stage_snapshot(
    target: Path, *, conninfo: str, source: PostGISSource, layer: str,
) -> Iterator[PostGISSnapshot]:
    frame, geometry_type, origin = _read_postgis(conninfo, source)
    with TemporaryDirectory(prefix=".postgis-export-", dir=target.parent) as directory:
        staged = Path(directory) / "snapshot.gpkg"
        fingerprint = _write_verified_snapshot(staged, layer, frame, geometry_type, origin)
        yield PostGISSnapshot(staged, layer, origin, fingerprint)


@contextmanager
def _publish_snapshot(staged: PostGISSnapshot, target: Path) -> Iterator[PostGISSnapshot]:
    identity = staged.path.stat()
    _require_unused(target)
    try:
        os.link(staged.path, target, follow_symlinks=False)
        if not os.path.samestat(identity, target.lstat()):
            raise InputChangedError("The PostGIS snapshot changed while publishing.")
        yield replace(staged, path=target)
    except BaseException as error:
        try:
            if os.path.samestat(identity, target.lstat()):
                target.unlink()
        except FileNotFoundError:
            pass
        except OSError as cleanup:
            error.add_note(f"Could not remove an unregistered PostGIS snapshot: {cleanup}")
        raise


def export_postgis_snapshot(
    destination: str | Path, *, conninfo: str, source: PostGISSource, layer: str = "snapshot",
) -> PostGISSnapshot:
    """Export one consistent PostGIS selection to a new, verified local file."""
    target = _destination(destination, layer)
    with (
        _stage_snapshot(target, conninfo=conninfo, source=source, layer=layer) as staged,
        _publish_snapshot(staged, target) as snapshot,
    ):
        return snapshot


def _check_registration(
    connection: sqlite3.Connection, source_key: str, target: Path, *, refresh: bool,
) -> DataAsset | None:
    previous = latest_asset_for_source(connection, source_key)
    if refresh and previous is None:
        raise SourceReadError("This selection has no registered snapshot to refresh.", field="source.selection")
    if not refresh and previous is not None:
        raise SourceReadError("This selection already has a registered snapshot.", field="source.selection")
    if connection.execute(
        "SELECT 1 FROM data_asset WHERE json_extract(source_json, '$.path') = ? LIMIT 1", (str(target),),
    ).fetchone() is not None:
        raise FileExistsError(f"Snapshot destination is already registered: {target}")
    return previous


def _register_snapshot(
    database_path: str | Path, destination: str | Path, *, conninfo: str, source: PostGISSource,
    layer: str, refresh: bool,
) -> DataAsset:
    target = _destination(destination, layer)
    source_key = source.source_key
    with open_database(database_path, read_only=True) as connection:
        _check_registration(connection, source_key, target, refresh=refresh)
    with _stage_snapshot(target, conninfo=conninfo, source=source, layer=layer) as staged:
        inspected = inspect_local_source(staged.path, layer=staged.layer)
        with open_database(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = _check_registration(connection, source_key, target, refresh=refresh)
            if inspected.fingerprint != staged.fingerprint or capture_file_state(staged.path) != inspected.state:
                raise InputChangedError("The PostGIS snapshot changed before registration.")
            # Keep publication open through commit so a rejected insert also withdraws the file.
            with _publish_snapshot(staged, target) as snapshot, connection:
                state = capture_file_state(snapshot.path)
                asset = DataAsset(
                    id=previous.id if previous is not None else uuid4().hex,
                    version=previous.version + 1 if previous is not None else 1,
                    source_type=SourceType.POSTGIS_SNAPSHOT,
                    source=inspected.source.model_copy(update={"path": snapshot.path}),
                    metadata=inspected.metadata, fingerprint=inspected.fingerprint,
                    availability=AssetAvailability.AVAILABLE, created_at=datetime.now(UTC),
                )
                insert_asset(connection, source_key, asset)
                if asset_for_ref(connection, asset.ref) != asset:
                    raise sqlite3.IntegrityError("The PostGIS snapshot version was not registered.")
                if capture_file_state(snapshot.path) != state:
                    raise InputChangedError("The PostGIS snapshot changed during registration.")
                return asset


def import_postgis_snapshot(
    database_path: str | Path, destination: str | Path, *, conninfo: str, source: PostGISSource,
    layer: str = "snapshot",
) -> DataAsset:
    """Register the first snapshot for a logical PostGIS selection."""
    return _register_snapshot(database_path, destination, conninfo=conninfo, source=source, layer=layer, refresh=False)


def refresh_postgis_snapshot(
    database_path: str | Path, destination: str | Path, *, conninfo: str, source: PostGISSource,
    layer: str = "snapshot",
) -> DataAsset:
    """Export an existing logical selection to a new file and append its asset version."""
    return _register_snapshot(database_path, destination, conninfo=conninfo, source=source, layer=layer, refresh=True)
