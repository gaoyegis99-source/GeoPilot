import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pyogrio
import pytest
import yaml

from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.step_io import ExecutionStatus, StepFileRef, StepRequest, StepResult
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.methods.models import MethodDefinition
from geopilot.persistence.schema import initialize_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.plans.compiler import PlanCompilationError, compile_plan_steps, compile_step_request
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.models import AnalysisPlan
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
DISTANCE = {"value": 0.5, "unit": "kilometer"}


def _load_yaml(path):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def qgis():
    bundled = ROOT / ".environments/qgis-deb/qgis_process"
    executable = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    if executable is None and bundled.is_file():
        executable = str(bundled)
    if executable is None:
        pytest.skip("Set GEOPILOT_QGIS_PROCESS for the compiler's real QGIS workflow")
    registry = ToolRegistry(qgis_process=executable)
    for name in ("qgis-buffer", "qgis-select-points-by-location"):
        registry.register(ToolContract.model_validate(_load_yaml(f"config/tools/{name}.yaml")))
    buffer_with_default = _load_yaml("config/tools/qgis-buffer.yaml")
    buffer_with_default["version"] = 2
    buffer_with_default["parameters"]["segments"]["default"] = 16
    registry.register(ToolContract.model_validate(buffer_with_default))
    bindings = _load_yaml("config/runner_bindings/qgis-buffer.yaml")
    bindings.update(_load_yaml("config/runner_bindings/qgis-road-workflow.yaml"))
    return registry, executable, bindings


@pytest.fixture(scope="module")
def workflow(tmp_path_factory):
    database = tmp_path_factory.mktemp("compiler-assets") / "assets.sqlite"
    initialize_database(database)
    selected = {role: register_asset(database, SAMPLE, layer=role).ref for role in ("roads", "targets")}
    method = MethodDefinition.model_validate({
        "id": "road-point-selection", "version": 1, "locked_order": True,
        "inputs": {"roads": "vector_line", "targets": "vector_point"},
        "critical_options": {
            "distance": {"type": "number", "unit": "meter", "minimum_exclusive": 0},
            "relation": ["intersects", "within"],
        },
        "steps": [
            {"key": "create_buffer", "tool": {"id": "qgis-buffer", "version": 1},
             "inputs": {"source": {"asset": "roads"}},
             "parameters": {"distance": {"option": "distance"}, "segments": {"literal": 16},
                            "dissolve": {"literal": True}, "end_cap_style": {"literal": "round"},
                            "join_style": {"literal": "round"}, "miter_limit": {"literal": 2}},
             "outputs": {"buffer": "vector_polygon"}},
            {"key": "select_targets", "tool": {"id": "qgis-select-points-by-location", "version": 1},
             "inputs": {"source": {"asset": "targets"}, "area": {"step": "create_buffer", "output": "buffer"}},
             "parameters": {"predicate": {"option": "relation"}}, "outputs": {"targets": "vector_point"}},
        ],
        "expected_artifacts": [{"role": "selected", "step": "select_targets", "output": "targets", "type": "vector_point"}],
        "check_rules": ["output_exists"],
    })
    context = build_task_context(
        database, task_id="task-compiler", goal=TaskGoal(summary="筛选道路缓冲区内的点"),
        method=method, selected=selected,
    )
    draft = build_draft(context, method, plan_id="plan-compiler", options={"distance": 500, "relation": "intersects"})
    assert draft.issues == ()
    return method, draft


def _confirm(draft, steps=None):
    fields = draft.model_dump(mode="python")
    return AnalysisPlan.model_validate({**fields, "status": "confirmed", "steps": draft.steps if steps is None else steps})


def _quantity_draft(draft):
    data = draft.model_dump(mode="python")
    data["critical_options"]["distance"] = DISTANCE
    data["steps"][0]["parameters"]["distance"] = DISTANCE
    data["steps"][0]["outputs"]["buffer"]["layer"] = None
    return AnalysisPlan.model_validate(data)


def _expect_error(code, field, action):
    with pytest.raises(PlanCompilationError) as captured:
        action()
    assert (captured.value.code, captured.value.field) == (code, field)
    assert captured.value.message


