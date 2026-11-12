import json
from pathlib import Path
from shutil import copyfile
from textwrap import dedent

import pyogrio
import pytest
import yaml

from geopilot.application.plan_runner import run_confirmed_plan
from geopilot.application.task_context import build_task_context
from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.task import TaskGoal
from geopilot.methods.loader import MethodCatalog
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.plans.draft_builder import build_draft
from geopilot.plans.service import confirm_plan, save_draft
from geopilot.scripts.registration import RegistrationCandidate, RegistrationError, validate_manual_registration
from geopilot.scripts.runner import run_script_snapshot
from geopilot.scripts.snapshot import create_script_snapshot, save_script_draft
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
TEMPLATE = ROOT / "docs/method-authoring/manual-registration.yaml"
RULES = ("output_exists",)
RUN_ID = "sample-filter"
SCRIPT = dedent('''
    import argparse
    import json
    from pathlib import Path

    import pyogrio

    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--result')
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding='utf-8'))
    if args.result:
        source = request['inputs']['source']
        output = request['output_targets']['selected']
    else:
        source = request['asset_files']['source']
        output = {'path': 'outputs/selected.gpkg', 'layer': 'selected'}
    points = pyogrio.read_dataframe(source['path'], layer=source['layer'])
    result = points[points.source_feature_id == request['parameters']['feature_id']]
    pyogrio.write_dataframe(result, output['path'], layer=output['layer'], driver='GPKG')
    if args.result:
        Path(args.result).write_text(json.dumps({'outputs': {'selected': output}}), encoding='utf-8')
    print(f'已筛选 {len(result)} 条建筑物点')
''')


@pytest.fixture
def project(tmp_path):
    database = tmp_path / "project.sqlite"
    workspace = tmp_path / "workspace"
    source = tmp_path / "buildings.gpkg"
    copyfile(SOURCE, source)
    initialize_database(database)
    asset = register_asset(database, source, layer="targets")
    return database, workspace, asset


def _prepare(project, script=SCRIPT):
    database, workspace, asset = project
    draft = save_script_draft(
        workspace, "select-building", script_text=script, inputs={"source": asset.ref},
        parameters={"feature_id": "building-west"},
    )
    snapshot = create_script_snapshot(database, workspace, draft.id, draft.version, script_run_id=RUN_ID)
    authored = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    authored["reference"]["script_run_id"] = RUN_ID
    authored["reference"]["script_sha256"] = snapshot.script_sha256
    authored["reference"]["inputs"]["source"] = asset.ref.model_dump(mode="json")
    candidate = RegistrationCandidate.model_validate(authored)
    return workspace / "scripts" / RUN_ID, snapshot, candidate


@pytest.fixture
def executed(project):
    workdir, snapshot, candidate = _prepare(project)
    result = run_script_snapshot(project[1], RUN_ID)
    assert result.status == "SUCCEEDED" and result.exit_code == 0
    return workdir, snapshot, candidate


def _validate(project, candidate):
    return validate_manual_registration(project[0], project[1], candidate, registered_rules=RULES)


def _reject(project, candidate, code, field):
    with pytest.raises(RegistrationError) as rejected:
        _validate(project, candidate)
    assert (rejected.value.code, rejected.value.field) == (code, field)


def test_manual_candidate_checks_real_snapshot_and_reference_without_publishing(project, executed, tmp_path):
    workdir, snapshot, candidate = executed
    project[2].source.path.unlink()
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in workdir.rglob("*") if path.is_file()}

    evidence = _validate(project, candidate)

    assert (evidence.script_run_id, evidence.script_id, evidence.script_version, evidence.script_sha256) == (
        RUN_ID, snapshot.script_id, snapshot.version, snapshot.script_sha256,
    )
    assert evidence.tool == candidate.tool.ref and evidence.method == candidate.method.ref
    assert evidence.output_fingerprints == {"selected": fingerprint_file(workdir / "outputs/selected.gpkg").digest}
    assert set(before) == {path for path in workdir.rglob("*") if path.is_file()}
    assert all(path.read_bytes() == content and path.stat().st_mtime_ns == stamp for path, (content, stamp) in before.items())
    assert "已筛选 1 条建筑物点" in (workdir / "process.log").read_text(encoding="utf-8")
    with open_database(project[0], read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM run").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM artifact").fetchone()[0] == 0

    formal_database = tmp_path / "formal.sqlite"
    initialize_database(formal_database)
    formal_asset = register_asset(formal_database, SOURCE, layer="targets")
    isolated = ToolRegistry(python_implementations=[candidate.tool.implementation])
    isolated.register(candidate.tool)
    context = build_task_context(
        formal_database, task_id="manual-review", goal=TaskGoal(summary="人工收录样例"),
        method=candidate.method, selected={"points": formal_asset.ref}, registry=isolated,
    )
    plan = build_draft(context, candidate.method, plan_id="manual-review", options=snapshot.parameters)
    assert not plan.issues
    save_draft(formal_database, plan, context=context)
    confirmed = confirm_plan(
        formal_database, plan.id, plan.version, catalog=MethodCatalog([candidate.method], ()),
        registry=isolated, registered_rules=RULES,
    )
    result = run_confirmed_plan(
        formal_database, confirmed.id, confirmed.version, workspace_root=tmp_path / "formal-workspace",
        run_id="formal-sample", registry=isolated, input_preflight=preflight_step_inputs,
        python_implementations={candidate.tool.implementation: workdir / snapshot.script_path},
    )
    assert (result.execution_status, result.check_status) == ("SUCCEEDED", "PASSED"), result
    step = result.steps["filter"]
    assert (step.status, step.exit_code) == ("SUCCEEDED", 0)
    reference = result.artifacts["filtered_points"]
    output = pyogrio.read_dataframe(result.workdir / reference.path, layer=reference.layer)
    assert output.crs.to_epsg() == 32650 and output.source_feature_id.tolist() == ["building-west"]
    assert (output.geometry.item().x, output.geometry.item().y) == (500750, 4401500)


