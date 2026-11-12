import json
import os
import shlex
import shutil
from pathlib import Path

import pyogrio
import pytest
import yaml

from geopilot.application.plan_runner import execute_claimed_run
from geopilot.application.task_context import build_task_context
from geopilot.checks.process_checks import ProcessCheckResult, check_run_process
from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import MethodCatalog
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.run_repository import get_run, list_run_steps, save_run_step
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, save_draft
from geopilot.runs.service import create_run
from geopilot.scheduler.loop import run_once
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"


def _tool(name):
    return ToolContract.model_validate(yaml.safe_load((ROOT / f"config/tools/{name}.yaml").read_text(encoding="utf-8")))


def _execute(tmp_path, tool, registry, parameters, *, step_keys, python_implementations=None,
             qgis_bindings=None, qgis_process=None):
    db = tmp_path / "project.sqlite"
    workspace = tmp_path / "workspace"
    initialize_database(db)
    (output,) = tool.outputs
    method = MethodDefinition.model_validate({
        "id": "process-evidence", "version": 1, "locked_order": True,
        "inputs": {"roads": "vector_line"}, "critical_options": {},
        "steps": [
            {
                "key": key, "tool": tool.ref,
                "inputs": {"source": {"asset": "roads"} if index == 0 else {"step": step_keys[index - 1], "output": output}},
                "parameters": {name: {"literal": value} for name, value in parameters.items()},
                "outputs": tool.outputs,
            }
            for index, key in enumerate(step_keys)
        ],
        "expected_artifacts": [{"role": "roads", "step": step_keys[-1], "output": output, "type": tool.outputs[output]}],
        "check_rules": ["output_exists"],
    })
    source_before = fingerprint_file(SOURCE)
    asset = register_asset(db, SOURCE, layer="roads")
    context = build_task_context(
        db, task_id="process-task", goal=TaskGoal(summary="Validate actual process evidence"),
        method=method, selected={"roads": asset.ref}, registry=registry,
    )
    draft = build_draft(context, method, plan_id="process-plan")
    assert not draft.issues, draft.issues
    save_draft(db, draft, context=context)
    confirmed = confirm_plan(
        db, draft.id, draft.version, catalog=MethodCatalog([method], ()), registry=registry,
        registered_rules={"output_exists"},
    )
    run_id = create_run(db, confirmed.id, confirmed.version, workspace_root=workspace)

    assert run_once(db, lambda run: execute_claimed_run(
        db, run, workspace_root=workspace, registry=registry,
        python_implementations=python_implementations, qgis_bindings=qgis_bindings, qgis_process=qgis_process,
    )) == run_id
    with open_database(db, read_only=True) as connection:
        run = get_run(connection, run_id)
        steps = list_run_steps(connection, run_id)
    assert (run.execution_status, run.check_status) == ("SUCCEEDED", "PASSED"), run
    assert [step.step_key for step in steps] == list(step_keys)
    assert all(step.result.exit_code == 0 for step in steps)
    assert fingerprint_file(SOURCE) == source_before
    return db, run, steps


@pytest.fixture
def python_run(tmp_path):
    tool = _tool("python-prepare-measurement-lines")
    implementations = {tool.implementation: ROOT / "src/geopilot/gis/tools/prepare_measurement_lines.py"}
    registry = ToolRegistry(python_implementations=implementations)
    registry.register(tool)
    db, run, steps = _execute(
        tmp_path, tool, registry, {"method": "local_projected", "target_crs": "EPSG:32650"},
        step_keys=("prepare", "verify"), python_implementations=implementations,
    )
    return db, run, steps, registry, implementations


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_real_claimed_python_process_matches_confirmed_request_result_and_environment(python_run):
    db, run, steps, registry, implementations = python_run
    workdir = Path(run.workdir)
    result = steps[-1].result.outputs["roads"]
    data = pyogrio.read_dataframe(workdir / result.path, layer=result.layer)
    assert sorted(data.source_feature_id) == ["road-edge", "road-far", "road-main"]
    assert data.crs.to_epsg() == 32650
    log = workdir / steps[0].log_path
    lines = log.read_text(encoding="utf-8").splitlines()
    command = next(index for index, line in enumerate(lines) if line.startswith("$ "))
    argv = shlex.split(lines[command][2:])
    # Earlier logs recorded absolute protocol paths. The interpreter may also be relocated later.
    argv[0] = "/historical runtime/bin/python"
    argv[3] = str(workdir / argv[3])
    argv[5] = str(workdir / argv[5])
    lines[command] = "$ " + shlex.join(argv)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    checks = check_run_process(db, run.id, registry=registry, python_implementations=implementations)

    assert checks and all(isinstance(row, ProcessCheckResult) and row.passed for row in checks), checks
    assert all(row.phase == "process" and row.severity == "blocking" for row in checks)
    assert {row.step_key for row in checks if row.rule == "step_environment_matches"} == {"prepare", "verify"}


