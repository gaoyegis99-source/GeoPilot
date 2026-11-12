"""Preview a model suggestion without writing a plan or starting execution."""

from collections.abc import Iterable
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from geopilot.application.task_context import TaskContext, build_task_context
from geopilot.domain.data_asset import DataAssetRef
from geopilot.domain.task import TaskGoal
from geopilot.llm.base import ModelErrorCode, ModelProvider, ModelProviderError
from geopilot.llm.candidate_validator import validate_model_candidate
from geopilot.llm.client import request_model_suggestion
from geopilot.llm.context_builder import OutboundContextError, build_outbound_context
from geopilot.llm.data_boundary import DataBoundaryError
from geopilot.llm.prompt_builder import build_model_request
from geopilot.llm.response_parser import ModelResponseError
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodRef
from geopilot.persistence.asset_repository import asset_for_ref
from geopilot.persistence.schema import open_database
from geopilot.plans.models import PlanIssue
from geopilot.remote_sensing.config_registry import SensorConfigRegistry
from geopilot.tools.registry import ToolRegistry


class ModelAssistRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: TaskGoal
    method: MethodRef | None = None
    inputs: dict[str, DataAssetRef] = Field(default_factory=dict)
    protected_literals: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=128)


class ModelAvailability(BaseModel):
    available: bool


class ModelPreview(BaseModel):
    method: MethodRef
    goal: TaskGoal
    options: dict[str, JsonValue]
    missing: list[str]
    explanation: str | None
    explanation_issue: PlanIssue | None
    issues: tuple[PlanIssue, ...]


def _problem(status: int, code: str, message: str, field: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "field": field})


def model_routes(
    database: str | Path,
    *,
    catalog: MethodCatalog | None,
    registry: ToolRegistry | None,
    registered_rules: Iterable[str],
    provider: ModelProvider | None,
    timeout: float,
    sensor_registry: SensorConfigRegistry | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/model-assistance")
    rules = tuple(registered_rules)

    @router.get("")
    def availability() -> ModelAvailability:
        return ModelAvailability(available=provider is not None and catalog is not None and registry is not None)

    @router.post("")
    def suggest(request: ModelAssistRequest) -> ModelPreview:
        if provider is None or catalog is None or registry is None:
            raise _problem(503, "MODEL_UNAVAILABLE", "模型服务未配置，可以继续手工填写。")

        preview_id = "model-preview-" + uuid4().hex
        if request.method is None:
            if request.inputs:
                raise _problem(422, "MODEL_INPUT_ROLE_REQUIRED", "请先选择资产所属的方法和角色。", "inputs")
            context = TaskContext(task_id=preview_id, goal=request.goal, method=MethodRef(id="task-context", version=1), roles={})
        else:
            try:
                definition = catalog.get_enabled(request.method.id, request.method.version)
            except KeyError as error:
                raise _problem(404, "METHOD_NOT_FOUND", "该本机方法版本不可用于新的建议。", "method") from error
            try:
                context = build_task_context(
                    database, task_id=preview_id, goal=request.goal, method=definition,
                    selected=request.inputs, registry=registry,
                )
            except ValueError as error:
                raise _problem(422, "MODEL_INPUT_ROLE_REQUIRED", "请为方法声明的角色选择资产。", "inputs") from error

        with open_database(database, read_only=True) as connection:
            assets = [asset_for_ref(connection, ref) for ref in set(request.inputs.values())]
        selected = [asset for asset in assets if asset is not None]
        protected = list(request.protected_literals)
        for asset in selected:
            protected.extend((str(asset.source.path), asset.source.path.name, asset.id, asset.fingerprint))

        try:
            summary = build_outbound_context(context, selected, protected_literals=protected)
            model_request = build_model_request(summary, catalog, registry=registry, registered_rules=rules)
            parsed = request_model_suggestion(provider, model_request, timeout=timeout, protected_literals=protected)
        except OutboundContextError as error:
            raise _problem(422, "OUTBOUND_DATA_REJECTED", "请先将目标中的原始数据替换为本机引用。", "goal.summary") from error
        except DataBoundaryError as error:
            raise _problem(422, error.code, "模型请求未通过数据边界检查，可继续手工填写。", error.field) from error
        except ModelResponseError as error:
            raise _problem(502, error.code, "模型返回的建议无法使用，可继续手工填写。", error.field) from error
        except ModelProviderError as error:
            status = {
                ModelErrorCode.TIMEOUT: 504, ModelErrorCode.RATE_LIMITED: 429, ModelErrorCode.INVALID_RESPONSE: 502,
            }.get(error.code, 503)
            raise _problem(status, error.code, "模型暂不可用，可继续手工填写。") from error

        method = catalog.get_enabled(parsed.method.id, parsed.method.version)
        applicable_inputs = {role: ref for role, ref in request.inputs.items() if role in method.inputs}
        validation = validate_model_candidate(
            database, task_id=preview_id, plan_id=preview_id, method=parsed.method,
            goal=request.goal, inputs=applicable_inputs, options=parsed.options,
            request=model_request, catalog=catalog, registry=registry, registered_rules=rules, sensor_registry=sensor_registry,
        )
        if validation.plan is None:
            issue = validation.issues[0]
            raise _problem(422, issue.code, issue.message, issue.field)
        return ModelPreview(
            method=parsed.method, goal=parsed.goal, options=validation.plan.critical_options,
            missing=parsed.missing, explanation=parsed.explanation, explanation_issue=parsed.explanation_issue,
            issues=validation.issues,
        )

    return router
