"""Expose local asset versions and executable method definitions to the workbench."""

import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from geopilot.api.spatial_preview import vector_preview
from geopilot.application.draft_options import derived_method_options
from geopilot.data.assets import InputChangedError, register_asset
from geopilot.data.errors import SourceReadError
from geopilot.data.geopackage_reader import list_geopackage_layers
from geopilot.data.resolver import resolve_asset
from geopilot.domain.data_asset import AssetAvailability, AssetMetadata, DataAsset, DataAssetRef, FileSource, SourceType
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition
from geopilot.methods.validator import validate_method
from geopilot.persistence.asset_repository import list_assets
from geopilot.persistence.schema import open_database
from geopilot.plans.models import PlanIssue
from geopilot.tools.registry import ToolRegistry


_LocalText = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
_METHOD_LABELS = {
    "attribute-filter-export": ("属性筛选与导出", "按指定字段和值筛选要素，并保存匹配结果。"),
    "road-buffer-statistics": ("道路缓冲统计", "计算道路影响范围，裁回分析区并按分区统计目标。"),
    "spatial-select-and-clip": ("空间筛选与裁剪", "用完整对象判断空间关系，再裁回分析范围。"),
    "polygon-zone-statistics": ("跨区图斑统计", "在分析范围内按面积、质心或最大重叠分区统计。"),
    "remote-sensing-index-change": ("两期 NDVI 变化", "按产品元数据计算两期指数、共同有效差值和等积面积。"),
    "dem-terrain-analysis": ("DEM 地形分析", "计算坡度或坡向，按阈值分类并以等积投影统计面积。"),
    "land-use-change": ("土地利用变化", "比较两期分类，生成变化图斑、转移矩阵和分区统计。"),
}


class AssetRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: _LocalText
    layer: _LocalText | None = None


class AssetEntry(DataAssetRef):
    source: FileSource
    source_type: SourceType
    metadata: AssetMetadata
    availability: AssetAvailability
    created_at: datetime

    @classmethod
    def from_asset(cls, asset: DataAsset) -> "AssetEntry":
        return cls(
            **asset.ref.model_dump(), source=asset.source, source_type=asset.source_type,
            metadata=asset.metadata, availability=asset.availability, created_at=asset.created_at,
        )


class Assets(BaseModel):
    assets: list[AssetEntry]


class LayerEntry(BaseModel):
    name: str
    geometry_type: str | None


class Layers(BaseModel):
    layers: list[LayerEntry]


class MethodEntry(BaseModel):
    title: str
    description: str
    definition: MethodDefinition
    available: bool
    issues: tuple[PlanIssue, ...]
    derived_options: tuple[str, ...] = ()


class Methods(BaseModel):
    methods: list[MethodEntry]


def catalog_routes(
    database_path: str | Path, *, catalog: MethodCatalog | None,
    registry: ToolRegistry | None, registered_rules: Iterable[str],
) -> APIRouter:
    router = APIRouter(prefix="/api")
    rule_names = tuple(registered_rules)

    @router.get("/assets")
    def read_assets() -> Assets:
        try:
            with open_database(database_path, read_only=True) as connection:
                entries = list_assets(connection)
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(status_code=500, detail={
                "code": "STORAGE_FAILED", "message": "Could not read the local asset catalogue.",
            }) from error
        return Assets(assets=[AssetEntry.from_asset(asset) for asset in entries])

    @router.get("/assets/layers")
    def read_layers(path: Annotated[str, Query(min_length=1)]) -> Layers:
        try:
            entries = list_geopackage_layers(path)
        except SourceReadError as error:
            raise HTTPException(status_code=422, detail={
                "code": error.code, "field": error.field, "message": str(error),
            }) from error
        return Layers(layers=[LayerEntry(name=name, geometry_type=kind) for name, kind in entries])

    @router.post("/assets")
    def register(request: AssetRegistration) -> AssetEntry:
        try:
            asset = register_asset(database_path, request.path, layer=request.layer)
        except (SourceReadError, InputChangedError) as error:
            raise HTTPException(status_code=409 if isinstance(error, InputChangedError) else 422, detail={
                "code": error.code, "field": error.field, "message": str(error),
            }) from error
        except (OSError, sqlite3.Error, ValidationError) as error:
            raise HTTPException(status_code=500, detail={
                "code": "STORAGE_FAILED", "message": "Could not save the local asset.",
            }) from error
        return AssetEntry.from_asset(asset)

    @router.get("/assets/{asset_id}/preview")
    def read_asset_preview(asset_id: str, version: Annotated[int, Query(gt=0)]) -> Response:
        try:
            asset = resolve_asset(database_path, DataAssetRef(asset_id=asset_id, asset_version=version))
        except LookupError as error:
            raise HTTPException(status_code=404, detail={"code": "ASSET_NOT_FOUND", "message": str(error)}) from error
        except (SourceReadError, InputChangedError) as error:
            raise HTTPException(status_code=409 if isinstance(error, InputChangedError) else 422, detail={
                "code": error.code, "field": error.field, "message": str(error),
            }) from error
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(status_code=500, detail={
                "code": "STORAGE_FAILED", "message": "Could not read the local asset.",
            }) from error
        if asset.metadata.kind != "vector":
            raise HTTPException(status_code=422, detail={
                "code": "PREVIEW_UNAVAILABLE", "message": "This asset does not have a vector preview.",
            })
        return vector_preview(asset.source.path, asset.source.layer)

    @router.get("/methods")
    def read_methods() -> Methods:
        if catalog is None or registry is None:
            raise HTTPException(status_code=503, detail={
                "code": "METHOD_CATALOG_UNAVAILABLE", "message": "The local method catalogue is unavailable.",
            })
        methods = []
        for definition in catalog.list():
            issues = tuple(
                PlanIssue(code=issue.code, field=issue.field, message=issue.message)
                for issue in validate_method(definition, registry=registry, registered_rules=rule_names)
            )
            title, description = _METHOD_LABELS.get(
                definition.id, (definition.id, f"{len(definition.inputs)} 个输入角色，{len(definition.steps)} 个步骤。"),
            )
            methods.append(MethodEntry(
                title=title, description=description, definition=definition, available=not issues, issues=issues,
                derived_options=derived_method_options(definition.ref),
            ))
        return Methods(methods=methods)

    return router