@pytest.mark.parametrize("fault,step_key,field", [
    ("tool", "prepare", "request.tool.version"),
    ("implementation", "verify", "log.implementation"),
    ("parameter", "verify", "request.parameters.target_crs"),
    ("order", "verify", "position"),
    ("environment", "verify", "environment.pyproj"),
    ("result", "verify", "result_file.environment.pyproj"),
    ("snapshot", None, "snapshot.plan.steps.1.parameters.target_crs"),
    ("handoff", "verify", "request_file.inputs.source.layer"),
    ("log", "verify", "log_path"),
])
def test_independent_evidence_deviations_are_localized(python_run, fault, step_key, field):
    db, run, steps, registry, implementations = python_run
    workdir = Path(run.workdir)
    first, second = steps
    second_log = workdir / second.log_path
    if fault == "tool":
        changed = first.request.model_copy(update={"tool": first.request.tool.model_copy(update={"version": 2})})
        with open_database(db) as connection, connection:
            save_run_step(connection, first.model_copy(update={"request": changed}))
    elif fault == "implementation":
        lines = second_log.read_text(encoding="utf-8").splitlines()
        command = next(index for index, line in enumerate(lines) if line.startswith("$ "))
        argv = shlex.split(lines[command][2:])
        argv[1] = str(ROOT / "src/geopilot/gis/tools/planar_area.py")
        lines[command] = "$ " + shlex.join(argv)
        second_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif fault == "parameter":
        # Two consistently altered request copies still disagree with the confirmed snapshot.
        parameters = {**second.request.parameters, "target_crs": "EPSG:32651"}
        changed = second.request.model_copy(update={"parameters": parameters})
        with open_database(db) as connection, connection:
            save_run_step(connection, second.model_copy(update={"request": changed}))
        _write(second_log.with_name("request.json"), changed.model_dump(mode="json"))
    elif fault == "order":
        with open_database(db) as connection, connection:
            connection.execute("UPDATE run_step SET position = 2 WHERE run_id = ? AND step_key = 'prepare'", (run.id,))
            connection.execute("UPDATE run_step SET position = 0 WHERE run_id = ? AND step_key = 'verify'", (run.id,))
            connection.execute("UPDATE run_step SET position = 1 WHERE run_id = ? AND step_key = 'prepare'", (run.id,))
    elif fault == "environment":
        path = second_log.with_name("python-result.json")
        manifest = _read(path)
        manifest["environment"]["pyproj"] = "0.0.0"
        _write(path, manifest)
    elif fault == "result":
        path = second_log.with_name("result.json")
        result = _read(path)
        result["environment"]["pyproj"] = "0.0.0"
        _write(path, result)
    elif fault == "snapshot":
        path = workdir / "request.json"
        snapshot = _read(path)
        snapshot["plan"]["steps"][1]["parameters"]["target_crs"] = "EPSG:32651"
        _write(path, snapshot)
    elif fault == "handoff":
        path = second_log.with_name("request.json")
        request = _read(path)
        request["inputs"]["source"]["layer"] = "wrong-layer"
        _write(path, request)
    else:
        second_log.unlink()

    checks = check_run_process(db, run.id, registry=registry, python_implementations=implementations)

    (match,) = [row for row in checks if not row.passed and (row.step_key, row.field) == (step_key, field)]
    assert match.observed != match.expected and match.phase == "process"
    with open_database(db, read_only=True) as connection:
        actual = get_run(connection, run.id)
    assert (actual.execution_status, actual.check_status) == ("SUCCEEDED", "PASSED")


def test_real_qgis_echo_matches_aliases_and_detects_engine_distance_deviation(tmp_path):
    executable = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if not executable:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS for actual QGIS process evidence")
    tool = _tool("qgis-buffer")
    registry = ToolRegistry(qgis_process=executable)
    registry.register(tool)
    bindings = yaml.safe_load((ROOT / "config/runner_bindings/qgis-buffer.yaml").read_text(encoding="utf-8"))
    db, run, (step,) = _execute(
        tmp_path, tool, registry,
        {"distance": 500, "segments": 16, "dissolve": True, "end_cap_style": "round", "join_style": "round", "miter_limit": 2},
        step_keys=("buffer",), qgis_bindings=bindings, qgis_process=executable,
    )
    initial = check_run_process(db, run.id, registry=registry, qgis_bindings=bindings)
    assert all(row.passed for row in initial), initial
    log = Path(run.workdir) / step.log_path
    original = log.read_text(encoding="utf-8")
    assert original.count('"DISTANCE": 500,') == 1
    log.write_text(original.replace('"DISTANCE": 500,', '"DISTANCE": 10,'), encoding="utf-8")

    checks = check_run_process(db, run.id, registry=registry, qgis_bindings=bindings)

    (changed,) = [row for row in checks if not row.passed and row.field == "engine.parameters.distance"]
    assert (changed.step_key, changed.observed, changed.expected) == ("buffer", 10, 500)
