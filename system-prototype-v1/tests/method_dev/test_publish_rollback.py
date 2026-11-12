import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from geopilot.api.server import create_local_app
from geopilot.domain.step_io import ExecutionStatus
from geopilot.execution.worker_lease import exclusive_local_lock
from geopilot.llm.base import ModelErrorCode, ModelProviderError
from geopilot.methods.loader import load_method_catalog, parse_method_yaml
from geopilot.methods import release_ledger
from geopilot.methods.release_ledger import (
    LEDGER_NAME, LOCK_NAME, Approval, CaseVerdict, Disabled, Published, ReleaseError, ReleaseLedger,
    ReleaseReview, ReleaseSummary, ReportReceipt, RolledBack, fingerprint, ledger_state, read_ledger, write_ledger,
)
from geopilot.plans.models import PlanStatus
from geopilot.runs.models import CheckStatus


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/methods/method_cli.py"
CONFIG = ROOT / "config"
FIXTURE = Path(__file__).parent / "fixtures/dem-terrain"
METHOD = "dem-terrain-analysis"
MAINTAINER = "isolated-test-maintainer"


def read_json(path):
    return json.loads(path.read_bytes())


def file_state(directory):
    return {
        path.relative_to(directory).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in directory.rglob("*") if path.is_file()
    }


def invoke(command, catalog, *args, code=0):
    result = subprocess.run(
        [sys.executable, str(CLI), command, "--catalog-dir", str(catalog), *map(str, args)],
        cwd=ROOT, capture_output=True, text=True, timeout=200, check=False,
    )
    assert result.returncode == code, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1 and payload["status"] == ("ok" if code == 0 else "error")
    if code == 0:
        assert payload["command"] == command and payload["errors"] == []
        return payload["data"]
    assert payload["data"] is None and len(payload["errors"]) == 1
    return payload["errors"][0]["code"]


@pytest.fixture
def release_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOPILOT_QGIS_PROCESS", str(tmp_path / "unused-qgis"))
    catalog = tmp_path / "installed-config" / "methods"
    shutil.copytree(CONFIG, catalog.parent)
    package = tmp_path / "test-package"
    shutil.copytree(FIXTURE, package)
    manifest = package / "manifest.json"
    sample = read_json(manifest)
    sample["method"]["version"] = 2
    # The original v1 draft derives this value. The declared v2 sample supplies the exact unit ratio.
    sample["critical_options"]["z_factor"] = 1.0
    sample["failure_cases"][0]["expected"] = {"phase": "confirm", "code": "METHOD_PRECONDITION_FAILED"}
    manifest.write_text(json.dumps(sample), encoding="utf-8")
    candidate = tmp_path / "candidate.yaml"
    definition = yaml.safe_load((catalog / f"{METHOD}.v1.yaml").read_bytes())
    definition["version"] = 2
    candidate.write_text(yaml.safe_dump(definition), encoding="utf-8")
    return catalog, manifest, candidate


