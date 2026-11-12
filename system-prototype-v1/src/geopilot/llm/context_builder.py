"""Build an allowlisted model summary from exact local asset versions."""

import re
import unicodedata
from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from geopilot.application.task_context import InputReadiness, TaskContext
from geopilot.domain.data_asset import (
    AssetAvailability,
    DataAsset,
    DataAssetRef,
    FieldMetadata,
    RasterMetadata,
    TableMetadata,
    VectorMetadata,
)


class OutboundContextError(ValueError):
    """The local context cannot be reduced to an outbound summary."""


class _Summary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


_FieldType = Literal["string", "integer", "number", "boolean", "datetime", "other"]
_Geometry = Literal[
    "Point", "MultiPoint", "LineString", "MultiLineString",
    "Polygon", "MultiPolygon", "GeometryCollection", "Other",
]


class FieldSummary(_Summary):
    name: str
    type: _FieldType
    missing_ratio: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None


class VectorSummary(_Summary):
    kind: Literal["vector"] = "vector"
    geometry_type: _Geometry
    feature_count: int
    fields: tuple[FieldSummary, ...]


class TableSummary(_Summary):
    kind: Literal["table"] = "table"
    row_count: int
    fields: tuple[FieldSummary, ...]


class RasterSummary(_Summary):
    kind: Literal["raster"] = "raster"
    width: int
    height: int
    band_count: int
    resolution_x: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    resolution_y: Annotated[float, Field(gt=0, allow_inf_nan=False)]


class RoleSummary(_Summary):
    role: str
    readiness: InputReadiness
    asset: Annotated[VectorSummary | TableSummary | RasterSummary, Field(discriminator="kind")] | None = None


class OutboundContext(_Summary):
    goal: str
    roles: tuple[RoleSummary, ...]


_REFERENCE = "（本机引用）"
_INVALID_GOAL = "Prepare the goal with local references before requesting model assistance."
_ROLE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_FIELD_NAME = re.compile(r"(?:[^\W\d]|_)[\w .()\-]{0,79}")
_GEOMETRY_TYPES = {
    "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection",
}
_NUMBER = r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
_COORDINATE_LIST = rf"{_NUMBER}(?:\s*[,，]\s*{_NUMBER}){{1,2}}"
_COORDINATES = re.compile(
    rf"(?:[\[(（]\s*{_COORDINATE_LIST}\s*[\])）]|(?<![\w.]){_COORDINATE_LIST}(?![\d.]))"
)
_COORDINATE_LABEL = re.compile(
    rf"(?:\b(?:latitude|longitude|easting|northing|lat|lon|lng)\b|经度|纬度)\s*[:：=]\s*{_NUMBER}",
    re.IGNORECASE,
)
_WKT = re.compile(
    r"\b(?:POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON|GEOMETRYCOLLECTION)"
    r"\s*(?:ZM|Z|M)?\s*(?:\(|EMPTY\b)",
    re.IGNORECASE,
)
_URI = re.compile(r"""[A-Za-z][A-Za-z0-9+.-]*://[^\s"'<>，。；！？]+""")
_QUOTED_PATH = re.compile(
    r"""(?P<quote>["'])(?:[A-Za-z]:[\\/]|\\\\|~?/|\.\.?[\\/])(?:(?!(?P=quote))[^\r\n])+(?P=quote)"""
)
_PATH = re.compile(r"""(?<![A-Za-z0-9_.])(?:[A-Za-z]:[\\/]|\\\\|~?/|\.\.?[\\/])[^\s"'<>，。；！？]+""")
_RELATIVE_FILE = re.compile(r"(?<![\w.])(?:[\w.-]+[\\/])+[\w.-]+\.[A-Za-z0-9]{1,12}(?![\w.])")
_FILENAME = re.compile(
    r"(?<![\w.])[\w.-]+\.(?:gpkg|geojson|shp|tiff?|cog|csv|sqlite3?|db|json|zip|kml|wkt)(?![\w.])",
    re.IGNORECASE,
)
_CONNECTION = re.compile(
    r"""\b(?:host|hostaddr|dbname|database|port|user|username|password|passfile|sslkey|"""
    r"""api[_-]?key|access[_-]?token|token|secret)\s*=\s*"""
    r"""(?:"[^"]*"|'[^']*'|[^\s,，;；]+)""",
    re.IGNORECASE,
)


