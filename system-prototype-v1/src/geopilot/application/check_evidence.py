"""Collect the actual input, process and registered artifact check evidence."""

from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue, ValidationError

from geopilot.application.run_checks import RunCheckContext
from geopilot.checks.artifact_checks import ArtifactRule, check_run_artifacts
from geopilot.checks.input_checks import InputCheckResult
from geopilot.checks.process_checks import check_run_process
from geopilot.execution.step_support import resolve_step_path
from geopilot.tools.registry import ToolRegistry


def _input_findings(context: RunCheckContext) -> list[dict[str, JsonValue]]:
    findings = []
    for step_key, step in context.steps.items():
        if step.log_path is None:
            continue
        path = resolve_step_path(context.workdir, step.log_path, field=f"steps.{step_key}.log_path")
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.startswith("input_check: "):
                        continue
                    finding = InputCheckResult.model_validate_json(line.removeprefix("input_check: "))
                    findings.append({**finding.model_dump(mode="json"), "step_key": step_key})
        except OSError:
            # The process checker reports missing or unreadable step logs.
            continue
        except (ValidationError, UnicodeError):
            findings.append({
                "rule": "input_check_evidence_is_readable", "phase": "input", "step_key": step_key,
                "field": f"steps.{step_key}.log_path", "severity": "blocking", "passed": False,
                "observed": step.log_path, "expected": "A valid typed input check record",
                "message": "The input check log contains malformed evidence.",
            })
    return findings


def collect_check_findings(
    database_path: str | Path, run_id: str, context: RunCheckContext, *,
    registry: ToolRegistry, qgis_bindings: Mapping | None, python_implementations: Mapping | None,
) -> dict[str, list[dict[str, JsonValue]]]:
    inputs = _input_findings(context)
    process = check_run_process(
        database_path, run_id, registry=registry, qgis_bindings=qgis_bindings,
        python_implementations=python_implementations,
    )
    artifacts = check_run_artifacts(database_path, run_id, rules={})
    return {
        "input": inputs,
        "process": [item.model_dump(mode="json") for item in process],
        "artifact": [item.model_dump(mode="json") for item in artifacts],
    }


def check_named_artifact_rule(
    database_path: str | Path, run_id: str, name: str, check: ArtifactRule,
) -> tuple[dict[str, JsonValue], list[dict[str, JsonValue]]]:
    findings = [
        item.model_dump(mode="json")
        for item in check_run_artifacts(database_path, run_id, rules={name: check})
        if item.rule == name
    ]
    if not findings:
        raise ValueError(f"Artifact check {name!r} returned no evidence")
    failures = [item for item in findings if item["passed"] is False]
    blocking = any(item["severity"] == "blocking" for item in failures)
    passed = False if failures else True if any(item["passed"] is True for item in findings) else None
    verdict = {
        "passed": passed,
        "severity": "blocking" if blocking or not failures else "warning",
        "message": findings[0]["message"] if len(findings) == 1 else "See the individual artifact findings.",
        "observed": findings[0]["observed"] if len(findings) == 1 else [item["observed"] for item in findings],
        "expected": findings[0]["expected"] if len(findings) == 1 else [item["expected"] for item in findings],
    }
    return verdict, findings
