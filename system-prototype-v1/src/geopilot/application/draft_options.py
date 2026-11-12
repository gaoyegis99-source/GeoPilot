"""Resolve method options that come from local GIS inputs."""

from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue

from geopilot.application.remote_sensing_plan import derived_remote_sensing_options, resolve_remote_sensing_draft_options
from geopilot.application.task_context import TaskContext
from geopilot.application.terrain_plan import derived_terrain_options, resolve_terrain_draft_options
from geopilot.methods.models import MethodRef
from geopilot.remote_sensing.config_registry import SensorConfigRegistry


def derived_method_options(method: MethodRef) -> tuple[str, ...]:
    return derived_terrain_options(method) + derived_remote_sensing_options(method)


def resolve_draft_options(
    database_path: str | Path, context: TaskContext, options: Mapping[str, JsonValue],
    *, sensor_registry: SensorConfigRegistry | None = None,
) -> tuple[TaskContext, Mapping[str, JsonValue]]:
    context, options = resolve_terrain_draft_options(database_path, context, options)
    return resolve_remote_sensing_draft_options(database_path, context, options, sensor_registry=sensor_registry)
