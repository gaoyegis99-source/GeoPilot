"""Versioned drafts and immutable requests for local script runs."""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, JsonValue, StrictStr, StringConstraints, field_validator, model_validator

from geopilot.domain.data_asset import DataAssetRef, _PositiveInt
from geopilot.domain.errors import _StepModel, _Text
from geopilot.domain.step_io import StepFileRef, _Timestamp
from geopilot.persistence.workspace import _validate_component


def _validate_id(value: str) -> str:
    _validate_component(value, "script identifier")
    return value


_Identifier = Annotated[StrictStr, AfterValidator(_validate_id)]
_Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class ScriptDraft(_StepModel):
    id: _Identifier
    version: _PositiveInt
    script_text: StrictStr
    inputs: dict[_Text, DataAssetRef]
    parameters: dict[_Text, JsonValue]
    created_at: _Timestamp

    @field_validator("script_text")
    @classmethod
    def require_utf8(cls, value: str) -> str:
        value.encode("utf-8")
        return value


class ScriptSnapshot(_StepModel):
    script_run_id: _Identifier
    script_id: _Identifier
    version: _PositiveInt
    script_path: Literal["snapshot/script.py"] = "snapshot/script.py"
    script_sha256: _Digest
    inputs: dict[_Text, DataAssetRef]
    asset_files: dict[_Text, StepFileRef]
    parameters: dict[_Text, JsonValue]
    created_at: _Timestamp

    @model_validator(mode="after")
    def validate_staged_inputs(self) -> Self:
        if self.inputs.keys() != self.asset_files.keys():
            raise ValueError("Snapshot asset files must match its input roles")
        if any(not reference.path.startswith("inputs/") for reference in self.asset_files.values()):
            raise ValueError("Snapshot asset files must be inside the staged inputs directory")
        return self
