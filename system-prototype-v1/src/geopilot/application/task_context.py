"""Assess selected asset versions and shortlist relevant assets for a task."""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Annotated, Literal

import pyogrio
from affine import Affine
from geopandas import GeoDataFrame, GeoSeries
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints
from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from shapely import union_all
from shapely.errors import GEOSException
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from geopilot.data.assets import InputChangedError
from geopilot.data.errors import SourceReadError
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.data.resolver import resolve_asset
from geopilot.data.vector_reader import local_ogr_path
from geopilot.domain.data_asset import AssetAvailability, AssetMetadata, DataAsset, DataAssetRef, RasterMetadata, VectorMetadata
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ParameterSpec, ToolContract
from geopilot.execution.step_support import matches_vector_type
from geopilot.gis.measurement.area import validate_area
from geopilot.gis.measurement.coverage import MeasurementPolicyError
from geopilot.gis.measurement.distance import validate_tool_distance
from geopilot.gis.measurement.geodesic_road_scope import (
    expand_wgs84_analysis_extent, prepare_wgs84_geometry, road_intersects_influence,
)
from geopilot.gis.validation.crs_validator import CRSValidationError, inspect_crs
from geopilot.methods.models import AssetRoleBinding, ExpectedArtifactBinding, InputContextRule, InputScopeRule, MethodDefinition, MethodRef
from geopilot.persistence.asset_repository import list_assets
from geopilot.persistence.schema import open_database
from geopilot.remote_sensing.product_metadata import ProductMetadataError, read_product_metadata
from geopilot.tools.parameter_normalizer import _convert_quantity
from geopilot.tools.registry import ToolRegistry


_Text = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]


class InputReadiness(StrEnum):
    READY = "READY"
    CONDITIONAL = "CONDITIONAL"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class InputAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    asset: DataAssetRef | None
    status: InputReadiness
    code: _Text | None = None
    message: _Text | None = None


class ContextCriteria(BaseModel):
    """Local task criteria for the asset shortlist."""

    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    scope: DataAssetRef | None = None
    object_types: dict[_Text, Literal["vector_point", "vector_line", "vector_polygon"]] = Field(default_factory=dict)
    expected_artifact: _Text | None = None


class TaskContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    task_id: _Text
    goal: TaskGoal
    method: MethodRef
    roles: dict[_Text, InputAssessment]
    criteria: ContextCriteria | None = Field(default=None, exclude_if=lambda value: value is None)
    candidates: dict[_Text, tuple[InputAssessment, ...]] | None = Field(default=None, exclude_if=lambda value: value is None)


def current_task_criteria(
    database_path: str | Path, *, task_id: str, method: MethodRef, goal: TaskGoal,
    selected: Mapping[str, DataAssetRef],
) -> ContextCriteria:
    """Reuse saved shortlist criteria when the current context matches the snapshot."""
    with open_database(database_path, read_only=True) as connection:
        row = connection.execute("SELECT context_json FROM task WHERE id = ?", (task_id,)).fetchone()
    if row is not None and row["context_json"] is not None:
        context = TaskContext.model_validate_json(row["context_json"])
        refs = {role: item.asset for role, item in context.roles.items() if item.asset is not None}
        if (context.task_id, context.method, context.goal, refs) == (task_id, method, goal, selected):
            return context.criteria or ContextCriteria()
    return ContextCriteria()


def _measurement_requirements(
    method: MethodDefinition, registry: ToolRegistry | None, options: Mapping[str, JsonValue],
) -> dict[str, list[tuple[str, ToolContract]]]:
    requirements: dict[str, list[tuple[str, ToolContract]]] = {}
    if registry is None:
        return requirements
    for step in method.steps_for(options):
        if step.tool is None:
            continue
        try:
            contract = registry.get(step.tool.id, step.tool.version).contract
        except KeyError:
            continue
        policy = contract.measurement_policy
        if policy.distance == policy.area == "not_applicable":
            continue
        for port, binding in step.inputs.items():
            kind = contract.inputs.get(port, "")
            if isinstance(binding, AssetRoleBinding) and kind.startswith(("vector", "raster")):
                requirements.setdefault(binding.asset, []).append((port, contract))
    return requirements


