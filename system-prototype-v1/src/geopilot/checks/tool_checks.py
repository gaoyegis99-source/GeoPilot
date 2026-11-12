"""Evaluate versioned tool rules against the concrete inputs and outputs of one step."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue
from pyproj import CRS
from pyproj.exceptions import CRSError

from geopilot.application.run_checks import RunCheckContext, _DATA_ERRORS, _point_count_summary
from geopilot.data import csv_output
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.data.vector_reader import read_layer_metadata
from geopilot.domain.step_io import StepFileRef, StepOutput, StepRequest, StepResult
from geopilot.execution.step_support import AdapterError, resolve_step_path
from geopilot.methods.models import AssetRoleBinding
from geopilot.plans.models import AnalysisPlan, PlanStep, ToolCheckRef
from geopilot.scheduler.output_resolver import resolve_named_output


@dataclass(frozen=True, slots=True)
class ToolCheckContext:
    run: RunCheckContext
    step: PlanStep
    request: StepRequest
    result: StepResult


@dataclass(frozen=True, slots=True)
class ToolFinding:
    field: str
    passed: bool
    observed: JsonValue
    expected: JsonValue
    message: str


ToolCheck = Callable[[ToolCheckContext], tuple[ToolFinding, ...]]
_READ_ERRORS = (*_DATA_ERRORS, AdapterError, CRSError)


def _field(context: ToolCheckContext, output: str) -> str:
    return f"steps.{context.step.key}.outputs.{output}"


def _output(context: ToolCheckContext, role: str) -> StepOutput:
    declared = context.step.outputs[role]
    output = context.result.outputs.get(role)
    field = _field(context, role)
    if output is None:
        raise ValueError(f"No output was reported for {field}")
    if (output.path, output.type) != (declared.target, declared.type) or (
        declared.layer is not None and declared.layer != output.layer
    ):
        raise ValueError(f"The reported output for {field} differs from the confirmed target")
    resolve_named_output(context.run.workdir, output, field=field)
    return output


def _output_exists(context: ToolCheckContext) -> tuple[ToolFinding, ...]:
    findings = []
    for role, declared in context.step.outputs.items():
        field = _field(context, role)
        expected = {"path": declared.target, "type": declared.type, "layer": declared.layer}
        try:
            output = _output(context, role)
        except _READ_ERRORS as error:
            findings.append(ToolFinding(field, False, None, expected, str(error)))
        else:
            findings.append(ToolFinding(field, True, output.model_dump(mode="json"), expected, "Named output reopened"))
    return tuple(findings)


def _crs(workdir: Path, reference: StepFileRef | StepOutput, field: str) -> CRS:
    path = resolve_step_path(workdir, reference.path, field=field)
    metadata = (
        read_raster_metadata(path) if path.suffix.lower() in {".tif", ".tiff", ".cog"}
        else read_layer_metadata(path, reference.layer)
    )
    known = getattr(metadata, "crs", None)
    if known is None:
        raise ValueError(f"Known CRS required for {field}")
    return CRS.from_user_input(known)


def _output_crs_expected(context: ToolCheckContext) -> tuple[ToolFinding, ...]:
    source_field = f"steps.{context.step.key}.inputs.source.crs"
    try:
        target = context.request.parameters.get("target_crs")
        expected = CRS.from_user_input(target) if target is not None else _crs(
            context.run.workdir, context.request.inputs["source"], source_field,
        )
    except _READ_ERRORS as error:
        return (ToolFinding(source_field, False, None, "Known input or confirmed target CRS", str(error)),)
    findings = []
    for role, declared in context.step.outputs.items():
        if not declared.type.startswith(("vector", "raster")):
            continue
        field = f"{_field(context, role)}.crs"
        try:
            actual = _crs(context.run.workdir, _output(context, role), field)
        except _READ_ERRORS as error:
            findings.append(ToolFinding(field, False, None, expected.to_string(), str(error)))
        else:
            passed = actual.equals(expected, ignore_axis_order=True)
            message = "Output CRS matches" if passed else "Output CRS differs from the expected CRS"
            findings.append(ToolFinding(field, passed, actual.to_string(), expected.to_string(), message))
    return tuple(findings)


def _area_csv_matches(context: ToolCheckContext, column: str) -> tuple[ToolFinding, ...]:
    expected = context.request.parameters.get(column)
    if not isinstance(expected, str) or not expected:
        return (ToolFinding(f"steps.{context.step.key}.parameters.{column}", False, expected, "Confirmed value",
                            f"The area check requires a confirmed {column}"),)
    findings = []
    for role, declared in context.step.outputs.items():
        if declared.type not in {"table_csv", "table"}:
            continue
        field = f"{_field(context, role)}.{column}"
        try:
            output = _output(context, role)
            path = resolve_step_path(context.run.workdir, output.path, field=field)
            values = set()
            rows = 0
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv_output.DictReader(stream, strict=True)
                if not reader.fieldnames or reader.fieldnames.count(column) != 1:
                    raise ValueError(f"The area CSV must contain one {column} column")
                for row in reader:
                    if None in row or None in row.values():
                        raise ValueError(f"Area CSV row {reader.line_num} has the wrong number of fields")
                    values.add(row[column])
                    rows += 1
            if column == "area_crs":
                crs = CRS.from_user_input(expected)
                passed = all(CRS.from_user_input(value).equals(crs, ignore_axis_order=True) for value in values)
            else:
                passed = values <= {expected}
            observed = {"row_count": rows, "values": sorted(values)}
            message = f"Area CSV values match {column}" if passed else f"Area CSV values differ for {column}"
            findings.append(ToolFinding(field, passed, observed, expected, message))
        except _READ_ERRORS as error:
            findings.append(ToolFinding(field, False, None, expected, str(error)))
    return tuple(findings)


def _detail_count_matches_summary(context: ToolCheckContext) -> tuple[ToolFinding, ...]:
    field = _field(context, "statistics")
    expected = {"missing_ids": [], "unexpected_ids": [], "duplicate_ids": [], "count_mismatch_rows": [],
                "summary_total_count": "selected_unique_count"}
    try:
        result = _point_count_summary(
            context.run, context.request.inputs["targets"], _output(context, "statistics"),
            targets_field=f"steps.{context.step.key}.inputs.targets", summary_field=field,
        )
    except _READ_ERRORS as error:
        return (ToolFinding(field, False, None, expected, str(error)),)
    return (ToolFinding(field, result.passed, result.observed, expected, result.message),)


TOOL_CHECKS: dict[tuple[str, int], ToolCheck] = {
    ("output_exists", 1): _output_exists,
    ("output_crs_expected", 1): _output_crs_expected,
    ("output_area_crs_expected", 1): lambda context: _area_csv_matches(context, "area_crs"),
    ("output_area_unit_expected", 1): lambda context: _area_csv_matches(context, "area_unit"),
    ("detail_count_matches_summary", 1): _detail_count_matches_summary,
}


def tool_check_version(name: str) -> int | None:
    return max((version for rule, version in TOOL_CHECKS if rule == name), default=None)


def tool_check_references(names: tuple[str, ...]) -> tuple[ToolCheckRef, ...] | None:
    """Return the current ordered rule versions for a valid tool declaration."""
    if len(set(names)) != len(names):
        return None
    references = []
    for name in names:
        version = tool_check_version(name)
        if version is None:
            return None
        references.append(ToolCheckRef(name=name, version=version))
    return tuple(references)


def tool_check_scopes(plan: AnalysisPlan) -> tuple[tuple[PlanStep, ToolCheckRef], ...]:
    return tuple((step, rule) for step in plan.steps for rule in step.tool_check_rules or ())


def tool_check_key(step: PlanStep, rule: ToolCheckRef) -> str:
    return f"steps.{step.key}.check_rules.{rule.name}"


def tool_file_paths(plan: AnalysisPlan, asset_files: Mapping[str, StepFileRef]) -> set[str]:
    """Find the distinct files supplying the confirmed tool checks."""
    outputs = {step.key: step.outputs for step in plan.steps}
    paths = set()
    for step in plan.steps:
        if not step.tool_check_rules:
            continue
        paths.update(output.target for output in step.outputs.values())
        for binding in step.inputs.values():
            paths.add(asset_files[binding.asset].path if isinstance(binding, AssetRoleBinding)
                      else outputs[binding.step][binding.output].target)
    return paths


def snapshot_tool_files(
    workdir: Path, paths: set[str], *, known_fingerprints: Mapping[str, str],
) -> dict[str, str | None]:
    """Record output and input file-group digests, including known sidecars."""
    fingerprints = {}
    for relative in sorted(paths):
        if relative in known_fingerprints:
            fingerprints[relative] = known_fingerprints[relative]
            continue
        try:
            path = resolve_step_path(workdir, relative, field=f"tool_files.{relative}")
            fingerprints[relative] = fingerprint_file(path).digest
        except (AdapterError, OSError):
            fingerprints[relative] = None
    return fingerprints
