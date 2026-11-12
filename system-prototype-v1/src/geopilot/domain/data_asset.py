"""Versioned asset references and metadata for the local GeoPilot service."""

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)


_Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
_PositiveInt = Annotated[int, Field(strict=True, gt=0)]
_NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
_FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
_PositiveNumber = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
_NoData = StrictInt | _FiniteNumber | Literal["nan", "inf", "-inf"] | None
_Bounds = tuple[_FiniteNumber, _FiniteNumber, _FiniteNumber, _FiniteNumber]
_T = TypeVar("_T")


def _require_array(value: object) -> object:
    if not isinstance(value, (list, tuple)):
        raise ValueError("Expected a list or tuple")
    return value


_Array = Annotated[_T, BeforeValidator(_require_array)]


class _AssetModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")


class SourceType(StrEnum):
    LOCAL_FILE = "local_file"
    POSTGIS_SNAPSHOT = "postgis_snapshot"


class AssetAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class DataAssetRef(_AssetModel):
    asset_id: _Text
    asset_version: _PositiveInt


class FileSource(_AssetModel):
    """Absolute path and optional layer, reserved for local use."""

    path: Path
    layer: _Text | None = None
    modified_ns: StrictInt | None = None

    @field_validator("path")
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or "\x00" in str(value):
            raise ValueError("Expected an absolute local file path")
        return value


class FieldMetadata(_AssetModel):
    name: _Text
    dtype: _Text
    missing_count: _NonnegativeInt | None = None


class _Metadata(_AssetModel):
    format: _Text
    crs: _Text | None = None
    bounds: _Array[_Bounds] | None = None
    issues: _Array[tuple[_Text, ...]] = ()

    @field_validator("bounds")
    @classmethod
    def require_ordered_bounds(cls, value: _Bounds | None) -> _Bounds | None:
        if value is not None:
            min_x, min_y, max_x, max_y = value
            if min_x > max_x or min_y > max_y:
                raise ValueError("Bounds minima must not exceed their maxima")
        return value


class _SpatialMetadata(_Metadata):
    @model_validator(mode="after")
    def mark_unknown_crs(self) -> Self:
        if self.crs is None and "CRS_UNKNOWN" not in self.issues:
            raise ValueError("Missing spatial CRS requires the CRS_UNKNOWN issue")
        return self


class VectorMetadata(_SpatialMetadata):
    kind: Literal["vector"] = "vector"
    geometry_type: _Text
    feature_count: _NonnegativeInt
    fields: _Array[tuple[FieldMetadata, ...]]


class TableMetadata(_Metadata):
    kind: Literal["table"] = "table"
    row_count: _NonnegativeInt
    fields: _Array[tuple[FieldMetadata, ...]]


class RasterMetadata(_SpatialMetadata):
    kind: Literal["raster"] = "raster"
    width: _PositiveInt
    height: _PositiveInt
    band_count: _PositiveInt
    transform: _Array[
        tuple[_FiniteNumber, _FiniteNumber, _FiniteNumber, _FiniteNumber, _FiniteNumber, _FiniteNumber]
    ]
    resolution: _Array[tuple[_PositiveNumber, _PositiveNumber]]
    nodata: _Array[tuple[_NoData, ...]]
    band_dtypes: _Array[tuple[_Text, ...]]
    band_names: _Array[tuple[_Text | None, ...]]
    band_units: _Array[tuple[_Text | None, ...]]

    @model_validator(mode="after")
    def match_band_count(self) -> Self:
        for field in ("nodata", "band_dtypes", "band_names", "band_units"):
            if len(getattr(self, field)) != self.band_count:
                raise ValueError(f"{field} must contain exactly {self.band_count} values")
        return self


AssetMetadata = Annotated[VectorMetadata | RasterMetadata | TableMetadata, Field(discriminator="kind")]


class DataAsset(_AssetModel):
    """One locally persisted version of an asset."""

    id: _Text
    version: _PositiveInt
    source_type: SourceType
    source: FileSource
    metadata: AssetMetadata
    fingerprint: _Text
    availability: AssetAvailability
    created_at: AwareDatetime

    @field_validator("source_type", "availability", mode="before")
    @classmethod
    def require_enum_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("Expected an enum string")
        return value

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_created_at(cls, value: object) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        raise ValueError("Expected an ISO 8601 timestamp or datetime")

    @property
    def ref(self) -> DataAssetRef:
        return DataAssetRef(asset_id=self.id, asset_version=self.version)
