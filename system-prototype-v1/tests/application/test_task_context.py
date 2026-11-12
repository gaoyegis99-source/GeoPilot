from pathlib import Path
from shutil import copyfile

import geopandas as gpd
import pyogrio
import pytest
import yaml
from shapely.geometry import box

from geopilot.application.task_context import InputReadiness, TaskContext, build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.data_asset import DataAssetRef
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.schema import initialize_database
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
ROADS = ROOT / "tests/fixtures/vector/road-buffer.gpkg"


def _method(inputs, steps=None):
    return MethodDefinition(
        id="context-method", version=1, locked_order=True, inputs=inputs, critical_options={},
        steps=steps or [{"key": "inspect"}], expected_artifacts=[], check_rules=[],
    )


def _context(database, method, selected, **options):
    return build_task_context(
        database, task_id="task-001", goal=TaskGoal(summary="Inspect selected data"),
        method=method, selected=selected, **options,
    )


def test_explicit_roles_reopen_real_versions_and_keep_missing_and_mismatched_inputs(tmp_path):
    database = tmp_path / "assets.sqlite"
    initialize_database(database)
    road = register_asset(database, ROADS, layer="roads")
    points = register_asset(database, ROADS, layer="targets")
    method = _method({"road": "vector_line", "parcel": "vector_polygon", "targets": "vector_point_or_polygon"})

    context = _context(database, method, {"road": road.ref, "parcel": points.ref})

    assert context.task_id == "task-001" and context.method == method.ref
    assert list(context.roles) == ["road", "parcel", "targets"]
    assert context.roles["road"].asset == road.ref
    assert context.roles["road"].status == InputReadiness.READY
    assert context.roles["parcel"].asset == points.ref
    assert context.roles["parcel"].status == InputReadiness.BLOCKED
    assert context.roles["parcel"].code == "METHOD_PRECONDITION_FAILED"
    assert context.roles["targets"].asset is None
    assert context.roles["targets"].status == InputReadiness.UNKNOWN
    assert context.roles["targets"].code == "INPUT_REQUIRED"
    assert TaskContext.model_validate_json(context.model_dump_json()) == context


def test_exact_missing_version_is_blocked_without_falling_back_to_latest(tmp_path):
    database = tmp_path / "assets.sqlite"
    initialize_database(database)
    road = register_asset(database, ROADS, layer="roads")
    unavailable = DataAssetRef(asset_id=road.id, asset_version=road.version + 1)

    context = _context(database, _method({"road": "vector_line"}), {"road": unavailable})

    assert context.roles["road"].asset == unavailable
    assert context.roles["road"].status == InputReadiness.BLOCKED
    assert context.roles["road"].code == "SOURCE_UNREADABLE"


@pytest.mark.parametrize(
    ("epsg", "status", "code"),
    [
        (3857, InputReadiness.CONDITIONAL, "MEASUREMENT_POLICY_REJECTED"),
        (None, InputReadiness.UNKNOWN, "CRS_UNKNOWN"),
        (6933, InputReadiness.READY, None),
    ],
)
def test_context_uses_real_measurement_requirements_of_bound_tools(tmp_path, epsg, status, code):
    database = tmp_path / "assets.sqlite"
    initialize_database(database)
    source = tmp_path / "parcels.gpkg"
    parcels = gpd.GeoDataFrame(geometry=[box(0, 0, 1000, 1000)], crs=epsg)
    if epsg is None:
        with pytest.warns(UserWarning, match="crs.*was not provided"):
            pyogrio.write_dataframe(parcels, source, layer="parcels")
    else:
        pyogrio.write_dataframe(parcels, source, layer="parcels")
    asset = register_asset(database, source, layer="parcels")
    contract = ToolContract.model_validate(
        yaml.safe_load((ROOT / "config/tools/python-area.yaml").read_text(encoding="utf-8"))
    )
    registry = ToolRegistry(python_implementations=[contract.implementation])
    registry.register(contract)
    method = _method({"parcel": "vector_polygon"}, [{
        "key": "measure", "tool": contract.ref,
        "inputs": {"source": {"asset": "parcel"}},
    }])

    context = _context(database, method, {"parcel": asset.ref}, registry=registry)

    assert context.roles["parcel"].status == status
    assert context.roles["parcel"].code == code
    if epsg == 3857:
        assert "Web Mercator" in context.roles["parcel"].message
    if epsg is None:
        attribute_context = _context(database, _method({"parcel": "vector_polygon"}), {"parcel": asset.ref})
        assert attribute_context.roles["parcel"].status == InputReadiness.READY


def test_context_reports_asset_content_change_against_selected_version(tmp_path):
    database = tmp_path / "assets.sqlite"
    initialize_database(database)
    source = tmp_path / "roads.gpkg"
    copyfile(ROADS, source)
    asset = register_asset(database, source, layer="roads")
    roads = pyogrio.read_dataframe(source, layer="roads", max_features=1)
    pyogrio.write_dataframe(roads, source, layer="roads", append=True)

    context = _context(database, _method({"road": "vector_line"}), {"road": asset.ref})

    assert context.roles["road"].status == InputReadiness.BLOCKED
    assert context.roles["road"].code == "INPUT_CHANGED"


def test_selection_requires_declared_role_names(tmp_path):
    database = tmp_path / "assets.sqlite"
    initialize_database(database)
    road = register_asset(database, ROADS, layer="roads")

    with pytest.raises(ValueError, match="undeclared"):
        _context(database, _method({"road": "vector_line"}), {"other": road.ref})
