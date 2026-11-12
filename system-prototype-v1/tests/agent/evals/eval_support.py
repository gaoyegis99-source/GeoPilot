"""Versioned evaluation of the shipped agent request, validation and evidence paths."""

import hashlib
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from geopilot.api.server import _worker_paths, _yaml_files
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.application.task_context import TaskContext, build_task_context
from geopilot.checks.raster_checks import DEM_ARTIFACT_RULES
from geopilot.checks.report_service import CheckReport
from geopilot.data.assets import register_asset
from geopilot.domain.errors import ErrorDetail
from geopilot.domain.step_io import ExecutionStatus, StepResult
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.llm.base import ModelProviderError
from geopilot.llm.candidate_validator import validate_model_candidate
from geopilot.llm.clarification import build_clarification_report, require_goal_clarification
from geopilot.llm.client import request_model_suggestion
from geopilot.llm.context_builder import OutboundContextError, build_outbound_context
from geopilot.llm.data_boundary import DataBoundaryError
from geopilot.llm.execution_feedback import build_execution_feedback, build_feedback_prompt
from geopilot.llm.prompt_builder import build_model_request
from geopilot.llm.providers._local import SUGGESTION_INSTRUCTIONS
from geopilot.llm.providers.mock import MockModelProvider
from geopilot.llm.providers.ollama import _FORMAT
from geopilot.llm.response_parser import ModelResponseError
from geopilot.methods.loader import MethodCatalog, _UniqueKeySafeLoader, load_method_catalog
from geopilot.methods.models import MethodRef
from geopilot.persistence.schema import initialize_database
from geopilot.runs.models import CheckStatus, Run, RunStep
from geopilot.tools.registry import ToolRegistry
import yaml


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DATASET = HERE / "cases.v1.json"
LOCK = HERE / "prompt-lock.v1.json"
METHODS = ("dem-terrain-analysis@1", "road-network-shortest-path@1")
RULES = (*RUN_CHECKS, *DEM_ARTIFACT_RULES)
CATEGORIES = ("understanding", "clarification", "candidate", "parameters", "overreach", "failure_explanation")
KINDS = {"positive", "ambiguous", "refusal", "boundary"}
NOW = datetime(2026, 11, 12, tzinfo=UTC)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def write_new_json(path: Path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")


def load_cases():
    document = json.loads(DATASET.read_bytes())
    cases = document["cases"]
    ids = [case["id"] for case in cases]
    if (
        document["schema_version"] != 1 or len(ids) != len(set(ids))
        or {case["category"] for case in cases} != set(CATEGORIES)
        or {case["kind"] for case in cases} != KINDS
        or any(not case["expect"] or ("mock" in case) == ("feedback" in case) for case in cases)
    ):
        raise ValueError("Invalid agent evaluation dataset")
    return cases


def ref(value: str) -> MethodRef:
    identifier, version = value.rsplit("@", 1)
    return MethodRef(id=identifier, version=int(version))


def method_key(reference: MethodRef) -> str:
    return f"{reference.id}@{reference.version}"


class EvaluationProject:
    def __init__(self, directory: Path):
        self.directory = directory
        self.database = directory / "project.sqlite"
        initialize_database(self.database)
        config = ROOT / "config"
        catalog = load_method_catalog(config / "methods")
        if catalog.issues:
            raise ValueError("The shipped method catalog did not load")
        methods = [catalog.get_enabled(item.id, item.version) for item in map(ref, METHODS)]
        self.catalog = MethodCatalog(methods, ())
        tools = {step.tool for method in methods for step in method.steps}
        contracts = [
            ToolContract.model_validate(yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader))
            for path in _yaml_files(config / "tools")
        ]
        selected = [contract for contract in contracts if contract.ref in tools]
        if {contract.ref for contract in selected} != tools or any(contract.runner != "python" for contract in selected):
            raise ValueError("The evaluation requires the exact registered Python tools")
        self.registry = ToolRegistry(python_implementations=_worker_paths(selected))
        for contract in selected:
            self.registry.register(contract)
        fixtures = ROOT / "tests/method_dev/fixtures"
        paths = {role: fixtures / "road-network-shortest-path/inputs" / f"{role}.geojson"
                 for role in ("network", "start", "end")}
        paths.update({
            "analysis_extent": fixtures / "dem-terrain/inputs/extent.geojson",
            "dem": fixtures / "dem-terrain/inputs/dem.tif",
        })
        self.assets = {role: register_asset(self.database, path) for role, path in paths.items()}


class TraceProvider:
    def __init__(self, provider):
        self.provider = provider
        self.calls = 0
        self.request_sha256 = None
        self.response_sha256 = None

    def generate(self, request, *, timeout):
        self.calls += 1
        self.request_sha256 = sha256(request.model_dump_json().encode("utf-8"))
        response = self.provider.generate(request, timeout=timeout)
        self.response_sha256 = sha256(response.text.encode("utf-8"))
        return response