def _matches_type(metadata: AssetMetadata, expected: str) -> bool:
    if isinstance(metadata, VectorMetadata):
        return (expected == "vector" or expected.startswith("vector_")) and matches_vector_type(metadata, expected)
    if metadata.kind == "raster":
        return expected == "raster" or expected.startswith("raster_")
    return expected == "table"


def _selected_asset(
    database_path: str | Path, ref: DataAssetRef | None, role: str,
) -> DataAsset | InputAssessment:
    if ref is None:
        return InputAssessment(
            asset=None, status=InputReadiness.UNKNOWN, code="INPUT_REQUIRED",
            message=f"Select an asset for {role}",
        )
    try:
        return resolve_asset(database_path, ref)
    except LookupError as error:
        return InputAssessment(
            asset=ref, status=InputReadiness.BLOCKED, code="SOURCE_UNREADABLE", message=str(error)
        )
    except (SourceReadError, InputChangedError) as error:
        return InputAssessment(
            asset=ref, status=InputReadiness.BLOCKED, code=error.code, message=str(error)
        )


def _assess_asset(
    asset: DataAsset, role: str, expected: str, measurements: list[tuple[str, ToolContract]],
    required_fields: Iterable[str] = (),
) -> InputAssessment:
    ref = asset.ref
    if asset.availability != AssetAvailability.AVAILABLE:
        return InputAssessment(
            asset=ref, status=InputReadiness.BLOCKED, code="SOURCE_UNREADABLE",
            message=f"The selected version for {role} is unavailable",
        )
    if not _matches_type(asset.metadata, expected):
        actual = asset.metadata.geometry_type if isinstance(asset.metadata, VectorMetadata) else asset.metadata.kind
        return InputAssessment(
            asset=ref, status=InputReadiness.BLOCKED, code="METHOD_PRECONDITION_FAILED",
            message=f"Role {role} requires {expected}, observed {actual}",
        )
    missing = sorted(set(required_fields) - {field.name for field in getattr(asset.metadata, "fields", ())})
    if missing:
        return _assessment(ref, InputReadiness.BLOCKED, "INPUT_FIELD_MISSING", f"缺少字段 {', '.join(missing)}")

    for port, contract in measurements:
        policy = contract.measurement_policy
        try:
            if policy.distance != "not_applicable":
                validate_tool_distance(asset.metadata, contract, field=f"inputs.{port}.crs")
            if policy.area == "equal_area":
                validate_area(asset.metadata, field=f"inputs.{role}.crs")
        except CRSValidationError as error:
            return InputAssessment(
                asset=ref, status=InputReadiness.UNKNOWN, code=error.code, message=error.message
            )
        except MeasurementPolicyError as error:
            status = InputReadiness.BLOCKED if policy.distance == "geodesic" else InputReadiness.CONDITIONAL
            return InputAssessment(asset=ref, status=status, code=error.code, message=error.message)
    return InputAssessment(asset=ref, status=InputReadiness.READY)


def _relevant_roles(method: MethodDefinition, artifact: str | None, options: Mapping[str, JsonValue]) -> set[str]:
    if artifact is None:
        return set(method.inputs)
    for item in method.expected_artifacts:
        if isinstance(item, str) and item == artifact:
            return set(method.inputs)
        if isinstance(item, ExpectedArtifactBinding) and item.role == artifact:
            break
    else:
        raise ValueError(f"Unknown expected artifact {artifact!r}")

    by_key = {step.key: step for step in method.steps_for(options)}
    remaining = [item.step]
    visited: set[str] = set()
    roles: set[str] = set()
    while remaining:
        key = remaining.pop()
        if key in visited:
            continue
        visited.add(key)
        if key not in by_key:
            return set(method.inputs)
        for binding in by_key[key].inputs.values():
            if isinstance(binding, AssetRoleBinding):
                roles.add(binding.asset)
            else:
                remaining.append(binding.step)
    return roles