def test_release_lifecycle_preserves_confirmed_plan_and_real_outputs(tmp_path, release_files):
    catalog, manifest, candidate = release_files
    source_before = file_state(CONFIG), file_state(FIXTURE)
    target_before = file_state(catalog.parent)
    review_dir = tmp_path / "review"
    inputs = ["--method", candidate, "--package", manifest]

    prepared = invoke("prepare-release", catalog, *inputs, "--work-dir", review_dir)
    assert prepared["scope"] == "local_method_review"
    assert file_state(catalog.parent) == target_before
    review_path = Path(prepared["review_path"])
    review = read_json(review_path)
    assert review == prepared["review"]
    assert prepared["approval_sha256"] == fingerprint(review)
    assert review["summary"]["candidate_sha256"] == hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert review["summary"]["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert review["summary"]["cases"][0]["verdict"] == "positive_match"
    assert review["summary"]["cases"][1]["code"] == "METHOD_PRECONDITION_FAILED"
    assert review["summary"]["cases"][1]["phase"] == "confirm"
    for receipt in review["reports"]:
        assert hashlib.sha256((review_dir / receipt["path"]).read_bytes()).hexdigest() == receipt["sha256"]

    published_args = [
        *inputs, "--review", review_path, "--approve", prepared["approval_sha256"],
        "--approver", MAINTAINER, "--reason", "Independent DEM and missing unit cases checked",
    ]
    assert invoke("publish", catalog, *inputs, "--review", review_path, "--work-dir", tmp_path / "missing",
                  code=2) == "CLI_USAGE"
    assert invoke("publish", catalog, *published_args, "--approve", "0" * 64,
                  "--work-dir", tmp_path / "wrong-approval", code=1) == "APPROVAL_MISMATCH"
    assert not (tmp_path / "wrong-approval").exists() and not (catalog / LEDGER_NAME).exists()

    raw_path = review_dir / review["reports"][0]["path"]
    for label, path, payload, expected in (
        ("candidate", candidate, candidate.read_bytes() + b"# changed candidate\n", "RELEASE_INPUT_CHANGED"),
        ("manifest", manifest, manifest.read_bytes() + b"\n", "RELEASE_INPUT_CHANGED"),
        ("config", catalog / f"{METHOD}.v1.yaml", (catalog / f"{METHOD}.v1.yaml").read_bytes() + b"# changed config\n",
         "RELEASE_INPUT_CHANGED"),
        ("report", raw_path, raw_path.read_bytes() + b"\n", "INVALID_RELEASE_EVIDENCE"),
    ):
        original = path.read_bytes()
        path.write_bytes(payload)
        try:
            assert invoke("publish", catalog, *published_args, "--work-dir", tmp_path / label, code=1) == expected
            assert not (tmp_path / label).exists() and not (catalog / LEDGER_NAME).exists()
        finally:
            path.write_bytes(original)

    replay = tmp_path / "replay"
    published = invoke("publish", catalog, *published_args, "--work-dir", replay)
    ledger = read_ledger(catalog)
    event = ledger.events[0]
    assert published["event"]["approval"]["approver"] == MAINTAINER
    assert event.candidate_yaml.encode("utf-8") == candidate.read_bytes()
    assert event.review.summary == event.replay.summary
    assert event.review.reports[0].sha256 != event.replay.reports[0].sha256
    assert invoke("publish", catalog, *published_args, "--work-dir", tmp_path / "duplicate",
                  code=1) == "METHOD_VERSION_EXISTS"
    assert not (tmp_path / "duplicate").exists() and len(read_ledger(catalog).events) == 1

    installed = load_method_catalog(catalog)
    assert not installed.issues and installed.get_enabled(METHOD).version == 2
    positive = replay / "cases" / review["summary"]["cases"][0]["case_id"]
    report = read_json(positive / "report.json")
    assert report["reference"]["status"] == "PASS"
    database, workspace = positive / "project.sqlite", positive / "workspace"
    historical = report["actual"]["plan"]
    prior_run = report["actual"]["run"]["run_id"]
    request = {
        "method": {"id": METHOD, "version": 2},
        "inputs": {role: item["asset"] for role, item in report["inputs"].items()},
        "critical_options": report["critical_options"],
    }

    with TestClient(create_local_app(database, workspace, config=catalog.parent)) as client:
        task = client.post("/api/tasks", json={"goal": {"summary": "Check a pending released draft"}}).json()["task_id"]
        response = client.post(f"/api/tasks/{task}/plans", json=request)
        assert response.status_code == 200 and not response.json()["issues"]
        draft = response.json()
        assert draft["status"] == PlanStatus.DRAFT.value

    builtin = installed.get(METHOD, 1)
    rollback_args = [
        "--from", f"{METHOD}@2", "--to", f"{METHOD}@1", "--approve", fingerprint(builtin.model_dump(mode="json")),
        "--approver", MAINTAINER, "--reason", "Restore the original method selection",
    ]
    assert invoke("rollback", catalog, *rollback_args, "--approve", "0" * 64, code=1) == "APPROVAL_MISMATCH"
    rollback = invoke("rollback", catalog, *rollback_args)
    assert rollback["event"]["target"] == {"id": METHOD, "version": 1}
    current = load_method_catalog(catalog)
    assert not current.issues and current.get_enabled(METHOD).version == 1 and current.get(METHOD, 2).version == 2
    with pytest.raises(KeyError):
        current.get_enabled(METHOD, 2)

    advertised = []

    class ProviderProbe:
        def generate(self, request, *, timeout):
            advertised.extend(item["version"] for item in request.payload["candidate_methods"] if item["id"] == METHOD)
            raise ModelProviderError(ModelErrorCode.INVALID_RESPONSE)

    # A restarted local service observes the new eligibility snapshot.
    with TestClient(create_local_app(database, workspace, config=catalog.parent, model_provider=ProviderProbe())) as client:
        methods = client.get("/api/methods").json()["methods"]
        assert [item["definition"]["version"] for item in methods if item["definition"]["id"] == METHOD] == [1]
        assert client.post(f"/api/tasks/{task}/plans", json=request).status_code == 404
        blocked = client.post(f"/api/plans/{draft['id']}/confirm", json={"plan_version": draft["version"]})
        assert blocked.status_code == 422 and blocked.json()["detail"]["issues"][0]["code"] == "METHOD_UNAVAILABLE"
        preview = client.post("/api/model-assistance", json={
            "method": request["method"], "goal": {"summary": "Check disabled method"},
        })
        assert preview.status_code == 404 and preview.json()["detail"]["code"] == "METHOD_NOT_FOUND" and advertised == []
        suggestion = client.post("/api/model-assistance", json={"goal": {"summary": "List available methods"}})
        assert suggestion.status_code == 502 and advertised == [1]
        exact = client.get(f"/api/plans/{historical['id']}", params={"version": historical["version"]}).json()
        assert exact["method"] == request["method"] and exact["status"] == PlanStatus.CONFIRMED.value
        endpoint = f"/api/plans/{historical['id']}"
        assert client.post(endpoint + "/confirm", json={"plan_version": historical["version"]}).status_code == 200
        prior_artifacts = client.get(f"/api/runs/{prior_run}/artifacts").json()["artifacts"]
        assert {item["role"] for item in prior_artifacts} == set(report["reference"]["outputs"])
        queued = client.post(endpoint + "/runs", json={"plan_version": historical["version"]})
        assert queued.status_code == 202, queued.text
        run_id = queued.json()["run_id"]
        deadline = time.monotonic() + 65
        while True:
            state = client.get(f"/api/runs/{run_id}").json()
            executing = state["execution_status"] in {ExecutionStatus.NOT_STARTED.value, ExecutionStatus.RUNNING.value}
            checking = state["execution_status"] == ExecutionStatus.SUCCEEDED.value and state["check_status"] in {
                CheckStatus.NOT_CHECKED.value, CheckStatus.CHECKING.value,
            }
            if not (executing or checking):
                break
            assert time.monotonic() < deadline, state
            time.sleep(0.1)
        assert (state["execution_status"], state["check_status"]) == (
            ExecutionStatus.SUCCEEDED.value, CheckStatus.PASSED.value,
        )

    assert [event.action for event in read_ledger(catalog).events] == ["publish", "rollback"]
    assert (file_state(CONFIG), file_state(FIXTURE)) == source_before
    (tmp_path / "lifecycle-evidence.json").write_text(json.dumps({
        "pending_draft_status": blocked.status_code,
        "pending_draft_issue": blocked.json()["detail"]["issues"][0],
        "model_preview_status": preview.status_code,
        "model_preview_issue": preview.json()["detail"]["code"],
        "model_advertised_versions": advertised,
        "historical_plan": {"method": exact["method"], "status": exact["status"], "id": historical["id"]},
        "historical_artifacts": prior_artifacts,
        "historical_replay": state,
        "source_config_and_fixtures_unchanged": True,
    }, indent=2) + "\n", encoding="utf-8")

    # Exercise the explicit no-predecessor operation against a separate isolated copy.
    disable_catalog = tmp_path / "disable-config" / "methods"
    shutil.copytree(catalog.parent, disable_catalog.parent)
    (disable_catalog / LEDGER_NAME).write_text(
        ledger.model_dump_json(indent=2) + "\n", encoding="utf-8",
    )
    before = (disable_catalog / LEDGER_NAME).read_bytes()
    disable_args = ["--method", f"{METHOD}@2", "--approver", MAINTAINER, "--reason", "Withdraw this version"]
    with exclusive_local_lock(disable_catalog / LOCK_NAME):
        assert invoke("disable", disable_catalog, *disable_args, code=1) == "CATALOG_BUSY"
        assert (disable_catalog / LEDGER_NAME).read_bytes() == before
    assert invoke("disable", disable_catalog, *disable_args)["event"]["approval"]["sha256"] == event.approval.sha256
    assert [event.action for event in read_ledger(disable_catalog).events] == ["publish", "disable"]
    withdrawn = load_method_catalog(disable_catalog)
    assert withdrawn.get(METHOD, 2).version == 2 and withdrawn.get_enabled(METHOD).version == 1


def test_forged_review_and_corrupt_ledger_are_rejected_before_trials(tmp_path, release_files):
    catalog, manifest, candidate = release_files
    review = tmp_path / "invented.json"
    review.write_text('{"status":"PASS","schema_version":1}', encoding="utf-8")
    work = tmp_path / "must-not-exist"
    args = ["--method", candidate, "--package", manifest, "--work-dir", work]
    error = invoke(
        "publish", catalog, *args, "--review", review, "--approve", "0" * 64,
        "--approver", MAINTAINER, "--reason", "Synthetic invalid proof", code=1,
    )
    assert error == "INVALID_RELEASE_EVIDENCE" and not work.exists() and not (catalog / LEDGER_NAME).exists()

    ledger_path = catalog / LEDGER_NAME
    ledger_path.write_text('{"schema_version":1,"events":[],"events":[]}', encoding="utf-8")
    before = ledger_path.read_bytes()
    assert invoke("prepare-release", catalog, *args, code=1) == "INVALID_RELEASE_LEDGER"
    loaded = load_method_catalog(catalog)
    assert len(loaded.issues) == 1 and loaded.issues[0].path == ledger_path
    assert ledger_path.read_bytes() == before and not work.exists()


def test_ledger_restores_an_earlier_approved_release_and_preserves_failed_atomic_write(
    release_files, monkeypatch,
):
    catalog, _, candidate = release_files
    when = datetime.now(UTC)
    builtin = load_method_catalog(catalog, include_releases=False)
    methods = {method.ref: method for method in builtin.list()}

    def event(version, method_id=METHOD):
        document = yaml.safe_load(candidate.read_bytes())
        document["id"] = method_id
        document["version"] = version
        content = yaml.safe_dump(document)
        ref = parse_method_yaml(content).ref
        # Synthetic receipts exercise the ledger transition. The CLI lifecycle uses actual trial receipts.
        summary = ReleaseSummary(
            method=ref, candidate_sha256=hashlib.sha256(content.encode()).hexdigest(), manifest_sha256="1" * 64,
            config={}, source_sha256="2" * 64, qgis_entrypoint_sha256=None, environment={},
            cases=[CaseVerdict(case_id="test-case", verdict="positive_match", phase="reference",
                               outputs={"statistics": {"kind": "json", "checks": {}}})],
        )
        review = ReleaseReview(
            summary=summary, reports=[ReportReceipt(case_id="test-case", path="cases/test-case/report.json",
                                                     sha256="3" * 64)],
        )
        return Published(
            method=ref, created_at=when, approval=Approval(sha256=review.digest, approver=MAINTAINER, reason="test"),
            candidate_yaml=content, review=review, review_path="review.json", replay=review, replay_path="replay.json",
        )

    older, newer = event(2), event(3)
    disable = Disabled(method=older.method, created_at=when, approval=older.approval)
    prior = ReleaseLedger(events=[older, disable, newer])
    restore = RolledBack(method=newer.method, target=older.method, created_at=when, approval=older.approval)
    updated = ReleaseLedger(events=[*prior.events, restore])
    assert ledger_state(prior, methods).disabled == {older.method}
    assert ledger_state(updated, methods).disabled == {newer.method}
    wrong = restore.model_copy(update={"approval": newer.approval})
    with pytest.raises(ReleaseError, match="exact target approval"):
        ledger_state(ReleaseLedger(events=[*prior.events, wrong]), methods)
    first = event(1, "release-test-first-method")
    withdraw = Disabled(method=first.method, created_at=when, approval=first.approval)
    withdrawn = ledger_state(ReleaseLedger(events=[first, withdraw]), methods)
    assert first.method in withdrawn.published and first.method in withdrawn.disabled

    with exclusive_local_lock(catalog / LOCK_NAME):
        write_ledger(catalog, prior)
        before = (catalog / LEDGER_NAME).read_bytes()

        def fail_replace(temporary, destination):
            assert temporary.parent == destination.parent == catalog
            assert temporary.is_file()
            raise OSError("simulated replace failure")

        with monkeypatch.context() as replacement:
            replacement.setattr(release_ledger.os, "replace", fail_replace)
            with pytest.raises(OSError, match="simulated replace failure"):
                write_ledger(catalog, updated)
        assert (catalog / LEDGER_NAME).read_bytes() == before
        assert list(catalog.glob(".release-ledger-*")) == []
        write_ledger(catalog, updated)

    restored = load_method_catalog(catalog)
    assert not restored.issues and restored.get_enabled(METHOD).version == 2
    assert restored.get(METHOD, 3).version == 3
    assert [item.action for item in read_ledger(catalog).events] == ["publish", "disable", "publish", "rollback"]