def _clean_goal(summary: str, protected_literals: Iterable[str]) -> str:
    if any(unicodedata.category(char).startswith("C") and char not in "\t\r\n" for char in summary):
        raise OutboundContextError(_INVALID_GOAL)
    text = unicodedata.normalize("NFC", summary)
    literals = {unicodedata.normalize("NFC", literal) for literal in protected_literals if literal}
    if literals:
        ordered = sorted(literals, key=lambda literal: (-len(literal), literal))
        text = re.sub("|".join(re.escape(literal) for literal in ordered), _REFERENCE, text)
    for pattern in (_QUOTED_PATH, _URI, _PATH, _RELATIVE_FILE, _FILENAME, _CONNECTION, _COORDINATE_LABEL, _COORDINATES):
        text = pattern.sub(_REFERENCE, text)
    text = " ".join(text.split())
    if not text or len(text) > 1200 or _WKT.search(text) or any(char in text for char in "{}[]"):
        raise OutboundContextError(_INVALID_GOAL)
    return text


def _field_type(dtype: str) -> _FieldType:
    kind = dtype.casefold()
    if kind in {"string", "str", "object", "category"} or kind.startswith("string["):
        return "string"
    if re.fullmatch(r"u?int(?:8|16|32|64)?", kind):
        return "integer"
    if re.fullmatch(r"float(?:16|32|64|128)?|double|decimal", kind):
        return "number"
    if kind in {"bool", "boolean"}:
        return "boolean"
    if kind in {"date", "datetime", "timestamp"} or kind.startswith("datetime64["):
        return "datetime"
    return "other"


def _fields(fields: tuple[FieldMetadata, ...], total: int) -> tuple[FieldSummary, ...]:
    summaries = []
    for field in fields:
        name = unicodedata.normalize("NFC", field.name)
        if not _FIELD_NAME.fullmatch(name) or _FILENAME.search(name) or _WKT.search(name):
            continue
        missing = field.missing_count
        ratio = missing / total if total and missing is not None and missing <= total else None
        summaries.append(FieldSummary(name=name, type=_field_type(field.dtype), missing_ratio=ratio))
    return tuple(summaries)


def _asset_summary(asset: DataAsset) -> VectorSummary | TableSummary | RasterSummary:
    metadata = asset.metadata
    if isinstance(metadata, VectorMetadata):
        geometry = metadata.geometry_type.split()[0]
        return VectorSummary(
            geometry_type=geometry if geometry in _GEOMETRY_TYPES else "Other",
            feature_count=metadata.feature_count, fields=_fields(metadata.fields, metadata.feature_count),
        )
    if isinstance(metadata, TableMetadata):
        return TableSummary(row_count=metadata.row_count, fields=_fields(metadata.fields, metadata.row_count))
    if isinstance(metadata, RasterMetadata):
        x, y = metadata.resolution
        return RasterSummary(
            width=metadata.width, height=metadata.height, band_count=metadata.band_count,
            resolution_x=x, resolution_y=y,
        )
    raise OutboundContextError("The selected asset has no supported summary type.")


def build_outbound_context(
    context: TaskContext, assets: Iterable[DataAsset], *, protected_literals: Iterable[str] = (),
) -> OutboundContext:
    """Extract known structure and replace local references before final request validation."""
    selected = {assessment.asset for assessment in context.roles.values() if assessment.asset is not None}
    exact: dict[DataAssetRef, DataAsset] = {}
    references = list(protected_literals)
    for asset in assets:
        ref = asset.ref
        if ref not in selected:
            continue
        if ref in exact:
            raise OutboundContextError("The local context contains a duplicate asset version.")
        exact[ref] = asset
        references.extend((str(asset.source.path), asset.source.path.name, asset.id, asset.fingerprint))

    roles = []
    for role, assessment in context.roles.items():
        if not _ROLE_NAME.fullmatch(role):
            raise OutboundContextError("The local context contains an unsupported role name.")
        asset = exact.get(assessment.asset) if assessment.asset is not None else None
        available = asset if asset is not None and asset.availability == AssetAvailability.AVAILABLE else None
        readiness = assessment.status
        if available is None and readiness in {InputReadiness.READY, InputReadiness.CONDITIONAL}:
            readiness = InputReadiness.UNKNOWN
        roles.append(RoleSummary(
            role=role, readiness=readiness, asset=_asset_summary(available) if available is not None else None,
        ))
    return OutboundContext(goal=_clean_goal(context.goal.summary, references), roles=tuple(roles))