def _vector_geometries(asset: DataAsset, metadata: VectorMetadata, crs: CRS) -> GeoSeries:
    frame = pyogrio.read_dataframe(local_ogr_path(asset.source.path), layer=asset.source.layer, columns=[])
    if (
        not isinstance(frame, GeoDataFrame) or len(frame) != metadata.feature_count
        or frame.crs is None or not frame.crs.to_2d().equals(crs, ignore_axis_order=True)
        or not frame.geometry.is_valid.all() or frame.geometry.is_empty.any()
    ):
        raise ValueError("The vector geometry or CRS could not be verified")
    return frame.geometry


def _footprint(asset: DataAsset) -> tuple[BaseGeometry, CRS]:
    metadata = asset.metadata
    crs = inspect_crs(metadata).crs.to_2d()
    if isinstance(metadata, RasterMetadata):
        if read_raster_metadata(asset.source.path) != metadata:
            raise ValueError("The raster grid differs from its registered version")
        affine = Affine(*metadata.transform)
        footprint = Polygon([affine * point for point in (
            (0, 0), (metadata.width, 0), (metadata.width, metadata.height), (0, metadata.height),
        )])
    elif isinstance(metadata, VectorMetadata):
        footprint = union_all(_vector_geometries(asset, metadata, crs).to_numpy())
    else:
        raise ValueError("The asset has no spatial footprint")
    if not footprint.is_valid:
        raise ValueError("The asset has no valid footprint")
    return footprint, crs


def _project_footprint(footprint: tuple[BaseGeometry, CRS], target: CRS) -> BaseGeometry:
    geometry, source = footprint
    if geometry.is_empty or source.equals(target, ignore_axis_order=True):
        return geometry
    project = Transformer.from_crs(source, target, always_xy=True)
    result = transform(lambda x, y, *_: project.transform(x, y, errcheck=True), geometry)
    if result.is_empty or not result.is_valid:
        raise ValueError("The transformed footprint could not be verified")
    return result


def _scope_distance_value(
    method: MethodDefinition, rule: InputScopeRule, options: Mapping[str, JsonValue],
) -> float:
    spec = method.critical_options[rule.distance_option]
    assert isinstance(spec, ParameterSpec)
    raw = options.get(rule.distance_option, spec.default)
    distance = _convert_quantity(spec, raw) if isinstance(raw, Mapping) else raw
    spec.validate_value(distance)
    return float(distance)


def _scope_distance(
    method: MethodDefinition, rule: InputScopeRule, scope: DataAsset, options: Mapping[str, JsonValue],
) -> tuple[float, CRS]:
    distance = _scope_distance_value(method, rule, options)
    chosen_crs = options.get(rule.distance_crs_option)
    if not isinstance(chosen_crs, str) or distance <= 0:
        raise ValueError("The scope distance needs a positive value and a confirmed measurement CRS")
    metadata = scope.metadata.model_copy(update={"crs": chosen_crs})
    return distance, inspect_crs(metadata, require_projected_metres=True).crs.to_2d()


def _assessment(ref: DataAssetRef, status: InputReadiness, code: str, message: str) -> InputAssessment:
    return InputAssessment(asset=ref, status=status, code=code, message=message)


