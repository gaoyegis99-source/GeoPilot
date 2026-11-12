"""Manually supplied contracts and independent reference results."""

from typing import Self

from pydantic import JsonValue, field_validator, model_validator

from geopilot.domain.data_asset import DataAssetRef, _PositiveInt
from geopilot.domain.errors import _StepModel, _Text
from geopilot.domain.step_io import StepFileRef
from geopilot.domain.tool_contract import ToolContract, ToolRef
from geopilot.methods.models import MethodDefinition, MethodRef
from geopilot.scripts.models import _Digest, _Identifier


class ReferenceOutput(StepFileRef):
    fingerprint: _Digest | None = None
    crs: _Text | None = None
    features: dict[_Text, _Text] | None = None

    @field_validator("path")
    @classmethod
    def require_output_path(cls, value: str) -> str:
        if not value.startswith("outputs/"):
            raise ValueError("A reference must name an output of the trial run")
        return value

    @model_validator(mode="after")
    def require_reference_evidence(self) -> Self:
        if (self.crs is None) != (self.features is None):
            raise ValueError("A vector reference requires both CRS and feature WKT by source ID")
        if self.fingerprint is None and self.features is None:
            raise ValueError("Provide independent geometry or file-group fingerprint evidence")
        return self


class ReferenceCase(_StepModel):
    script_run_id: _Identifier
    script_sha256: _Digest
    inputs: dict[_Text, DataAssetRef]
    parameters: dict[_Text, JsonValue]
    basis: _Text
    outputs: dict[_Text, ReferenceOutput]


class RegistrationCandidate(_StepModel):
    tool: ToolContract
    method: MethodDefinition
    reference: ReferenceCase


class RegistrationEvidence(_StepModel):
    script_run_id: _Identifier
    script_id: _Identifier
    script_version: _PositiveInt
    script_sha256: _Digest
    tool: ToolRef
    method: MethodRef
    output_fingerprints: dict[_Text, _Digest]
