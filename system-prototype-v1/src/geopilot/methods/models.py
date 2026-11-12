"""Versioned analysis method templates and their declared bindings."""

from collections.abc import Mapping
from typing import Annotated, Literal, Self, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StringConstraints,
    model_validator,
)

from geopilot.domain.tool_contract import ParameterSpec, ToolRef


_Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
_PositiveInt = Annotated[int, Field(strict=True, gt=0)]
_T = TypeVar("_T")


def _require_array(value: object) -> object:
    if not isinstance(value, (list, tuple)):
        raise ValueError("Expected an ordered list or tuple")
    return value


_Array = Annotated[_T, BeforeValidator(_require_array)]


class _MethodModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", revalidate_instances="always", allow_inf_nan=False
    )


class MethodRef(_MethodModel):
    id: _Text
    version: _PositiveInt


class ReferenceOption(_MethodModel):
    type: Literal["reference"]
    required_for: _Text | None = None


_TypedOption = Annotated[ParameterSpec | ReferenceOption, Field(discriminator="type")]
_Option = _TypedOption | Literal["user_defined", "user_selected"]
_EnumShorthand = Annotated[list[_Text], Field(min_length=1, json_schema_extra={"uniqueItems": True})]


def _normalize_option(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return {"type": "enum", "values": value}
    return value


_CriticalOption = Annotated[
    _Option, BeforeValidator(_normalize_option, json_schema_input_type=_Option | _EnumShorthand)
]


class AssetRoleBinding(_MethodModel):
    """Bind an input port to a declared method input role."""

    asset: _Text


class StepOutputBinding(_MethodModel):
    """Bind an input port to a step's named output."""

    step: _Text
    output: _Text


class OptionBinding(_MethodModel):
    option: _Text


class LiteralBinding(_MethodModel):
    literal: JsonValue


class ExpectedArtifactBinding(_MethodModel):
    role: _Text
    step: _Text
    output: _Text
    type: _Text


class MethodStep(_MethodModel):
    key: _Text
    tool: ToolRef | None = None
    inputs: dict[_Text, AssetRoleBinding | StepOutputBinding] = Field(default_factory=dict)
    parameters: dict[_Text, OptionBinding | LiteralBinding] = Field(default_factory=dict)
    outputs: dict[_Text, _Text] = Field(default_factory=dict)


class StepVariant(_MethodModel):
    """Bind an exact tool and any branch-specific ports or parameters for a step."""

    tool: ToolRef
    inputs: dict[_Text, AssetRoleBinding | StepOutputBinding] | None = Field(default=None, exclude_if=lambda value: value is None)
    parameters: dict[_Text, OptionBinding | LiteralBinding] | None = Field(default=None, exclude_if=lambda value: value is None)
    outputs: dict[_Text, _Text] | None = Field(default=None, exclude_if=lambda value: value is None)


class StepVariants(_MethodModel):
    option: _Text
    choices: dict[_Text, dict[_Text, StepVariant]]

    def selected(self, options: Mapping[str, JsonValue]) -> str | None:
        value = options.get(self.option)
        return value if isinstance(value, str) and value in self.choices else None


class InputScopeRule(_MethodModel):
    """Compare a role with another selected role or the optional task scope."""

    reference_role: _Text | None = None
    distance_option: _Text | None = None
    distance_crs_option: _Text | None = None
    distance_method_option: _Text | None = Field(default=None, exclude_if=lambda value: value is None)
    allow_empty: StrictBool = False

    @model_validator(mode="after")
    def paired_distance_options(self) -> Self:
        if (self.distance_option is None) != (self.distance_crs_option is None):
            raise ValueError("A distance scope requires both a distance and a measurement CRS option")
        if self.distance_method_option is not None and self.distance_option is None:
            raise ValueError("A distance method option requires a distance scope")
        return self


class InputContextRule(_MethodModel):
    required_fields: _Array[tuple[_Text, ...]] = ()
    field_options: _Array[tuple[_Text, ...]] = ()
    scopes: _Array[tuple[InputScopeRule, ...]] = ()


class MethodDefinition(_MethodModel):
    id: _Text
    version: _PositiveInt
    locked_order: StrictBool
    inputs: dict[_Text, _Text]
    critical_options: dict[_Text, _CriticalOption]
    preconditions: _Array[tuple[_Text, ...]] = ()
    steps: _Array[tuple[MethodStep, ...]]
    expected_artifacts: _Array[tuple[_Text | ExpectedArtifactBinding, ...]]
    check_rules: _Array[tuple[_Text, ...]]
    input_context: dict[_Text, InputContextRule] = Field(default_factory=dict, exclude_if=lambda value: not value)
    step_variants: StepVariants | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def variant_links(self) -> Self:
        variants = self.step_variants
        if variants is None:
            return self
        option = self.critical_options.get(variants.option)
        if not isinstance(option, ParameterSpec) or option.type != "enum" or not option.required:
            raise ValueError("Step variants require a declared required enum option")
        if variants.choices.keys() != set(option.values or ()):
            raise ValueError("Step variant choices must match every value of the selector option")
        keys = {step.key for step in self.steps}
        unbound = {step.key for step in self.steps if step.tool is None}
        for value, overrides in variants.choices.items():
            if overrides.keys() - keys:
                raise ValueError(f"Step variant {value!r} refers to undeclared step keys")
            if unbound - overrides.keys():
                raise ValueError(f"Step variant {value!r} leaves steps without an exact tool")
        return self

    @model_validator(mode="after")
    def input_context_links(self) -> Self:
        for role, rule in self.input_context.items():
            if role not in self.inputs:
                raise ValueError(f"Input context refers to undeclared role {role!r}")
            for name in rule.field_options:
                option = self.critical_options.get(name)
                if not isinstance(option, ParameterSpec) or option.type != "string":
                    raise ValueError(f"Input context field option {name!r} must declare a string")
            for scope in rule.scopes:
                reference = scope.reference_role
                if reference is not None and (reference == role or reference not in self.inputs):
                    raise ValueError(f"Input context scope for {role!r} has an invalid reference role")
                if scope.distance_option is not None:
                    distance = self.critical_options.get(scope.distance_option)
                    if not isinstance(distance, ParameterSpec) or distance.unit != "meter":
                        raise ValueError("Input context distance must use a declared metre option")
                    if not isinstance(self.critical_options.get(scope.distance_crs_option), ReferenceOption):
                        raise ValueError("Input context distance requires a declared CRS reference option")
                if scope.distance_method_option is not None:
                    distance_method = self.critical_options.get(scope.distance_method_option)
                    if (
                        not isinstance(distance_method, ParameterSpec) or distance_method.type != "enum"
                        or set(distance_method.values or ()) - {"local_projected", "geodesic"}
                    ):
                        raise ValueError("Input context distance mode must use a declared distance-method enum")
        return self

    def steps_for(self, options: Mapping[str, JsonValue]) -> tuple[MethodStep, ...]:
        """Resolve the selected tools and bindings while preserving the declared step order."""
        variants = self.step_variants
        if variants is None or (mode := variants.selected(options)) is None:
            return self.steps

        overrides = variants.choices[mode]
        steps = []
        for step in self.steps:
            override = overrides.get(step.key)
            if override is not None:
                step = step.model_copy(update={
                    "tool": override.tool,
                    "inputs": step.inputs if override.inputs is None else override.inputs,
                    "parameters": step.parameters if override.parameters is None else override.parameters,
                    "outputs": step.outputs if override.outputs is None else override.outputs,
                })
            steps.append(step)
        return tuple(steps)

    def inactive_variant_options(self, options: Mapping[str, JsonValue]) -> set[str]:
        variants = self.step_variants
        if variants is None or (mode := variants.selected(options)) is None:
            return set()
        bound = {
            binding.option for step in self.steps_for(options) for binding in step.parameters.values()
            if isinstance(binding, OptionBinding)
        }
        return {
            name for name, spec in self.critical_options.items()
            if isinstance(spec, ReferenceOption) and spec.required_for in variants.choices
            and spec.required_for != mode and name not in bound
        }

    @property
    def ref(self) -> MethodRef:
        return MethodRef(id=self.id, version=self.version)