def test_compile_and_run_actual_qgis_using_versioned_options_and_named_results(workflow, qgis, tmp_path):
    method, draft = workflow
    registry, executable, bindings = qgis
    draft = _quantity_draft(draft)
    before = draft.model_dump_json()

    steps = compile_plan_steps(draft, method, registry=registry)
    assert draft.model_dump_json() == before and not list(tmp_path.iterdir())
    assert steps[0].parameters == {
        "distance": 500, "segments": 16, "dissolve": True, "end_cap_style": "round", "join_style": "round", "miter_limit": 2,
    }
    assert steps[1].parameters == {"predicate": "intersects"}
    plan = _confirm(draft, steps)
    assert compile_plan_steps(plan, method, registry=registry) == steps

    workspace = create_run_workspace(tmp_path, plan.task_id, "compiled-run")
    sample = workspace.path / "inputs/sample.gpkg"
    shutil.copyfile(SAMPLE, sample)
    original = sample.read_bytes()
    assets = {role: StepFileRef(path="inputs/sample.gpkg", layer=role) for role in ("roads", "targets")}
    earlier = {}
    for step in plan.steps:
        workspace.create_step(step.key)
        for output in step.outputs.values():
            (workspace.path / output.target).parent.mkdir(parents=True, exist_ok=True)
        request = compile_step_request(plan, step.key, run_id="compiled-run", asset_files=assets, prior_results=earlier)
        assert request == StepRequest.model_validate_json(request.model_dump_json())
        assert request.tool == step.tool and request.parameters == step.parameters
        assert request.workdir_ref == "compiled-run"
        assert request.log_path == f"steps/{step.key}/process.log" and request.cancel_path == "cancel.flag"
        if earlier:
            buffer = earlier["create_buffer"].outputs["buffer"]
            assert buffer.layer and request.inputs["area"] == StepFileRef(path=buffer.path, layer=buffer.layer)
        result = run_step(request, registry=registry, workdir=workspace.path, qgis_bindings=bindings, qgis_process=executable)
        assert result.status is ExecutionStatus.SUCCEEDED and result.exit_code == 0, result
        assert result.environment["qgis_version"] and (workspace.path / request.log_path).is_file()
        earlier[step.key] = result

    output = earlier["select_targets"].outputs["targets"]
    actual = pyogrio.read_dataframe(workspace.path / output.path, layer=output.layer)
    roads = pyogrio.read_dataframe(sample, layer="roads")
    points = pyogrio.read_dataframe(sample, layer="targets")
    independent_buffer = roads.geometry.union_all().buffer(500, quad_segs=16)
    expected = sorted(points.loc[points.geometry.intersects(independent_buffer), "source_feature_id"])
    assert expected and sorted(actual["source_feature_id"]) == expected
    assert actual.crs == points.crs and sample.read_bytes() == original


