import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.persistence.schema import initialize_database
from geopilot.scripts.runner import run_script_snapshot
from geopilot.scripts.snapshot import create_script_snapshot, save_script_draft


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/methods/method_cli.py"
TEMPLATE = ROOT / "docs/method-authoring/manual-registration.yaml"
SOURCE = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
SCRIPT = dedent("""
    import argparse
    import json
    from pathlib import Path

    import pyogrio

    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    request = json.loads(parser.parse_args().request.read_text(encoding="utf-8"))
    source = request["asset_files"]["source"]
    frame = pyogrio.read_dataframe(source["path"], layer=source["layer"])
    selected = frame[frame.source_feature_id == request["parameters"]["feature_id"]]
    pyogrio.write_dataframe(selected, "outputs/selected.gpkg", layer="selected", driver="GPKG")
""")


def invoke(cwd, *args):
    return subprocess.run(
        [sys.executable, str(CLI), *map(str, args)],
        cwd=cwd,
        env={**os.environ, "GEOPILOT_TEST_SECRET": "must-not-leak-env-token"},
        capture_output=True,
        text=True,
        check=False,
    )


def envelope(process, code, command):
    assert process.returncode == code, (process.stdout, process.stderr)
    assert process.stderr == ""
    payload = json.loads(process.stdout)
    assert set(payload) == {"schema_version", "command", "status", "data", "errors"}
    assert payload["schema_version"] == 1 and payload["command"] == command
    assert payload["status"] == ("ok" if code == 0 else "error")
    assert all(set(issue) == {"code", "field", "message"} for issue in payload["errors"])
    assert (payload["data"] is None) == bool(code)
    return payload


def file_state(root):
    return {
        path.relative_to(root): (path.stat().st_mtime_ns, path.read_bytes())
        for path in root.rglob("*") if path.is_file()
    }


@pytest.fixture
def executed(tmp_path):
    database = tmp_path / "project.sqlite"
    workspace = tmp_path / "试验 workspace"
    source = tmp_path / "private-original-source-path.gpkg"
    shutil.copyfile(SOURCE, source)
    initialize_database(database)
    asset = register_asset(database, source, layer="targets")
    parameters = {"feature_id": "building-west"}
    draft = save_script_draft(
        workspace, "select-building", script_text=SCRIPT, inputs={"source": asset.ref}, parameters=parameters,
    )
    snapshot = create_script_snapshot(database, workspace, draft.id, draft.version, script_run_id="cli-trial")
    assert run_script_snapshot(workspace, "cli-trial").exit_code == 0
    document = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    document["reference"].update(
        script_run_id="cli-trial", script_sha256=snapshot.script_sha256,
        inputs={"source": asset.ref.model_dump(mode="json")},
    )
    review = tmp_path / "审查 review"
    review.mkdir()
    candidate = review / "candidate.yaml"
    candidate.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    options = (
        "--candidate", candidate.relative_to(tmp_path),
        "--database", database.name, "--workspace", workspace.name,
    )
    return candidate, workspace, document, options


def test_inspect_parses_a_candidate_without_trial_inputs(tmp_path):
    candidate = tmp_path / "candidate.yaml"
    shutil.copyfile(TEMPLATE, candidate)
    before = file_state(tmp_path)

    result = envelope(invoke(tmp_path, "inspect", "--candidate", candidate.name), 0, "inspect")

    assert result["errors"] == []
    data = result["data"]
    assert set(data) == {"scope", "candidate"} and data["scope"] == "candidate_structure"
    assert data["candidate"]["method"]["version"] == 1
    assert data["candidate"]["reference"]["script_run_id"] == "replace-with-trial-run-id"
    assert file_state(tmp_path) == before


def test_validate_and_report_use_real_trial_without_writes_or_hidden_input(tmp_path, executed):
    _, workspace, _, options = executed
    before = file_state(tmp_path)

    validation = envelope(invoke(tmp_path, "validate", *options), 0, "validate")
    report_process = invoke(tmp_path, "report", *options)
    report = envelope(report_process, 0, "report")

    data = report["data"]
    assert set(validation["data"]) == {"scope", "evidence"}
    assert validation["data"]["scope"] == data["scope"] == "manual_registration"
    assert data["disposition"] == "manual_review_required"
    assert data["evidence"] == validation["data"]["evidence"]
    assert data["candidate"]["reference"]["script_run_id"] == data["evidence"]["script_run_id"] == "cli-trial"
    assert data["candidate"]["reference"]["script_sha256"] == data["evidence"]["script_sha256"]
    output = workspace / "scripts/cli-trial/outputs/selected.gpkg"
    assert data["evidence"]["output_fingerprints"] == {"selected": fingerprint_file(output).digest}
    assert "private-original-source-path" not in report_process.stdout
    assert "must-not-leak-env-token" not in report_process.stdout
    assert file_state(tmp_path) == before