def build_task_context(
    database_path: str | Path,
    *,
    task_id: str,
    goal: TaskGoal,
    method: MethodDefinition,
    selected: Mapping[str, DataAssetRef],
    registry: ToolRegistry | None = None,
    criteria: ContextCriteria | None = None,
    options: Mapping[str, JsonValue] | None = None,
    verify_selected: bool = True,
) -> TaskContext:
    """Reopen selected versions and optionally build the local task shortlist."""
    unknown = selected.keys() - method.inputs.keys()
    if unknown:
        raise ValueError(f"Selection contains undeclared input roles: {', '.join(sorted(unknown))}")
    options = options or {}
    requirements = _measurement_requirements(method, registry, options)
    if criteria is None:
        roles = {}
        for role, expected in method.inputs.items():
            observed = _selected_asset(database_path, selected.get(role), role)
            rule = method.input_context.get(role)
            roles[role] = observed if isinstance(observed, InputAssessment) else _assess_asset(
                observed, role, expected, requirements.get(role, []), rule.required_fields if rule is not None else (),
            )
        return TaskContext(task_id=task_id, goal=goal, method=method.ref, roles=roles)

    for role, kind in criteria.object_types.items():
        declared = method.inputs.get(role, "")
        if declared != "vector" and kind.removeprefix("vector_") not in declared.removeprefix("vector_").split("_or_"):
            raise ValueError(f"Object type {kind!r} is incompatible with role {role!r}")
    relevant = _relevant_roles(method, criteria.expected_artifact, options)
    with open_database(database_path, read_only=True) as connection:
        assets = {asset.ref: asset for asset in list_assets(connection)}
    checked: dict[DataAssetRef, DataAsset | InputAssessment] = {}

    def observe(role: str, ref: DataAssetRef | None) -> DataAsset | InputAssessment:
        if ref is None:
            return _selected_asset(database_path, ref, role)
        if not verify_selected and ref in assets:
            return assets[ref]
        if ref not in checked:
            checked[ref] = _selected_asset(database_path, ref, role)
            if isinstance(checked[ref], DataAsset):
                assets[ref] = checked[ref]
        return checked[ref]

    observed = {role: observe(role, selected.get(role)) for role in method.inputs}
    task_scope = observe("候选筛选范围", criteria.scope) if criteria.scope is not None else None

    @cache
    def footprint(ref: DataAssetRef) -> tuple[BaseGeometry, CRS]:
        return _footprint(assets[ref])

    @cache
    def is_product(ref: DataAssetRef) -> bool:
        try:
            read_product_metadata(assets[ref].source.path)
        except ProductMetadataError:
            return False
        return True

    @cache
    def geodesic_influence(scope_ref: DataAssetRef, distance: float) -> BaseGeometry:
        scope = assets[scope_ref]
        if not isinstance(scope.metadata, VectorMetadata):
            raise ValueError("The geodesic influence area requires polygon geometry")
        crs = inspect_crs(scope.metadata).crs
        polygons = _vector_geometries(scope, scope.metadata, crs.to_2d())
        extent = union_all(prepare_wgs84_geometry(polygons.to_numpy(), crs))
        return expand_wgs84_analysis_extent(extent, distance)

    reference_roles = {scope.reference_role for rule in method.input_context.values() for scope in rule.scopes}

    def assess(role: str, asset: DataAsset) -> InputAssessment:
        ref = asset.ref
        expected = criteria.object_types.get(role, method.inputs[role])
        rule = method.input_context.get(role, InputContextRule())
        fields = set(rule.required_fields)
        for name in rule.field_options:
            spec = method.critical_options[name]
            assert isinstance(spec, ParameterSpec)
            value = options.get(name, spec.default)
            if isinstance(value, str) and value.strip():
                fields.add(value)

        baseline = _assess_asset(asset, role, expected, requirements.get(role, []), fields)
        if baseline.status is InputReadiness.BLOCKED:
            return baseline
        if role in reference_roles and expected.startswith(("vector", "raster")) and asset.metadata.crs is None:
            return _assessment(ref, InputReadiness.UNKNOWN, "CRS_UNKNOWN", f"{role} 缺少 CRS，无法核验空间范围")
        if expected in {"raster_product", "raster_elevation"} and is_product(ref) != (expected == "raster_product"):
            return _assessment(ref, InputReadiness.BLOCKED, "INPUT_OBJECT_MISMATCH", f"资产与角色 {role} 的产品类型不匹配")

        scopes = rule.scopes
        if not scopes and criteria.scope and role not in reference_roles and role != "analysis_extent" and expected.startswith(("vector", "raster")):
            scopes = (InputScopeRule(),)
        for scope_rule in scopes:
            mode = options.get(scope_rule.distance_method_option) if scope_rule.distance_method_option else "local_projected"
            if mode not in ("local_projected", "geodesic"):
                continue
            reference = scope_rule.reference_role
            scope_ref = selected.get(reference) if reference is not None else None
            if scope_ref is None and reference in {None, "analysis_extent"}:
                scope_ref, scope, scope_type = criteria.scope, task_scope, "vector_polygon"
                label, allow_empty = "候选筛选范围", False
            elif scope_ref is not None:
                scope, scope_type = observed[reference], method.inputs[reference]
                label, allow_empty = reference, scope_rule.allow_empty
            if scope_ref is None or scope_ref == ref:
                continue
            if isinstance(scope, InputAssessment):
                return _assessment(ref, InputReadiness.UNKNOWN, "INPUT_SCOPE_UNVERIFIED", f"无法核验 {label}：{scope.message}")
            if scope is None or scope.availability != AssetAvailability.AVAILABLE:
                return _assessment(ref, InputReadiness.UNKNOWN, "INPUT_SCOPE_UNVERIFIED", f"无法核验 {label} 的资产版本")
            if not _matches_type(scope.metadata, scope_type):
                return _assessment(ref, InputReadiness.UNKNOWN, "INPUT_SCOPE_UNVERIFIED", f"{label} 需要 {scope_type} 类型资产")
            if asset.metadata.crs is None or scope.metadata.crs is None:
                owner = role if asset.metadata.crs is None else label
                return _assessment(ref, InputReadiness.UNKNOWN, "CRS_UNKNOWN", f"{owner} 缺少 CRS，无法核验空间范围")
            try:
                if mode == "geodesic":
                    if scope_rule.distance_option not in options:
                        continue
                    distance = _scope_distance_value(method, scope_rule, options)
                    expanded = geodesic_influence(scope_ref, distance)
                    if not isinstance(asset.metadata, VectorMetadata):
                        raise ValueError("Geodesic road selection requires vector lines")
                    crs = inspect_crs(asset.metadata).crs
                    lines = _vector_geometries(asset, asset.metadata, crs.to_2d())
                    prepared_lines = prepare_wgs84_geometry(lines.to_numpy(), crs)
                    matches = any(road_intersects_influence(line, expanded) for line in prepared_lines)
                else:
                    if scope_rule.distance_option is None:
                        distance, target_crs = 0, footprint(scope_ref)[1]
                    else:
                        distance, target_crs = _scope_distance(method, scope_rule, scope, options)
                    source_geometry = _project_footprint(footprint(ref), target_crs)
                    scope_geometry = _project_footprint(footprint(scope_ref), target_crs)
                    if scope_geometry.is_empty:
                        raise ValueError("The reference layer contains no geometry")
                    matches = source_geometry.distance(scope_geometry) <= distance if distance else source_geometry.intersects(scope_geometry)
            except (OSError, OverflowError, RuntimeError, ValueError, ProjError, GEOSException) as error:
                return _assessment(ref, InputReadiness.UNKNOWN, "INPUT_SCOPE_UNVERIFIED", f"无法核验 {label}：{error}")
            if not matches:
                status = baseline.status if allow_empty else InputReadiness.BLOCKED
                detail = "，方法允许生成空结果" if allow_empty else ""
                return _assessment(ref, status, "INPUT_SCOPE_DISJOINT", f"资产与 {label} 不交叠{detail}")
        return baseline

    roles = {role: asset if isinstance(asset, InputAssessment) else assess(role, asset) for role, asset in observed.items()}

    candidates = {}
    for role, expected in method.inputs.items():
        if role not in relevant:
            continue
        shortlist = []
        for asset in assets.values():
            if (
                asset.availability != AssetAvailability.AVAILABLE
                or isinstance(checked.get(asset.ref), InputAssessment)
                or not _matches_type(asset.metadata, expected)
            ):
                continue
            result = assess(role, asset)
            if result.status is not InputReadiness.BLOCKED and result.code != "INPUT_SCOPE_DISJOINT":
                shortlist.append(result)
        candidates[role] = tuple(shortlist)
    return TaskContext(task_id=task_id, goal=goal, method=method.ref, roles=roles, criteria=criteria, candidates=candidates)