@pytest.mark.parametrize("field,edit,code", [
    ("method", lambda data: data["method"].update(version=2), "METHOD_MISMATCH"),
    ("inputs.roads", lambda data: data["inputs"].pop("roads"), "MISSING_ASSET_ROLE"),
    ("inputs.other", lambda data: data["inputs"].update(other=data["inputs"]["roads"]), "UNKNOWN_ASSET_ROLE"),
    ("steps.0.key", lambda data: data.update(steps=data["steps"][::-1]), "LOCKED_STEP_ORDER"),
    ("steps.0.tool", lambda data: data["steps"][0]["tool"].update(version=2), "TOOL_MISMATCH"),
    ("steps.0.inputs.source", lambda data: data["steps"][0]["inputs"].update(source={"asset": "targets"}), "INPUT_BINDING_MISMATCH"),
    ("steps.0.outputs.buffer.type", lambda data: data["steps"][0]["outputs"]["buffer"].update(type="vector_point"), "OUTPUT_BINDING_MISMATCH"),
    ("steps.0.parameters.distance", lambda data: data["steps"][0]["parameters"].update(distance=501), "PARAMETER_MISMATCH"),
    ("steps.0.parameters.dissolve", lambda data: data["steps"][0]["parameters"].update(dissolve=1), "PARAMETER_MISMATCH"),
    ("steps.0.parameters.distance", lambda data: data["steps"][0]["parameters"].pop("distance"), "MISSING_PARAMETER"),
    ("steps.0.parameters.added", lambda data: data["steps"][0]["parameters"].update(added=1), "UNBOUND_PARAMETER"),
    ("critical_options.distance", lambda data: data["critical_options"].pop("distance"), "OPTION_REQUIRED"),
    ("steps.0.parameters.distance", lambda data: data["critical_options"].update(distance={"value": 3, "unit": "degree"}), "INVALID_PARAMETER"),
    ("steps.1.outputs.targets.target", lambda data: data["steps"][1]["outputs"]["targets"].update(target=data["steps"][0]["outputs"]["buffer"]["target"]), "OUTPUT_TARGET_CONFLICT"),
    ("steps.1.outputs.targets.target", lambda data: data["steps"][1]["outputs"]["targets"].update(target="steps/create_buffer/process.log"), "OUTPUT_TARGET_CONFLICT"),
    ("steps.1.outputs.targets.target", lambda data: data["steps"][1]["outputs"]["targets"].update(target=data["steps"][0]["outputs"]["buffer"]["target"] + "/nested.gpkg"), "OUTPUT_TARGET_CONFLICT"),
])
def test_compile_rejects_invalid_versions_bindings_and_drift(workflow, qgis, field, edit, code):
    method, draft = workflow
    data = draft.model_dump(mode="python")
    edit(data)
    plan = AnalysisPlan.model_validate(data)
    _expect_error(code, field, lambda: compile_plan_steps(plan, method, registry=qgis[0]))


def test_compile_rejects_unavailable_tool_and_preserves_original_issue_field(workflow):
    method, draft = workflow
    registry = ToolRegistry(qgis_process="/definitely-missing-qgis-process")
    contract = ToolContract.model_validate(_load_yaml("config/tools/qgis-buffer.yaml"))
    registry.register(contract)
    _expect_error("TOOL_UNAVAILABLE", "steps.0.tool", lambda: compile_plan_steps(draft, method, registry=registry))

    data = draft.model_dump(mode="python")
    data["issues"] = [{"code": "INPUT_CHANGED", "field": "inputs.roads", "message": "道路资产已有变化"}]
    invalid = AnalysisPlan.model_validate(data)
    _expect_error("PLAN_HAS_ISSUES", "inputs.roads", lambda: compile_plan_steps(invalid, method, registry=registry))


def test_required_tool_parameter_needs_a_template_binding(workflow, qgis):
    method, draft = workflow
    changed = method.model_dump(mode="python")
    del changed["steps"][0]["parameters"]["segments"]
    without_binding = MethodDefinition.model_validate(changed)
    _expect_error("INVALID_PARAMETER", "steps.0.parameters.segments",
                  lambda: compile_plan_steps(draft, without_binding, registry=qgis[0]))


def test_compile_uses_declared_tool_default_and_rejects_an_edited_default(workflow, qgis):
    method, draft = workflow
    definition = method.model_dump(mode="python")
    data = draft.model_dump(mode="python")
    for step in (definition["steps"][0], data["steps"][0]):
        step["tool"]["version"] = 2
        del step["parameters"]["segments"]
    method = MethodDefinition.model_validate(definition)
    draft = AnalysisPlan.model_validate(data)

    compiled = compile_plan_steps(draft, method, registry=qgis[0])
    assert "segments" not in draft.steps[0].parameters
    assert compiled[0].parameters["segments"] == 16
    assert compile_plan_steps(_confirm(draft, compiled), method, registry=qgis[0]) == compiled

    data["steps"][0]["parameters"]["segments"] = 17
    edited = AnalysisPlan.model_validate(data)
    _expect_error("PARAMETER_MISMATCH", "steps.0.parameters.segments",
                  lambda: compile_plan_steps(edited, method, registry=qgis[0]))


def test_compile_revalidates_targets_in_an_existing_snapshot(workflow, qgis):
    method, draft = workflow
    step = draft.steps[0]
    target = step.outputs["buffer"].model_copy(update={"target": "../outside.gpkg"})
    step = step.model_copy(update={"outputs": {"buffer": target}})
    plan = draft.model_copy(update={"steps": (step, draft.steps[1])})

    _expect_error("INVALID_PLAN", "steps.0.outputs.buffer.target",
                  lambda: compile_plan_steps(plan, method, registry=qgis[0]))


