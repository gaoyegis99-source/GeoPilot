import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from geopilot.application.plan_runner import execute_claimed_run
from geopilot.checks.cache_reuse import verify_reuse_source
from geopilot.checks.process_checks import check_run_process
from geopilot.domain.step_io import ExecutionStatus, StepResult, StepReuse
from geopilot.domain.tool_contract import ToolContract
from geopilot.persistence.run_repository import get_run, get_run_step, save_run_step, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.compiler import compile_step_request
from geopilot.plans.models import AnalysisPlan
from geopilot.runs.models import CheckStatus, RunStep
from geopilot.runs.service import create_run
from geopilot.scheduler.loop import claim_next_run, run_once
from geopilot.tools.registry import ToolRegistry


WORKER = """
import json
import sys
from pathlib import Path

request = json.loads(Path(sys.argv[2]).read_text())
Path(request['output_targets']['summary']['path']).write_text('{"value":1}')
Path(sys.argv[4]).write_text(json.dumps({'outputs': request['output_targets']}))
"""


@pytest.fixture
def reused_run(tmp_path):
    database = tmp_path / "project.sqlite"
    workspace = tmp_path / "workspace"
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER, encoding="utf-8")
    tool = ToolContract.model_validate({
        "id": "python-reuse-test", "version": 1, "runner": "python", "implementation": "test.reuse",
        "inputs": {}, "outputs": {"summary": "json"}, "parameters": {}, "preconditions": [],
        "measurement_policy": {"distance": "not_applicable", "area": "not_applicable"},
        "check_rules": ["output_exists"],
    })
    registry = ToolRegistry(python_implementations=(tool.implementation,))
    registry.register(tool)
    plan = AnalysisPlan.model_validate({
        "id": "plan", "task_id": "task", "version": 1, "status": "confirmed", "goal": {"summary": "Summarize"},
        "inputs": {}, "critical_options": {}, "method": {"id": "reuse-test", "version": 1}, "issues": [],
        "steps": [{"key": "summarize", "tool": tool.ref, "inputs": {}, "parameters": {},
                   "outputs": {"summary": {"type": "json", "target": "outputs/summary.json"}}}],
        "expected_artifacts": [{"role": "summary", "step": "summarize", "output": "summary", "type": "json"}],
        "check_rules": ["output_exists"],
    })
    initialize_database(database)
    now = datetime.now(UTC)
    with open_database(database) as connection, connection:
        connection.execute("INSERT INTO task VALUES ('task', ?, NULL, ?)", (plan.goal.model_dump_json(), now.isoformat()))
        connection.execute("INSERT INTO analysis_plan VALUES ('plan', 1, 'task', 'confirmed', ?, ?)",
                           (plan.model_dump_json(), now.isoformat()))
    source_id = create_run(database, plan.id, plan.version, workspace_root=workspace)
    assert run_once(database, lambda run: execute_claimed_run(
        database, run, workspace_root=workspace, registry=registry, python_implementations={tool.implementation: worker},
    )) == source_id
    with open_database(database, read_only=True) as connection:
        source = get_run(connection, source_id)
        original = get_run_step(connection, source_id, "summarize")
    assert (source.execution_status, source.check_status) == (ExecutionStatus.SUCCEEDED, CheckStatus.PASSED)
    assert original.result.exit_code == 0 and original.result.reuse is None

    run_id = create_run(database, plan.id, plan.version, workspace_root=workspace)
    run = claim_next_run(database)
    assert run.id == run_id
    workdir = Path(run.workdir)
    request = compile_step_request(plan, "summarize", run_id=run_id, asset_files={}, prior_results={})
    output = original.result.outputs["summary"]
    payload = (Path(source.workdir) / output.path).read_bytes()
    (workdir / output.path).write_bytes(payload)
    reuse = StepReuse(cache_key="a" * 64, source_run_id=source_id, source_step_key=original.step_key,
                      output_sha256=sha256(payload).hexdigest())
    result = StepResult(
        run_id=run_id, step_key=request.step_key, status=ExecutionStatus.SUCCEEDED, exit_code=None,
        outputs={"summary": output}, log_path=request.log_path, warnings=[], error=None,
        started_at=now, finished_at=now, environment={}, reuse=reuse,
    )
    log = workdir / request.log_path
    log.parent.mkdir()
    log.write_text(f"cache reuse: {reuse.cache_key} from {source_id}/{original.step_key}\n", encoding="utf-8")
    log.with_name("request.json").write_text(request.model_dump_json(), encoding="utf-8")
    log.with_name("result.json").write_text(result.model_dump_json(), encoding="utf-8")
    with open_database(database) as connection, connection:
        save_run_step(connection, RunStep(
            run_id=run_id, step_key=request.step_key, position=0, status=result.status, request=request, result=result,
            log_path=result.log_path, started_at=result.started_at, finished_at=result.finished_at,
        ))
    return database, registry, source, original, run, request, result


