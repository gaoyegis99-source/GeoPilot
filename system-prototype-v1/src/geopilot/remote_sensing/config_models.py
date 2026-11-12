"""Versioned spectral and quality rules for an exact remote-sensing product."""

import re
from datetime import date
from typing import Annotated, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, HttpUrl, StringConstraints, model_validator

from geopilot.domain.data_asset import _Array, _NonnegativeInt, _PositiveInt, _PositiveNumber, _Text


_Token = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
_RoleName = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]*$")]
_MetadataComponent = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")]
_MetadataPath = Annotated[_Array[tuple[_MetadataComponent, ...]], Field(min_length=1)]
_BitWidth = Annotated[int, Field(strict=True, ge=1, le=64)]
_CLOSED_MAP_SCHEMA = {"additionalProperties": False}
_SPECTRAL_MAP_SCHEMA = {**_CLOSED_MAP_SCHEMA, "required": ["red", "nir"]}
_QualityCodes = Annotated[dict[_RoleName, _NonnegativeInt], Field(min_length=1, json_schema_extra=_CLOSED_MAP_SCHEMA)]
_ClassValues = Annotated[_Array[tuple[_NonnegativeInt, ...]], Field(min_length=1)]


def _source_date(value: object) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return date.fromisoformat(value)
    raise ValueError("The source date must be YYYY-MM-DD")


class _ConfigModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always", allow_inf_nan=False)


class SensorProductKey(_ConfigModel):
    sensor: _Token
    product_level: _Token
    processing_version: _Token


class SensorConfigRef(_ConfigModel):
    id: _Token
    version: _PositiveInt


class SensorBand(_ConfigModel):
    code: _Token
    resolution_m: _PositiveNumber
    nodata_dn: _NonnegativeInt | None


class MetadataFieldRef(_ConfigModel):
    """A field path relative to the product metadata root."""

    path: _MetadataPath


class BandMetadataFieldRef(MetadataFieldRef):
    """A repeated metadata field selected by its zero-based product band ID."""

    band_id: _NonnegativeInt


class MultiplyAddScale(_ConfigModel):
    kind: Literal["multiply_add"]
    scale_field: MetadataFieldRef
    offset_field: MetadataFieldRef

    @model_validator(mode="after")
    def distinct_fields(self) -> Self:
        if self.scale_field == self.offset_field:
            raise ValueError("The multiplier and additive offset require distinct metadata fields")
        return self


class QuantificationScale(_ConfigModel):
    kind: Literal["quantification"]
    quantification_field: MetadataFieldRef
    dn_offset_field: BandMetadataFieldRef

    @model_validator(mode="after")
    def distinct_fields(self) -> Self:
        if self.quantification_field.path == self.dn_offset_field.path:
            raise ValueError("Quantification and the band DN offset require distinct metadata fields")
        return self


ScaleOffset = Annotated[MultiplyAddScale | QuantificationScale, Field(discriminator="kind")]


class BitmaskQualityRule(_ConfigModel):
    kind: Literal["bitmask"]
    bit_width: _BitWidth
    invalid_bits: _QualityCodes

    @model_validator(mode="after")
    def valid_bit_positions(self) -> Self:
        bits = set(self.invalid_bits.values())
        if max(bits) >= self.bit_width or len(bits) != len(self.invalid_bits):
            raise ValueError("Invalid quality bits must be distinct positions inside the declared bit width")
        return self


class ClassificationQualityRule(_ConfigModel):
    kind: Literal["classification"]
    classes: _QualityCodes
    valid_values: _ClassValues

    @model_validator(mode="after")
    def valid_class_selection(self) -> Self:
        classes = set(self.classes.values())
        selected = set(self.valid_values)
        if len(classes) != len(self.classes) or len(selected) != len(self.valid_values) or not selected <= classes:
            raise ValueError("Quality class codes must be distinct and the valid subset must be declared")
        return self


QualityRule = Annotated[BitmaskQualityRule | ClassificationQualityRule, Field(discriminator="kind")]


class SensorSourceReference(_ConfigModel):
    title: _Text
    url: HttpUrl
    updated_at: Annotated[date, BeforeValidator(_source_date)]


class SensorProductConfig(SensorProductKey):
    """Bind named bands and metadata fields to a versioned product definition."""

    id: _Token
    version: _PositiveInt
    band_roles: Annotated[dict[_RoleName, SensorBand], Field(json_schema_extra=_SPECTRAL_MAP_SCHEMA)]
    scale_offset: Annotated[dict[_RoleName, ScaleOffset], Field(json_schema_extra=_SPECTRAL_MAP_SCHEMA)]
    quality_rules: Annotated[dict[_RoleName, QualityRule], Field(min_length=1, json_schema_extra=_CLOSED_MAP_SCHEMA)]
    source_reference: SensorSourceReference

    @model_validator(mode="after")
    def linked_band_roles(self) -> Self:
        roles = set(self.band_roles)
        quality = set(self.quality_rules)
        required_spectral = {"red", "nir"}
        if not required_spectral <= roles:
            raise ValueError("Red and near-infrared band roles are required")
        if not quality <= roles - required_spectral or (roles & {"quality", "saturation"}) - quality:
            raise ValueError("Quality rules must reference declared quality bands")
        if set(self.scale_offset) != roles - quality:
            raise ValueError("Every spectral band must have exactly one scale and offset declaration")
        locations = {(band.code, band.resolution_m) for band in self.band_roles.values()}
        if len(locations) != len(roles):
            raise ValueError("Band roles must refer to distinct product bands")
        offsets = [spec.dn_offset_field for spec in self.scale_offset.values() if isinstance(spec, QuantificationScale)]
        if len(set(offsets)) != len(offsets):
            raise ValueError("Spectral bands must select distinct band-indexed offset fields")
        return self

    @property
    def key(self) -> SensorProductKey:
        return SensorProductKey(sensor=self.sensor, product_level=self.product_level, processing_version=self.processing_version)

    @property
    def ref(self) -> SensorConfigRef:
        return SensorConfigRef(id=self.id, version=self.version)
