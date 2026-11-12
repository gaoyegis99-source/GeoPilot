from pathlib import Path

from geopilot.application.method_preconditions import METHOD_PRECONDITIONS
from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database
from geopilot.plans.draft_builder import build_draft


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"


def _draft(tmp_path, method_id, version, layers, options):
    database = tmp_path / "assets.sqlite"
    initialize_database(database)
    method = load_method_catalog(ROOT / "config/methods").get(method_id, version)
    assets = {role: register_asset(database, SOURCE, layer=layer) for role, layer in layers.items()}
    context = build_task_context(
        database, task_id="method-check", goal=TaskGoal(summary="Check a configured method"),
        method=method, selected={role: asset.ref for role, asset in assets.items()},
    )
    return build_draft(context, method, plan_id="plan-check", options=options), assets


def test_point_count_uses_the_confirmed_crs_for_real_analysis_extent_targets_and_zones(tmp_path):
    plan, assets = _draft(
        tmp_path, "road-buffer-statistics", 2,
        {role: role for role in ("analysis_extent", "roads", "targets", "zones")},
        {
            "distance_m": 500, "distance_method": "local_projected", "distance_crs_ref": "EPSG:32650",
            "spatial_relation": "intersects", "boundary_assignment": "centroid",
            "boundary_tie": "unassigned", "statistic": "count", "output_scope": "clipped_to_analysis_extent",
        },
    )
    check = METHOD_PRECONDITIONS["point_count_inputs_match_distance_crs"]
    assert check(plan, assets) == ()
    mismatched = plan.model_copy(update={"critical_options": {**plan.critical_options, "distance_crs_ref": "EPSG:32649"}})

    issues = check(mismatched, assets)

    assert {issue.field for issue in issues} == {"inputs.analysis_extent.crs", "inputs.targets.crs", "inputs.zones.crs"}
    assert all("EPSG:32650" in issue.message for issue in issues)


def test_attribute_filter_requires_the_declared_field_on_the_real_selected_layer(tmp_path):
    plan, assets = _draft(
        tmp_path, "attribute-filter-export", 1, {"features": "targets"},
        {"field_name": "source_feature_id", "match_value": "building-west"},
    )
    check = METHOD_PRECONDITIONS["selected_field_exists"]
    assert check(plan, assets) == ()
    missing = plan.model_copy(update={"critical_options": {**plan.critical_options, "field_name": "missing_column"}})

    issue, = check(missing, assets)

    assert issue.code == "METHOD_PRECONDITION_FAILED" and issue.field == "critical_options.field_name"
    assert "missing_column" in issue.message
