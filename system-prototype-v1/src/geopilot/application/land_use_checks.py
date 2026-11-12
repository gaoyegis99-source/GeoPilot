"""Check the actual source mapping, overlay balance and published land-use totals."""

import json
import math
from collections.abc import Callable
from functools import wraps
from pathlib import Path

import pyogrio
import shapely
from pyogrio.errors import DataLayerError, DataSourceError
from pyproj import CRS
from pyproj.exceptions import ProjError
from shapely.errors import GEOSException

from geopilot.application.run_checks import RunCheck, RunCheckContext, RunCheckResult
from geopilot.domain.step_io import StepFileRef
from geopilot.execution.step_support import resolve_step_path
from geopilot.gis.vector.transition_matrix import TransitionEvidenceError, verify_transition_outputs


def _guard(check: Callable[[RunCheckContext], RunCheckResult]) -> RunCheck:
    @wraps(check)
    def checked(context: RunCheckContext) -> RunCheckResult:
        try:
            return check(context)
        except TransitionEvidenceError as error:
            return RunCheckResult(False, str(error), {
                "field": error.field, "observed": error.observed, "expected": error.expected,
            })
        except (OSError, ValueError, KeyError, TypeError, DataLayerError, DataSourceError, ProjError, GEOSException) as error:
            return RunCheckResult(False, f"Cannot verify land-use evidence. {error}")
    return checked


def _path(context: RunCheckContext, output: StepFileRef) -> Path:
    return resolve_step_path(context.workdir, output.path, field="checks.land_use")


def _vector(context: RunCheckContext, output: StepFileRef):
    return pyogrio.read_dataframe(_path(context, output), layer=output.layer)