def test_reused_step_validates_persisted_and_disk_evidence_without_a_process_verdict(reused_run):
    database, registry, source, _, run, request, result = reused_run
    assert verify_reuse_source(database, result.reuse, request)
    assert not verify_reuse_source(database, result.reuse.model_copy(update={"source_run_id": run.id}), request)

    checks = check_run_process(database, run.id, registry=registry)
    assert checks and all(check.passed for check in checks), checks
    assert {check.field for check in checks if check.rule == "step_cache_reuse_matches"} == {
        "log.reuse_marker", "reuse.output_sha256", "reuse.source",
    }
    assert {check.rule for check in checks} >= {"step_request_matches", "step_result_matches"}
    assert all(check.rule not in {"step_process_matches", "step_environment_matches"} for check in checks)
    assert any(check.rule == "step_process_matches" and check.passed
               for check in check_run_process(database, source.id, registry=registry))


@pytest.mark.parametrize(("fault", "field"), [
    ("marker", "log.reuse_marker.0"), ("current_output", "reuse.output_sha256"),
    ("source_output", "reuse.source"), ("huge_source_output", "reuse.source"), ("unpassed_source", "reuse.source"),
    ("tool_version", "reuse.source"), ("source_parameters", "reuse.source"), ("reused_source", "reuse.source"),
    ("disk_request", "request_file.parameters.changed"), ("disk_result", "result_file.reuse.cache_key"),
])
def test_cache_reuse_checks_reject_inconsistent_evidence(reused_run, fault, field):
    database, registry, source, original, run, request, result = reused_run
    log = Path(run.workdir) / request.log_path
    output_path = result.outputs["summary"].path
    if fault == "marker":
        log.write_text("cache reuse: wrong marker\n", encoding="utf-8")
    elif fault == "current_output":
        (Path(run.workdir) / output_path).write_bytes(b'{"value":2}')
    elif fault == "source_output":
        (Path(source.workdir) / output_path).write_bytes(b'{"value":2}')
    elif fault == "huge_source_output":
        with (Path(source.workdir) / output_path).open("wb") as stream:
            stream.truncate(10**9)
    elif fault == "disk_request":
        path = log.with_name("request.json")
        document = json.loads(path.read_bytes())
        document["parameters"]["changed"] = True
        path.write_text(json.dumps(document), encoding="utf-8")
    elif fault == "disk_result":
        path = log.with_name("result.json")
        document = json.loads(path.read_bytes())
        document["reuse"]["cache_key"] = "b" * 64
        path.write_text(json.dumps(document), encoding="utf-8")
    else:
        with open_database(database) as connection, connection:
            if fault == "unpassed_source":
                update_run(connection, source.model_copy(update={"check_status": CheckStatus.CHECKING}))
            elif fault == "tool_version":
                tool = original.request.tool.model_copy(update={"version": 2})
                save_run_step(connection, original.model_copy(update={
                    "request": original.request.model_copy(update={"tool": tool}),
                }))
            elif fault == "source_parameters":
                save_run_step(connection, original.model_copy(update={
                    "request": original.request.model_copy(update={"parameters": {"changed": True}}),
                }))
            else:
                reused_source = original.result.model_copy(update={"reuse": result.reuse, "exit_code": None})
                save_run_step(connection, original.model_copy(update={"result": reused_source}))

    checks = check_run_process(database, run.id, registry=registry)
    assert any(check.field == field and not check.passed for check in checks), checks
    assert all(check.rule != "step_process_matches" for check in checks)