@pytest.fixture
def runtime(workflow):
    _, draft = workflow
    plan = _confirm(draft)
    output = plan.steps[0].outputs["buffer"]
    now = datetime(2026, 11, 11, tzinfo=UTC)
    result = StepResult(run_id="run-1", step_key="create_buffer", status="SUCCEEDED", exit_code=0,
                        outputs={"buffer": {"path": output.target, "layer": output.layer, "type": output.type}},
                        log_path="steps/create_buffer/process.log", warnings=[], error=None,
                        started_at=now, finished_at=now, environment={})
    files = {"targets": StepFileRef(path="inputs/sample.gpkg", layer="targets")}
    return plan, result, files


def _fail_step(data):
    data.update(status="FAILED", exit_code=1, outputs={}, error={
        "code": "PROCESS_FAILED", "phase": "execute", "step_key": data["step_key"],
        "field": None, "message": "Processing returned an error", "suggestion": "Inspect the process log",
        "log_path": data["log_path"],
    })


@pytest.mark.parametrize("edit,code,field", [
    (lambda data: data.update(run_id="old-run"), "PRIOR_RESULT_MISMATCH", "prior_results.create_buffer.run_id"),
    (lambda data: data.update(step_key="different-step"), "PRIOR_RESULT_MISMATCH", "prior_results.create_buffer.step_key"),
    (_fail_step, "PRIOR_STEP_FAILED", "prior_results.create_buffer.status"),
    (lambda data: data.update(outputs={}), "PRIOR_OUTPUT_MISSING", "prior_results.create_buffer.outputs.buffer"),
    (lambda data: data["outputs"]["buffer"].update(path="intermediate/wrong.gpkg"), "PRIOR_OUTPUT_MISMATCH", "prior_results.create_buffer.outputs.buffer.path"),
    (lambda data: data["outputs"]["buffer"].update(layer="wrong"), "PRIOR_OUTPUT_MISMATCH", "prior_results.create_buffer.outputs.buffer.layer"),
    (lambda data: data["outputs"]["buffer"].update(type="vector_point"), "PRIOR_OUTPUT_MISMATCH", "prior_results.create_buffer.outputs.buffer.type"),
])
def test_request_rejects_wrong_prior_result_or_reported_output(runtime, edit, code, field):
    plan, result, files = runtime
    data = result.model_dump(mode="python")
    edit(data)
    prior_results = {"create_buffer": StepResult.model_validate(data)}
    _expect_error(code, field, lambda: compile_step_request(
        plan, "select_targets", run_id="run-1", asset_files=files, prior_results=prior_results,
    ))


def test_runtime_requires_confirmation_staged_file_and_prior_result(workflow, runtime):
    _, draft = workflow
    plan, result, files = runtime
    _expect_error("PLAN_NOT_CONFIRMED", "status", lambda: compile_step_request(
        draft, "select_targets", run_id="run-1", asset_files=files, prior_results={},
    ))
    _expect_error("ASSET_FILE_MISSING", "steps.1.inputs.source.asset", lambda: compile_step_request(
        plan, "select_targets", run_id="run-1", asset_files={}, prior_results={"create_buffer": result},
    ))
    _expect_error("PRIOR_RESULT_MISSING", "prior_results.create_buffer", lambda: compile_step_request(
        plan, "select_targets", run_id="run-1", asset_files=files, prior_results={},
    ))
    collision = {"targets": StepFileRef(path=plan.steps[1].outputs["targets"].target, layer="targets")}
    _expect_error("ASSET_FILE_CONFLICT", "asset_files.targets.path", lambda: compile_step_request(
        plan, "select_targets", run_id="run-1", asset_files=collision, prior_results={"create_buffer": result},
    ))


def test_request_rejects_a_later_producer_even_with_matching_result(runtime):
    plan, result, files = runtime
    out_of_order = _confirm(plan, plan.steps[::-1])
    _expect_error("FUTURE_STEP_REFERENCE", "steps.0.inputs.area.step", lambda: compile_step_request(
        out_of_order, "select_targets", run_id="run-1", asset_files=files, prior_results={"create_buffer": result},
    ))