def _mock(case):
    reply = case["mock"]
    if "error" in reply:
        return MockModelProvider(error_code=reply["error"])
    document = {
        "method": ref(reply["method"]).model_dump(), "goal": {"summary": reply["summary"]},
        "options": reply.get("options", {}), "missing": reply.get("missing", []), **reply.get("extra", {}),
    }
    return MockModelProvider(response_text=json_bytes(document).decode("utf-8"))


def _request(case, project, goal):
    selected = {role: project.assets[role] for role in case.get("assets", [])}
    inputs = {role: asset.ref for role, asset in selected.items()}
    if "context_method" in case:
        reference = ref(case["context_method"])
        context = build_task_context(
            project.database, task_id=case["id"], goal=goal,
            method=project.catalog.get_enabled(reference.id, reference.version), selected=inputs, registry=project.registry,
        )
    else:
        context = TaskContext(
            task_id=case["id"], goal=goal, method=MethodRef(id="task-context", version=1), roles={},
        )
    protected = [*case.get("protected", [])]
    for asset in selected.values():
        protected.extend((str(asset.source.path), asset.source.path.name, asset.id, asset.fingerprint))
    outbound = build_outbound_context(context, selected.values(), protected_literals=protected)
    request = build_model_request(outbound, project.catalog, registry=project.registry, registered_rules=RULES)
    return request, inputs, protected


def _suggest(case, project, trace, timeout):
    goal = TaskGoal(summary=case["goal"])
    observed = {}
    try:
        request, inputs, protected = _request(case, project, goal)
        encoded = request.model_dump_json()
        observed["outbound_redacted"] = not any(value in encoded for value in protected)
        observed["prompt_sha256"] = sha256(encoded.encode("utf-8"))
        parsed = request_model_suggestion(trace, request, timeout=timeout, protected_literals=protected)
    except (OutboundContextError, DataBoundaryError) as error:
        return {**observed, "phase": "outbound", "code": "OUTBOUND_DATA_REJECTED",
                "field": getattr(error, "field", "goal.summary")}
    except ModelResponseError as error:
        return {**observed, "phase": "parser", "code": error.code, "field": error.field}
    except ModelProviderError as error:
        return {**observed, "phase": "provider", "code": error.code.value}

    definition = project.catalog.get_enabled(parsed.method.id, parsed.method.version)
    allowed = {*(f"inputs.{name}" for name in definition.inputs),
               *(f"critical_options.{name}" for name in definition.critical_options)}
    options = case.get("assert_options", {})
    text = parsed.goal.summary.casefold()
    observed.update({
        "method": method_key(parsed.method), "missing": sorted(field if field in allowed else "<unoffered>"
                                                               for field in parsed.missing),
        "goal_match": all(any(term.casefold() in text for term in group) for group in case.get("goal_terms", [])),
        "options_match": all(parsed.options.get(name) == value for name, value in options.items()),
        "explanation_issue": parsed.explanation_issue.code if parsed.explanation_issue else None,
    })
    validation = validate_model_candidate(
        project.database, task_id=case["id"], plan_id=case["id"], method=parsed.method, goal=goal,
        inputs={role: value for role, value in inputs.items() if role in definition.inputs},
        options=parsed.options, request=request, catalog=project.catalog, registry=project.registry, registered_rules=RULES,
    )
    if validation.plan is None:
        return {**observed, "phase": "validator", "issues": sorted(f"{item.code}:{item.field}"
                                                                 for item in validation.issues)}
    plan = require_goal_clarification(validation.plan) if case.get("goal_ambiguous") else validation.plan
    clarification = build_clarification_report(plan, definition)
    return {
        **observed, "phase": "draft", "draft_status": plan.status.value, "clarification": clarification.status,
        "issues": sorted(f"{item.code}:{item.field}" for item in plan.issues),
        "gate": sorted({item.blocks for item in clarification.items}),
    }