def _document(context: RunCheckContext, output: StepFileRef) -> dict:
    value = json.loads(_path(context, output).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a land-use evidence object")
    return value


@_guard
def _classification_mapping_complete(context: RunCheckContext) -> RunCheckResult:
    options = context.plan.critical_options
    observed = {}
    passed = True
    for period in ("first", "second"):
        role = f"{period}_classes"
        source = _vector(context, context.asset_files[role])
        mapped = _vector(context, context.steps["map_classification_systems"].outputs[role])
        source_field = options[f"{period}_class_field"]
        mapping = options[f"{period}_class_mapping"]
        source_codes = source[source_field].astype(str)
        ids = source["source_feature_id"].astype(str)
        raw = dict(zip(ids, source_codes, strict=True))
        missing = sorted(set(raw.values()) - mapping.keys())
        expected = {key: (value, mapping.get(value), options[f"{period}_period"]) for key, value in raw.items()}
        actual = {
            str(key): (code, category, label) for key, code, category, label in
            mapped[["source_feature_id", "source_class_code", "class_code", "period"]].itertuples(index=False, name=None)
        }
        differences = sorted(key for key in expected.keys() | actual.keys() if expected.get(key) != actual.get(key))
        unique_ids = len(raw) == len(source) and len(actual) == len(mapped)
        observed[role] = {
            "source_count": len(source), "mapped_count": len(mapped), "unique_ids": unique_ids,
            "unmapped_codes": missing, "mismatching_ids": differences[:20], "mismatch_count": len(differences),
        }
        passed = passed and unique_ids and not missing and not differences
    return RunCheckResult(passed, "Compare both mapped layers with original codes and confirmed class maps.", observed)


def _area_settings(context: RunCheckContext) -> tuple[CRS, float]:
    overlay_step = next(step for step in context.plan.steps if step.key == "overlay")
    return CRS.from_user_input(overlay_step.parameters["area_crs"]), float(overlay_step.parameters["area_tolerance"])


@_guard
def _overlay_area_balanced(context: RunCheckContext) -> RunCheckResult:
    area_crs, tolerance = _area_settings(context)
    projected = context.steps["choose_equal_area_projection"].outputs
    evidence = context.steps["overlay"].outputs
    overlay = _vector(context, evidence["overlay_polygons"])
    extent = _vector(context, projected["analysis_extent"])
    report = _document(context, evidence["overlay_report"])
    scope = shapely.union_all(extent.geometry.array)
    period_metrics = {}
    unions = []
    for role in ("first_classes", "second_classes"):
        frame = _vector(context, projected[role])
        if frame.crs is None or not frame.crs.equals(area_crs, ignore_axis_order=True):
            raise ValueError(f"Projected {role} does not use the confirmed equal-area CRS")
        parts = shapely.intersection(frame.geometry.to_numpy(), scope)
        covered = shapely.union_all(parts)
        unions.append(covered)
        sum_area = math.fsum(shapely.area(parts))
        period_metrics[role] = {
            "clipped_area_sum_m2": sum_area, "covered_area_m2": covered.area,
            "overlap_area_m2": max(0.0, sum_area - covered.area), "uncovered_area_m2": scope.difference(covered).area,
        }
    observed = {
        "analysis_extent_m2": scope.area, "overlay_area_m2": math.fsum(overlay.geometry.area),
        "overlay_union_area_m2": shapely.union_all(overlay.geometry.array).area,
        "common_period_area_m2": unions[0].intersection(unions[1]).area,
    }
    deviations = [abs(value - float(report[key])) for key, value in observed.items()]
    deviations.extend(abs(value - scope.area) for value in observed.values())
    for role, metrics in period_metrics.items():
        deviations.extend(abs(value - float(report["periods"][role][key])) for key, value in metrics.items())
        deviations.extend(metrics[key] for key in ("overlap_area_m2", "uncovered_area_m2"))
    observed.update(periods=period_metrics, area_tolerance_m2=tolerance, fragment_count=len(overlay))
    crs_ok = all(frame.crs is not None and frame.crs.equals(area_crs, ignore_axis_order=True) for frame in (extent, overlay))
    passed = (
        crs_ok and not overlay.empty and overlay.geometry.is_valid.all()
        and CRS.from_user_input(report["area_crs"]).equals(area_crs, ignore_axis_order=True)
        and report["area_unit"] == "square_meter" and report["area_tolerance_m2"] == tolerance
        and report["fragment_count"] == len(overlay) and all(delta <= tolerance for delta in deviations)
    )
    return RunCheckResult(bool(passed), "Compare reopened coverage, overlap, geometry and overlay area with the confirmed tolerance.", observed)


@_guard
def _transition_totals_match(context: RunCheckContext) -> RunCheckResult:
    _, tolerance = _area_settings(context)
    allocations = _document(context, context.steps["apply_boundary_assignment"].outputs["allocations"])
    change_report = _document(context, context.steps["build_change_polygons"].outputs["change_report"])
    parameters = {step.key: step.parameters for step in context.plan.steps}
    for field, actual, expected in (
        ("allocations.boundary_assignment", allocations.get("boundary_assignment"),
         parameters["apply_boundary_assignment"]["boundary_assignment"]),
        ("change_report.minimum_change_area_m2", change_report.get("minimum_change_area_m2"),
         parameters["build_change_polygons"]["minimum_change_area"]),
        ("change_report.merge_adjacent_classes", change_report.get("merge_adjacent_classes"),
         parameters["build_change_polygons"]["merge_adjacent_classes"]),
    ):
        if actual != expected or isinstance(actual, bool) != isinstance(expected, bool):
            raise TransitionEvidenceError(field, observed=actual, expected=expected)
    observed = verify_transition_outputs(
        _vector(context, context.steps["overlay"].outputs["overlay_polygons"]),
        _vector(context, context.artifacts["change_polygons"]),
        allocations, change_report,
        _document(context, context.artifacts["transition_matrix"]),
        _document(context, context.artifacts["zone_statistics"]),
        area_tolerance_m2=tolerance,
    )
    return RunCheckResult(True, "Reconcile the actual land-use matrix, zones, changes and filtered source fragments.", observed)


LAND_USE_CHECKS: dict[str, RunCheck] = {
    "classification_mapping_complete": _classification_mapping_complete,
    "overlay_area_balanced": _overlay_area_balanced,
    "transition_totals_match": _transition_totals_match,
}
