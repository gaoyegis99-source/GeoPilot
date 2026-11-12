"""Versioned declarations for local GIS tools and their parameters."""

import math
from operator import ge, gt, le, lt
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)


_Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
_PositiveInt = Annotated[int, Field(strict=True, gt=0)]
_Number = StrictInt | Annotated[float, Field(strict=True, allow_inf_nan=False)]
_JSON_VALUE = TypeAdapter(JsonValue, config=ConfigDict(allow_inf_nan=False))
_VALUE_TYPES = {
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "enum": (str,),
    "string": (str,),
}


def _require_ordered_list(value: object) -> object:
    if not isinstance(value, (list, tuple)):
        raise ValueError("Expected an ordered list")
    return value


_Names = Annotated[tuple[_Text, ...], BeforeValidator(_require_ordered_list)]
_BandIndexes = Annotated[tuple[_PositiveInt, ...], BeforeValidator(_require_ordered_list)]


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", revalidate_instances="always", allow_inf_nan=False
    )


class ToolRef(_ContractModel):
    id: _Text
    version: _PositiveInt


class ParameterSpec(_ContractModel):
    type: Literal["number", "integer", "boolean", "enum", "string", "json"]
    unit: _Text | None = None
    minimum: _Number | None = None
    maximum: _Number | None = None
    minimum_exclusive: _Number | None = None
    maximum_exclusive: _Number | None = None
    values: _Names | None = None
    required: StrictBool = True
    default: JsonValue = None

    def _validate_bounds(self) -> None:
        if self.type not in ("number", "integer"):
            limits = (self.minimum, self.maximum, self.minimum_exclusive, self.maximum_exclusive)
            if self.unit is not None or any(limit is not None for limit in limits):
                raise ValueError("Units and bounds require a numeric parameter type")
            return

        if self.minimum is not None and self.minimum_exclusive is not None:
            raise ValueError("Specify only one lower bound")
        if self.maximum is not None and self.maximum_exclusive is not None:
            raise ValueError("Specify only one upper bound")

        lower = self.minimum if self.minimum is not None else self.minimum_exclusive
        upper = self.maximum if self.maximum is not None else self.maximum_exclusive
        if lower is None or upper is None:
            return
        lower_exclusive = self.minimum_exclusive is not None
        upper_exclusive = self.maximum_exclusive is not None
        if lower > upper or (lower == upper and (lower_exclusive or upper_exclusive)):
            raise ValueError("The lower and upper bounds must allow a value")
        if self.type == "integer":
            first = math.floor(lower) + 1 if lower_exclusive else math.ceil(lower)
            last = math.ceil(upper) - 1 if upper_exclusive else math.floor(upper)
            if first > last:
                raise ValueError("The lower and upper bounds must allow an integer")

    def validate_value(self, value: object) -> None:
        """Validate a value already expressed in the parameter's declared unit."""
        if self.type == "json":
            try:
                _JSON_VALUE.validate_python(value)
            except ValidationError as error:
                raise ValueError("Expected a finite JSON value") from error
            return

        if type(value) not in _VALUE_TYPES[self.type]:
            raise ValueError(f"Expected a value of type {self.type}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Expected a finite number")
        if self.type == "enum" and value not in (self.values or ()):
            raise ValueError("Expected one of the declared enum values")

        for name, limit, compare in (
            ("minimum", self.minimum, ge),
            ("maximum", self.maximum, le),
            ("minimum_exclusive", self.minimum_exclusive, gt),
            ("maximum_exclusive", self.maximum_exclusive, lt),
        ):
            if limit is not None and not compare(value, limit):
                raise ValueError(f"Value violates {name}={limit}")

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        self._validate_bounds()
        if self.type == "enum":
            if not self.values or len(set(self.values)) != len(self.values):
                raise ValueError("Enum values must be nonempty and unique")
        elif self.values is not None:
            raise ValueError("Only enum parameters can declare values")
        if self.default is not None:
            self.validate_value(self.default)
        return self


class MeasurementPolicy(_ContractModel):
    distance: Literal["local_projected", "geodesic", "not_applicable"]
    area: Literal["equal_area", "not_applicable"]
    forbid_web_mercator: StrictBool = True
    forbid_geographic_planar: StrictBool = True

    @model_validator(mode="after")
    def require_planar_guards(self) -> Self:
        planar = self.distance == "local_projected" or self.area == "equal_area"
        if planar and not (self.forbid_web_mercator and self.forbid_geographic_planar):
            raise ValueError("Planar measurement requires Web Mercator and geographic CRS guards")
        return self


class RasterNoDataPolicy(_ContractModel):
    """Declare how the selected one-based bands may represent missing samples."""

    mode: Literal["allow", "require_valid_samples", "reject_invalid_samples"]
    bands: Literal["all"] | _BandIndexes | None = None
    band_parameter: _Text | None = None
    nodata_declaration: Literal["any", "none", "nan"] = "any"

    @model_validator(mode="after")
    def validate_band_selector(self) -> Self:
        if (self.bands is None) == (self.band_parameter is None):
            raise ValueError("Specify exactly one of bands and band_parameter")
        if isinstance(self.bands, tuple) and (not self.bands or len(set(self.bands)) != len(self.bands)):
            raise ValueError("Band indexes must be nonempty and unique")
        return self


class ToolContract(_ContractModel):
    id: _Text
    version: _PositiveInt
    runner: Literal["qgis", "python"]
    implementation: _Text
    inputs: dict[_Text, _Text]
    outputs: dict[_Text, _Text]
    parameters: dict[_Text, ParameterSpec]
    measurement_policy: MeasurementPolicy
    preconditions: _Names
    check_rules: _Names
    raster_nodata: dict[_Text, RasterNoDataPolicy] = Field(default_factory=dict, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def validate_raster_nodata(self) -> Self:
        for role, policy in self.raster_nodata.items():
            kind = self.inputs.get(role, "")
            if kind != "raster" and not kind.startswith("raster_"):
                raise ValueError(f"raster_nodata.{role} requires a declared raster input role")
            if policy.band_parameter is not None:
                parameter = self.parameters.get(policy.band_parameter)
                if (
                    parameter is None or parameter.type != "integer"
                    or (not parameter.required and parameter.default is None)
                ):
                    raise ValueError(f"raster_nodata.{role}.band_parameter requires an integer parameter with a value")
        return self

    @property
    def ref(self) -> ToolRef:
        return ToolRef(id=self.id, version=self.version)