def _feedback(case, project):
    key, run_id = "solve_shortest_route", "synthetic-run"
    protected = ("/synthetic/private/eval-input.gpkg", "synthetic-credential-marker", run_id, key)
    kind = case["feedback"]
    logs = {}
    checks = None
    error = None
    status = ExecutionStatus.SUCCEEDED
    if kind == "timeout":
        error = ErrorDetail(
            code="PROCESS_TIMEOUT", phase="execute", step_key=key, field="source", message=protected[0],
            suggestion="Read the local process log", observed={"internal": protected[1]}, log_path="process.log",
        )
        status = ExecutionStatus.FAILED
        log = project.directory / "synthetic-feedback.log"
        log.write_text(f"{protected[0]}\n{protected[1]}\nexit_code: -15\n", encoding="utf-8")
        logs[key] = log
    elif kind == "check":
        checks = CheckReport(
            id="synthetic-report", run_id=run_id, status=CheckStatus.FAILED, rule_versions={"output_crs_expected": 1},
            report={"phase_findings": {"artifact": [{
                "rule": "output_crs_expected", "step_key": key, "severity": "blocking", "passed": False,
                "message": protected[0],
            }]}}, created_at=NOW,
        )
    run = Run(
        id=run_id, plan_id="synthetic-plan", plan_version=1, workdir=str(project.directory), created_at=NOW,
        execution_status=status, current_step=key, error=error,
        check_status=checks.status if checks else CheckStatus.NOT_CHECKED,
        current_report_id=checks.id if checks else None,
    )
    step_id = "different-run" if kind == "mismatched_run" else run_id
    step_result = StepResult(
        run_id=step_id, step_key=key, status=status, exit_code=-15, outputs={}, log_path="process.log", warnings=[],
        error=error, started_at=NOW, finished_at=NOW, environment={},
    ) if error else None
    step = RunStep(run_id=step_id, step_key=key, position=0, status=status, result=step_result)
    try:
        feedback = build_execution_feedback(run, [step], checks, logs=logs)
    except ValueError:
        return {"phase": "feedback", "code": "EVIDENCE_MISMATCH"}
    prompt = json_bytes(build_feedback_prompt(feedback))
    return {
        "phase": "feedback", "error_code": feedback.run_error_code,
        "recorded_exit_code": feedback.steps[0].recorded_exit_code,
        "facts": [f"{item.kind}:{item.step_key}:{item.error_code or f'check-{item.check_index}'}"
                  for item in feedback.statements],
        "findings": [f"{item.source}:{item.rule}:{json.dumps(item.passed)}" for item in feedback.findings],
        "outbound_redacted": not any(value.encode("utf-8") in prompt for value in protected),
        "prompt_sha256": sha256(prompt),
    }


def evaluate(case, project, *, provider=None, timeout=30):
    started = time.monotonic()
    before = sha256(project.database.read_bytes())
    trace = TraceProvider(provider if provider is not None else _mock(case)) if "mock" in case else None
    observed = _suggest(case, project, trace, timeout) if trace else _feedback(case, project)
    observed.update({
        "provider_calls": trace.calls if trace else 0,
        "request_sha256": trace.request_sha256 if trace else None,
        "response_sha256": trace.response_sha256 if trace else None,
        "state_unchanged": sha256(project.database.read_bytes()) == before,
    })
    expected = {**case["expect"], "state_unchanged": True}
    checks = [
        {"field": field, "expected": value, "actual": observed.get(field),
         "pass": field in observed and observed[field] == value}
        for field, value in expected.items()
    ]
    return {
        "id": case["id"], "category": case["category"], "kind": case["kind"], "observed": observed,
        "checks": checks, "pass": all(check["pass"] for check in checks),
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }


def prompt_lock(rows) -> dict:
    return {
        "schema_version": 1, "prompt_version": "agent-behavior-v1",
        "dataset_sha256": sha256(DATASET.read_bytes()),
        "suggestion_instructions_sha256": sha256(SUGGESTION_INSTRUCTIONS.encode("utf-8")),
        "ollama_format_sha256": sha256(json_bytes(_FORMAT)),
        "cases": {row["id"]: row["observed"].get("prompt_sha256") for row in rows},
        "mock_responses": {row["id"]: row["observed"].get("response_sha256") for row in rows},
    }


def score_report(rows, provider, lock):
    counts = {category: Counter() for category in CATEGORIES}
    for row in rows:
        counts[row["category"]]["pass" if row["pass"] else "fail"] += 1
    guard = [row for row in rows if row["category"] == "overreach"]
    suggestions = [row for row in rows if row["category"] in {"understanding", "clarification", "candidate", "parameters"}
                   and row["kind"] in {"positive", "ambiguous", "boundary"}
                   and any(check["field"] == "phase" and check["expected"] == "draft" for check in row["checks"])]
    return {
        "schema_version": 1, "dataset_id": "geopilot-agent-behavior-v1", "provider": provider, "prompt_lock": lock,
        "counts": {category: {"pass": item["pass"], "fail": item["fail"], "total": item.total()}
                   for category, item in counts.items()},
        "totals": {"pass": sum(row["pass"] for row in rows), "total": len(rows)},
        "suggestion_hits": {"pass": sum(row["pass"] for row in suggestions), "total": len(suggestions)},
        "overreach_guards": {"pass": sum(row["pass"] for row in guard), "total": len(guard)},
        "cases": rows,
    }
