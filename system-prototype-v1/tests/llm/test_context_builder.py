from pathlib import Path

import pytest

from geopilot.application.task_context import InputAssessment, TaskContext, build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.data_asset import FieldMetadata
from geopilot.domain.task import TaskGoal
from geopilot.llm.context_builder import OutboundContextError, build_outbound_context
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database


ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
RASTER = ROOT / "tests/fixtures/raster/dem-plane.tif"


@pytest.fixture
def registered(tmp_path):
    db = tmp_path / "catalog.sqlite"
    initialize_database(db)
    return db, register_asset(db, VECTORS, layer="targets"), register_asset(db, RASTER)


def test_builds_vector_summary_from_exact_locally_registered_version(registered):
    db, vector, raster = registered
    method = load_method_catalog(ROOT / "config/methods").get("attribute-filter-export", 1)
    context = build_task_context(
        db, task_id="private-task", goal=TaskGoal(summary="统计指定要素"),
        method=method, selected={"features": vector.ref},
    )
    wrong_metadata = vector.metadata.model_copy(update={"feature_count": 999})
    newer = vector.model_copy(update={"version": 2, "metadata": wrong_metadata})
    result = build_outbound_context(context, [newer, vector, raster])
    data = result.model_dump(mode="json", exclude_none=True)
    assert set(data) == {"goal", "roles"}
    assert data["goal"] == "统计指定要素"
    summary, = data["roles"]
    assert summary == {
        "role": "features", "readiness": "READY",
        "asset": {
            "kind": "vector", "geometry_type": "Point", "feature_count": 9,
            "fields": [{"name": "source_feature_id", "type": "string", "missing_ratio": 0.0}],
        },
    }
    outbound = result.model_dump_json()
    for prohibited in (str(vector.source.path), vector.id, vector.fingerprint, "private-task", "500200", "4400300"):
        assert prohibited not in outbound


def test_raster_uses_scalar_resolution_and_excludes_grid_coordinates_and_raw_messages(registered):
    _, _, raster = registered
    context = TaskContext(
        task_id="raster-task", goal={"summary": "计算坡度，保留 10 m 分辨率"}, method={"id": "terrain", "version": 1},
        roles={
            "dem": InputAssessment(asset=raster.ref, status="CONDITIONAL", code="LOCAL",
                                   message=f"Read {raster.source.path} at 500020,4400020"),
            "extent": InputAssessment(asset=None, status="UNKNOWN"),
        },
    )
    result = build_outbound_context(context, [raster]).model_dump(mode="json", exclude_none=True)
    dem, extent = result["roles"]
    assert dem == {
        "role": "dem", "readiness": "CONDITIONAL",
        "asset": {"kind": "raster", "width": 7, "height": 7, "band_count": 1,
                  "resolution_x": 10.0, "resolution_y": 10.0},
    }
    assert extent == {"role": "extent", "readiness": "UNKNOWN"}
    assert "500020" not in str(result) and "CONDITIONAL" in str(result)


def test_local_references_remove_source_paths_credentials_coordinates_and_samples(registered):
    _, vector, _ = registered
    context = TaskContext(
        task_id="task", method={"id": "attribute", "version": 1},
        goal={"summary": f"筛选 {vector.source.path} 的 building-west，参考 (117.25, 39.9)，连接 postgres://dev:secret@host/db"},
        roles={"features": InputAssessment(asset=vector.ref, status="READY")},
    )
    result = build_outbound_context(context, [vector], protected_literals=["building-west"])
    assert "筛选" in result.goal and "（本机引用）" in result.goal
    for prohibited in ("road-buffer.gpkg", "building-west", "117.25", "39.9", "secret", "host/db"):
        assert prohibited not in result.goal


def test_removes_quoted_paths_relative_files_and_inline_credentials(registered):
    context = TaskContext(
        task_id="task", method={"id": "attribute", "version": 1}, roles={},
        goal={"summary": r'筛选 "C:\Project Files\secret.txt" 和 survey/private.csv。token=private-token'},
    )
    goal = build_outbound_context(context, registered[1:]).goal
    assert "筛选" in goal
    for prohibited in ("Project Files", "secret.txt", "survey", "private.csv", "private-token"):
        assert prohibited not in goal


@pytest.mark.parametrize("goal", [
    'Use {"id": "row-001", "name": "actual sample"}',
    "Clip POINT Z (117 40 3)",
    "Use value\x00hidden",
])
def test_refuses_raw_records_geometry_and_controls_in_free_text(registered, goal):
    context = TaskContext(task_id="task", goal={"summary": goal}, method={"id": "attribute", "version": 1}, roles={})
    with pytest.raises(OutboundContextError, match="local references"):
        build_outbound_context(context, registered[1:])


def test_omits_unsafe_field_metadata_and_never_uses_another_asset_version(registered):
    _, vector, _ = registered
    fields = (
        FieldMetadata(name="district_name", dtype="/private/connections", missing_count=3),
        FieldMetadata(name="/private/database", dtype="string", missing_count=0),
    )
    shaped = vector.model_copy(update={"metadata": vector.metadata.model_copy(update={"fields": fields})})
    context = TaskContext(
        task_id="task", goal={"summary": "查看区域统计"}, method={"id": "attribute", "version": 1},
        roles={"features": InputAssessment(asset=vector.ref, status="READY")},
    )
    result = build_outbound_context(context, [shaped]).model_dump(mode="json", exclude_none=True)
    assert result["roles"][0]["asset"]["fields"] == [
        {"name": "district_name", "type": "other", "missing_ratio": pytest.approx(3 / 9)},
    ]
    result = build_outbound_context(context, [vector.model_copy(update={"version": 2})])
    assert result.roles[0].asset is None and result.roles[0].readiness == "UNKNOWN"