def test_report_rejects_a_reference_changed_after_validation(tmp_path, executed):
    candidate, _, document, options = executed
    envelope(invoke(tmp_path, "validate", *options), 0, "validate")
    document["reference"]["outputs"]["selected"]["features"]["building-west"] = "POINT (500751 4401500)"
    candidate.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    before = file_state(tmp_path)

    report = envelope(invoke(tmp_path, "report", *options), 1, "report")

    (finding,) = report["errors"]
    assert finding["code"] == "OUTPUT_REFERENCE_MISMATCH"
    assert finding["field"] == "reference.outputs.selected.features.building-west"
    assert file_state(tmp_path) == before


@pytest.mark.parametrize("content", [b"tool: one\ntool: two\n", b"\xff\xfe", b"tool: [\n"])
def test_invalid_candidate_document_is_structured(tmp_path, content):
    candidate = tmp_path / "candidate.yaml"
    candidate.write_bytes(content)

    result = envelope(invoke(tmp_path, "inspect", "--candidate", candidate.name), 1, "inspect")

    (finding,) = result["errors"]
    assert (finding["code"], finding["field"]) == ("CANDIDATE_PARSE_ERROR", "candidate")


def test_model_errors_keep_candidate_field_paths(tmp_path):
    candidate = tmp_path / "candidate.yaml"
    document = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    document["tool"]["version"] = True
    document["method"]["unexpected"] = "invalid"
    candidate.write_text(yaml.safe_dump(document), encoding="utf-8")

    result = envelope(invoke(tmp_path, "inspect", "--candidate", candidate.name), 1, "inspect")

    assert {(item["code"], item["field"]) for item in result["errors"]} == {
        ("INVALID_CANDIDATE", "tool.version"),
        ("INVALID_CANDIDATE", "method.unexpected"),
    }


def test_candidate_must_serialize_to_utf8_json(tmp_path):
    candidate = tmp_path / "candidate.yaml"
    document = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    document["reference"]["parameters"]["feature_id"] = "\ud800"
    candidate.write_text(yaml.safe_dump(document), encoding="utf-8")

    result = envelope(invoke(tmp_path, "inspect", "--candidate", candidate.name), 1, "inspect")

    (finding,) = result["errors"]
    assert (finding["code"], finding["field"]) == ("INVALID_CANDIDATE", "$")


def test_missing_inputs_and_unavailable_commands_return_usage_or_io_error(tmp_path):
    for args in (
        ("validate", "--candidate", TEMPLATE), ("publish", "--candidate", TEMPLATE),
        ("inspect", "--cand", TEMPLATE),
    ):
        error = envelope(invoke(tmp_path, *args), 2, None)["errors"][0]
        assert (error["code"], error["field"]) == ("CLI_USAGE", "$")
    error = envelope(invoke(tmp_path, "inspect", "--candidate", "absent.yaml"), 2, "inspect")["errors"][0]
    assert (error["code"], error["field"]) == ("INPUT_READ_ERROR", "candidate")
    args = ("validate", "--candidate", TEMPLATE, "--database", "absent.sqlite", "--workspace", "absent")
    error = envelope(invoke(tmp_path, *args), 2, "validate")["errors"][0]
    assert (error["code"], error["field"]) == ("INPUT_READ_ERROR", "database")
    assert not list(tmp_path.iterdir())
    version = invoke(tmp_path, "--version")
    assert (version.returncode, version.stdout.strip()) == (0, "GeoPilot method CLI 1")


@pytest.mark.parametrize("journal", ["wal", "sidecar"])
def test_database_modes_that_could_create_or_modify_sidecars_are_rejected(tmp_path, journal):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    if journal == "wal":
        connection = sqlite3.connect(database)
        try:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        finally:
            connection.close()
        assert list(tmp_path.iterdir()) == [database]
    else:
        database.with_name(database.name + "-journal").write_bytes(b"existing journal")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = file_state(tmp_path)

    result = envelope(invoke(
        tmp_path, "report", "--candidate", TEMPLATE, "--database", database.name, "--workspace", workspace.name,
    ), 2, "report")

    (finding,) = result["errors"]
    assert (finding["code"], finding["field"]) == ("DATABASE_REVIEW_COPY_REQUIRED", "database")
    assert file_state(tmp_path) == before


def test_unknown_registered_rule_cannot_be_authorized_by_the_candidate(tmp_path):
    candidate = tmp_path / "candidate.yaml"
    document = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    document["method"]["check_rules"] = ["unregistered-rule"]
    candidate.write_text(yaml.safe_dump(document), encoding="utf-8")
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = file_state(tmp_path)

    result = envelope(invoke(
        tmp_path, "validate", "--candidate", candidate.name, "--database", database.name,
        "--workspace", workspace.name,
    ), 1, "validate")

    (finding,) = result["errors"]
    assert (finding["code"], finding["field"]) == ("UNKNOWN_CHECK_RULE", "method.check_rules.0")
    assert file_state(tmp_path) == before