@pytest.mark.parametrize("attempted", [False, True])
def test_readable_output_without_successful_run_is_not_registration_evidence(project, attempted):
    workdir, _, candidate = _prepare(project, SCRIPT + "\nraise SystemExit(7)\n")
    if attempted:
        result = run_script_snapshot(project[1], RUN_ID)
        assert (result.status, result.exit_code) == ("FAILED", 7)
        assert pyogrio.read_info(workdir / "outputs/selected.gpkg", layer="selected")["features"] == 1
    _reject(project, candidate, "SCRIPT_RUN_INVALID", "reference.script_run_id")


@pytest.mark.parametrize(("change", "code", "field"), [
    ("missing_precondition", "CONTRACT_INCOMPLETE", "tool.preconditions"),
    ("missing_reference", "REFERENCE_MISMATCH", "reference.outputs"),
    ("unknown_rule", "UNKNOWN_CHECK_RULE", "method.check_rules.0"),
    ("self_reference", "SELF_STEP_REFERENCE", "method.steps.0.inputs.source.step"),
    ("wrong_script", "REFERENCE_MISMATCH", "reference.script_sha256"),
    ("wrong_parameter", "REFERENCE_MISMATCH", "reference.parameters"),
    ("wrong_geometry", "OUTPUT_REFERENCE_MISMATCH", "reference.outputs.selected.features.building-west"),
])
def test_candidate_must_match_contract_and_independent_reference(project, executed, change, code, field):
    _, _, original = executed
    payload = json.loads(original.model_dump_json())
    if change == "missing_precondition":
        payload["tool"]["preconditions"] = []
    elif change == "missing_reference":
        payload["reference"]["outputs"] = {}
    elif change == "unknown_rule":
        payload["method"]["check_rules"] = ["unregistered-rule"]
    elif change == "self_reference":
        payload["method"]["steps"][0]["inputs"]["source"] = {"step": "filter", "output": "selected"}
    elif change == "wrong_script":
        payload["reference"]["script_sha256"] = "0" * 64
    elif change == "wrong_parameter":
        payload["reference"]["parameters"]["feature_id"] = "building-east"
    else:
        payload["reference"]["outputs"]["selected"]["features"]["building-west"] = "POINT (500751 4401500)"
    _reject(project, RegistrationCandidate.model_validate(payload), code, field)


def test_staged_asset_fingerprint_must_match_historical_registration(project, executed):
    workdir, snapshot, candidate = executed
    staged = workdir / snapshot.asset_files["source"].path
    with staged.open("r+b") as stream:
        last = stream.seek(-1, 2)
        original = stream.read(1)
        stream.seek(last)
        stream.write(bytes([original[0] ^ 1]))
    _reject(project, candidate, "INPUT_CHANGED", "reference.inputs.source")


def test_deterministic_table_matches_a_separately_authored_file_reference(project, tmp_path):
    script = SCRIPT + dedent('''
        rows = result.source_feature_id.tolist()
        Path('outputs/statistics.csv').write_text(
            'feature_id,count\\n' + ''.join(f'{feature},1\\n' for feature in rows), encoding='utf-8',
        )
    ''')
    workdir, _, original = _prepare(project, script)
    assert run_script_snapshot(project[1], RUN_ID).exit_code == 0
    reference = tmp_path / "expected.csv"
    reference.write_text("feature_id,count\nbuilding-west,1\n", encoding="utf-8")
    payload = json.loads(original.model_dump_json())
    payload["tool"]["outputs"]["statistics"] = "table_csv"
    payload["method"]["steps"][0]["outputs"]["statistics"] = "table_csv"
    payload["method"]["expected_artifacts"].append({
        "role": "statistics", "step": "filter", "output": "statistics", "type": "table_csv",
    })
    payload["reference"]["outputs"]["statistics"] = {
        "path": "outputs/statistics.csv", "fingerprint": fingerprint_file(reference).digest,
    }
    evidence = _validate(project, RegistrationCandidate.model_validate(payload))
    assert evidence.output_fingerprints["statistics"] == fingerprint_file(workdir / "outputs/statistics.csv").digest
    payload["reference"]["outputs"]["statistics"]["fingerprint"] = "0" * 64
    _reject(project, RegistrationCandidate.model_validate(payload), "OUTPUT_REFERENCE_MISMATCH", "reference.outputs.statistics.fingerprint")
