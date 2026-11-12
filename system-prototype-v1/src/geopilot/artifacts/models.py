"""Typed registration records for final run outputs and their provenance."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from geopilot.domain.data_asset import DataAssetRef
from geopilot.domain.errors import _RunPath, _Text
from geopilot.domain.step_io import StepOutput, _Layer, _Timestamp
from geopilot.domain.tool_contract import ToolRef


ArtifactKind = Literal["vector", "raster", "table", "json"]
_Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class _ArtifactModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always", allow_inf_nan=False)


class ArtifactSource(_ArtifactModel):
    plan_id: _Text
    plan_version: int = Field(strict=True, gt=0)
    step_key: _Text
    output_name: _Text
    tool: ToolRef
    inputs: dict[_Text, DataAssetRef]


class Artifact(_ArtifactModel):
    id: _Text
    run_id: _Text
    role: _Text
    uri: _RunPath
    layer: _Layer | None = None
    type: _Text
    kind: ArtifactKind
    format: _Text
    fingerprint: _Digest
    metadata: dict[_Text, JsonValue]
    source: ArtifactSource
    created_at: _Timestamp

    @property
    def output(self) -> StepOutput:
        return StepOutput(path=self.uri, layer=self.layer, type=self.type)
