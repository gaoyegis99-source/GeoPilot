"""Prepare, approve, and record local method releases."""

import hashlib
import math
import platform
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import yaml

from geopilot.api.server import _yaml_files
from geopilot.execution.worker_lease import exclusive_local_lock
from geopilot.methods.loader import load_method_catalog, released_methods
from geopilot.methods.models import MethodRef
from geopilot.methods.release_ledger import (
    LEDGER_NAME,
    LOCK_NAME,
    Approval,
    CaseVerdict,
    Disabled,
    Published,
    ReleaseError,
    ReleaseLedger,
    ReleaseReview,
    ReleaseSummary,
    ReportReceipt,
    RolledBack,
    fingerprint,
    ledger_state,
    read_ledger,
    read_local_json,
    target_digest,
    write_ledger,
)

from scripts.methods.reference_package import (
    ReferencePackage,
    ReferencePackageError,
    _assert_package_intact,
    _read_json,
    _safe_file,
    load_reference_package,
)
from scripts.methods.template import TemplateError, parse_reference
from scripts.methods.trial import ROOT, TrialError, _digest, _reserve_directory, _selected_method, resolve_qgis, run_trial


REVIEW_NAME = "review.json"
RELEASE_COMMANDS = ("prepare-release", "publish", "disable", "rollback")
_REGISTRY_FAMILIES = ("tools", "runner_bindings", "sensors")
_RUNTIME_PACKAGES = (
    "fastapi", "starlette", "httpx2", "pydantic", "pyyaml", "numpy", "pandas", "geopandas",
    "pyogrio", "pyproj", "rasterio", "shapely",
)


@dataclass(frozen=True, slots=True)
class _Inputs:
    catalog: Path
    method: MethodRef
    candidate: Path
    candidate_bytes: bytes
    manifest: Path
    package: ReferencePackage
    qgis: Path | None
    timeout: float

    @property
    def candidate_sha256(self) -> str:
        return hashlib.sha256(self.candidate_bytes).hexdigest()

    @property
    def manifest_sha256(self) -> str:
        return self.package._files[self.manifest]


@dataclass(frozen=True, slots=True)
class _Installation:
    config: dict[str, str]
    source_sha256: str
    qgis_entrypoint_sha256: str | None
    environment: dict


def _directory(path: Path) -> Path:
    try:
        directory = path.resolve(strict=True)
        if not directory.is_dir() or directory.name != "methods":
            raise NotADirectoryError("Expected an existing methods/ directory within the target configuration")
    except OSError as error:
        raise ReleaseError("INPUT_READ_ERROR", "catalog_dir", str(error), exit_code=2) from error
    return directory


def _approval(sha256: str, approver: str, reason: str) -> Approval:
    try:
        return Approval(sha256=sha256, approver=approver.strip(), reason=reason.strip())
    except ValueError as error:
        raise ReleaseError("INVALID_APPROVAL", "approve", "Provide a SHA-256, approver and reason", exit_code=2) from error


def _reference(value: str, field: str) -> MethodRef:
    try:
        return parse_reference(value, field, MethodRef)
    except TemplateError as error:
        raise ReleaseError(error.code, error.field, str(error), exit_code=2) from error


def _catalog_data(directory: Path):
    builtins = load_method_catalog(directory, include_releases=False)
    if builtins.issues:
        issue = builtins.issues[0]
        raise ReleaseError("INVALID_METHOD_CONFIG", "catalog_dir", f"{issue.path.name}: {issue.message}")
    methods = {method.ref: method for method in builtins.list()}
    ledger = read_ledger(directory)
    state = ledger_state(ledger, methods)
    try:
        released_methods(state)
    except (ValueError, RecursionError, yaml.YAMLError) as error:
        raise ReleaseError("INVALID_RELEASE_LEDGER", "catalog_dir", str(error)) from error
    return ledger, state, methods


def _require_new(ref, state, builtins):
    if ref in state.published or ref in builtins:
        raise ReleaseError("METHOD_VERSION_EXISTS", "method", "The exact method version already exists")


def _inputs(catalog_dir, candidate, manifest, qgis_process, timeout):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ReleaseError("INVALID_TIMEOUT", "timeout", "Timeout must be positive and finite", exit_code=2)
    catalog = _directory(catalog_dir)
    try:
        package = load_reference_package(manifest)
        definition, payload, candidate_path = _selected_method(package, candidate, {}, catalog.parent)
    except ReferencePackageError as error:
        raise ReleaseError(error.code, error.field, str(error)) from error
    except TrialError as error:
        raise ReleaseError(error.code, error.field, str(error), exit_code=error.exit_code) from error
    _, state, builtins = _catalog_data(catalog)
    _require_new(definition.ref, state, builtins)
    return _Inputs(catalog, definition.ref, candidate_path, payload, package.root / manifest.name, package,
                   resolve_qgis(qgis_process), timeout)


def _installation(inputs: _Inputs) -> _Installation:
    config = inputs.catalog.parent
    try:
        configuration = {}
        for family in (*_REGISTRY_FAMILIES, "methods"):
            for path in _yaml_files(config / family):
                if path.is_symlink():
                    raise OSError(f"Configuration symlink: {path.name}")
                configuration[path.relative_to(config).as_posix()] = _digest(path)
        sources = [
            *sorted((ROOT / "src/geopilot").rglob("*.py")),
            *sorted((ROOT / "scripts/methods").glob("*.py")),
            *(ROOT / path for path in ("scripts/quality/validate_config_schemas.py", "requirements.txt",
                                       "requirements-test.txt", "pyproject.toml")),
        ]
        source_hashes = {path.relative_to(ROOT).as_posix(): _digest(path) for path in sources}
        # The shipped wrapper binds these files into a content-addressed QGIS image.
        bundle = ROOT / ".environments/qgis-deb"
        if inputs.qgis is not None and inputs.qgis.resolve() == (bundle / "qgis_process").resolve():
            for name in ("qgis_process", "qgis_process.bin"):
                path = bundle / "usr/bin" / name
                source_hashes[path.relative_to(ROOT).as_posix()] = _digest(path)
        qgis_hash = _digest(inputs.qgis) if inputs.qgis is not None else None
        environment = {
            "platform": platform.platform(), "python_executable_sha256": _digest(Path(sys.executable)),
            "packages": {package: version(package) for package in _RUNTIME_PACKAGES},
        }
    except (OSError, PackageNotFoundError) as error:
        raise ReleaseError("INSTALLATION_READ_FAILED", "catalog_dir", str(error)) from error
    return _Installation(configuration, fingerprint(source_hashes), qgis_hash, environment)


def _unchanged(inputs: _Inputs, installation: _Installation):
    try:
        _assert_package_intact(inputs.package)
        candidate_unchanged = _digest(inputs.candidate) == inputs.candidate_sha256
    except (OSError, ReferencePackageError) as error:
        raise ReleaseError("RELEASE_INPUT_CHANGED", "review", str(error)) from error
    if not candidate_unchanged or _installation(inputs) != installation:
        raise ReleaseError("RELEASE_INPUT_CHANGED", "review", "Candidate, configuration, or execution source changed")


def _cases(package: ReferencePackage):
    return [(package.case_id, None), *((case.id, case.expected) for case in package.failure_cases)]


def _case_verdict(report, case_id, expected, package):
    actual = report["actual"]
    if report["schema_version"] != 1 or report["case_id"] != case_id or report["status"] != "PASS":
        raise ValueError("Trial report version, case or status is invalid")
    if report["source_integrity"]["status"] != "PASS":
        raise ValueError("Trial input integrity check did not pass")
    if expected is not None:
        if (
            report["verdict"] != "expected_failure_matched" or report["expected_failure"] != expected.model_dump()
            or actual["phase"] != expected.phase or actual["run_count"] != 0 or actual["run"] is not None
            or report["reference"] is not None
            or not any(issue["code"] == expected.code for issue in actual["issues"])
        ):
            raise ValueError("Trial did not match the declared failure case")
        return CaseVerdict(case_id=case_id, verdict=report["verdict"], phase=expected.phase, code=expected.code)
    run, reference = actual["run"], report["reference"]
    if (
        report["verdict"] != "positive_match" or report["expected_failure"] is not None
        or actual["phase"] != "reference" or actual["run_count"] != 1 or actual["workers_stopped"] is not True
        or run["execution_status"] != "SUCCEEDED" or run["check_status"] != "PASSED"
        or reference["status"] != "PASS" or reference["outputs"].keys() != package.outputs.keys()
        or any(item["status"] != "PASS" for item in reference["outputs"].values())
    ):
        raise ValueError("Trial execution, checks or independent reference did not pass")
    outputs = {role: {"kind": result["kind"], "checks": result["checks"]} for role, result in reference["outputs"].items()}
    return CaseVerdict(case_id=case_id, verdict=report["verdict"], phase="reference", outputs=outputs)


def _summarize(inputs: _Inputs, installation: _Installation, root: Path) -> ReleaseReview:
    receipts = []
    verdicts = []
    environment = None
    trial_config = {
        path: digest for path, digest in installation.config.items() if path.split("/", 1)[0] in _REGISTRY_FAMILIES
    }
    try:
        for case_id, expected in _cases(inputs.package):
            relative = f"cases/{case_id}/report.json"
            path = _safe_file(root, relative, {".json"}, "review")
            digest = _digest(path)
            report = _read_json(path)
            if _digest(path) != digest:
                raise ValueError("Trial report changed during review")
            if report["method"] != inputs.method.model_dump() or report["fingerprints"] != {
                "manifest": inputs.manifest_sha256, "method": inputs.candidate_sha256, "config": trial_config,
            }:
                raise ValueError("Trial method, package or target registry fingerprints differ")
            if environment is None:
                environment = report["environment"]
            elif report["environment"] != environment:
                raise ValueError("Cases ran with different observed environments")
            verdicts.append(_case_verdict(report, case_id, expected, inputs.package))
            receipts.append(ReportReceipt(case_id=case_id, path=relative, sha256=digest))
        if "qgis" in environment["observed"] and installation.qgis_entrypoint_sha256 is None:
            raise ValueError("The QGIS executable was not recorded before the trial")
        summary = ReleaseSummary(
            method=inputs.method, candidate_sha256=inputs.candidate_sha256, manifest_sha256=inputs.manifest_sha256,
            config=installation.config, source_sha256=installation.source_sha256,
            qgis_entrypoint_sha256=installation.qgis_entrypoint_sha256,
            environment={"installation": installation.environment, "trial": environment}, cases=verdicts,
        )
    except (OSError, ValueError, AttributeError, KeyError, TypeError, RecursionError) as error:
        raise ReleaseError("INVALID_RELEASE_EVIDENCE", "review", str(error)) from error
    return ReleaseReview(summary=summary, reports=receipts)


def _run_cases(inputs: _Inputs, installation: _Installation, work_dir: Path) -> tuple[Path, ReleaseReview]:
    try:
        root = _reserve_directory(work_dir, inputs.package, inputs.catalog.parent)
    except TrialError as error:
        raise ReleaseError(error.code, error.field, str(error), exit_code=error.exit_code) from error
    candidate = root / "candidate.yaml"
    with candidate.open("xb") as stream:
        stream.write(inputs.candidate_bytes)
    candidate.chmod(0o400)
    cases = root / "cases"
    cases.mkdir()
    failures = []
    for case_id, _ in _cases(inputs.package):
        try:
            run_trial(inputs.manifest, cases / case_id, case=case_id, candidate=candidate, qgis_process=inputs.qgis,
                      timeout=inputs.timeout, config_root=inputs.catalog.parent)
        except TrialError as error:
            failures.append(f"{case_id} ({error.code}): {error}")
    if failures:
        raise ReleaseError("RELEASE_TRIAL_FAILED", "package", f"Trial evidence: {cases}. " + " | ".join(failures))
    _unchanged(inputs, installation)
    review = _summarize(inputs, installation, root)
    path = root / REVIEW_NAME
    with path.open("x", encoding="utf-8") as stream:
        stream.write(review.model_dump_json(indent=2) + "\n")
    return path, review


def _load_review(path):
    try:
        if path.is_symlink():
            raise OSError("Expected a regular review file")
        path = path.resolve(strict=True)
        review = read_local_json(path, ReleaseReview)
    except (OSError, ValueError, RecursionError) as error:
        raise ReleaseError("INVALID_RELEASE_EVIDENCE", "review", str(error)) from error
    return path, review


def _load_approved_review(path, inputs, installation, approval):
    path, review = _load_review(path)
    if approval.sha256 != review.digest:
        raise ReleaseError("APPROVAL_MISMATCH", "approve", "Approve the exact digest produced by prepare-release")
    summary = review.summary
    if (summary.method, summary.candidate_sha256, summary.manifest_sha256) != (
        inputs.method, inputs.candidate_sha256, inputs.manifest_sha256,
    ) or (summary.config, summary.source_sha256, summary.qgis_entrypoint_sha256) != (
        installation.config, installation.source_sha256, installation.qgis_entrypoint_sha256,
    ) or summary.environment.get("installation") != installation.environment:
        raise ReleaseError("RELEASE_INPUT_CHANGED", "review", "Candidate, package, or target installation differs from review")
    if _summarize(inputs, installation, path.parent) != review:
        raise ReleaseError("INVALID_RELEASE_EVIDENCE", "review", "Review summary or raw report digest differs")
    return path, review


@contextmanager
def _locked(directory):
    try:
        with exclusive_local_lock(directory / LOCK_NAME):
            yield
    except BlockingIOError as error:
        raise ReleaseError("CATALOG_BUSY", "catalog_dir", "Another release operation holds the catalog lock") from error
    except OSError as error:
        raise ReleaseError("RELEASE_STORAGE_FAILED", "catalog_dir", str(error)) from error


def _append(directory, ledger, builtins, event):
    updated = ReleaseLedger(events=[*ledger.events, event])
    ledger_state(updated, builtins)
    write_ledger(directory, updated)
    return {"scope": "local_method_release", "ledger": str(directory / LEDGER_NAME),
            "event": event.model_dump(mode="json", exclude={"candidate_yaml"})}


def prepare_release(catalog_dir, candidate, manifest, work_dir, *, qgis_process, timeout):
    inputs = _inputs(catalog_dir, candidate, manifest, qgis_process, timeout)
    path, review = _run_cases(inputs, _installation(inputs), work_dir)
    return {"scope": "local_method_review", "review_path": str(path),
            "approval_sha256": review.digest, "review": review.model_dump(mode="json")}


def publish_release(catalog_dir, candidate, manifest, work_dir, *, qgis_process, timeout, review_path, approval):
    inputs = _inputs(catalog_dir, candidate, manifest, qgis_process, timeout)
    installation = _installation(inputs)
    approved_path, approved = _load_approved_review(review_path, inputs, installation, approval)
    replay_path, replay = _run_cases(inputs, installation, work_dir)
    if replay.summary != approved.summary:
        raise ReleaseError("RELEASE_REPLAY_MISMATCH", "review", f"Actual replay differs. Replay: {replay_path}")
    with _locked(inputs.catalog):
        ledger, state, builtins = _catalog_data(inputs.catalog)
        _require_new(inputs.method, state, builtins)
        _unchanged(inputs, installation)
        _load_approved_review(approved_path, inputs, installation, approval)
        if _load_review(replay_path)[1] != replay or _summarize(inputs, installation, replay_path.parent) != replay:
            raise ReleaseError("INVALID_RELEASE_EVIDENCE", "review", f"Replay evidence changed: {replay_path}")
        return _append(inputs.catalog, ledger, builtins, Published(
            method=inputs.method, created_at=datetime.now(UTC), approval=approval,
            candidate_yaml=inputs.candidate_bytes.decode("utf-8"), review=approved, review_path=str(approved_path),
            replay=replay, replay_path=str(replay_path),
        ))


def disable_release(catalog_dir, method, *, approver, reason):
    directory = _directory(catalog_dir)
    ref = _reference(method, "method")
    with _locked(directory):
        ledger, state, builtins = _catalog_data(directory)
        if ref not in state.published or ref in state.disabled:
            raise ReleaseError("METHOD_NOT_ENABLED", "method", "Select an enabled published version")
        return _append(directory, ledger, builtins, Disabled(
            method=ref, created_at=datetime.now(UTC),
            approval=_approval(state.published[ref].approval.sha256, approver, reason),
        ))


def rollback_release(catalog_dir, source, target, *, approval):
    directory = _directory(catalog_dir)
    ref = _reference(source, "from")
    target_ref = _reference(target, "to")
    with _locked(directory):
        ledger, state, builtins = _catalog_data(directory)
        if approval.sha256 != target_digest(target_ref, builtins, state.published):
            raise ReleaseError("APPROVAL_MISMATCH", "approve", "Approve the exact rollback target digest")
        return _append(directory, ledger, builtins, RolledBack(
            method=ref, target=target_ref, created_at=datetime.now(UTC), approval=approval,
        ))


def add_release_commands(commands):
    for command, help_text in zip(RELEASE_COMMANDS, (
        "Run all release review cases in a new directory", "Approve, replay and publish an exact method version",
        "Disable an exact published method", "Roll back to an exact earlier method version",
    ), strict=True):
        parser = commands.add_parser(command, help=help_text, allow_abbrev=False)
        parser.add_argument("--catalog-dir", type=Path, required=True, help="target configuration methods/ directory")
        if command in {"prepare-release", "publish"}:
            parser.add_argument("--method", type=Path, required=True, help="external UTF-8 method YAML")
            parser.add_argument("--package", type=Path, required=True, help="reference package manifest")
            parser.add_argument("--work-dir", type=Path, required=True, help="new evidence directory")
            parser.add_argument("--qgis-process", type=Path, help="trusted local QGIS Processing executable")
            parser.add_argument("--timeout", type=float, default=120, help="deadline in seconds for each case")
        if command == "publish":
            parser.add_argument("--review", type=Path, required=True, help="review.json from prepare-release")
        if command == "disable":
            parser.add_argument("--method", required=True, help="published ID@VERSION")
        if command == "rollback":
            parser.add_argument("--from", dest="source", required=True, help="enabled published ID@VERSION")
            parser.add_argument("--to", dest="target", required=True, help="earlier approved ID@VERSION")
        if command in {"publish", "rollback"}:
            parser.add_argument("--approve", required=True, help="approved SHA-256 digest")
        if command != "prepare-release":
            parser.add_argument("--approver", required=True, help="name of local maintainer")
            parser.add_argument("--reason", required=True, help="human approval or maintenance reason")


def execute_release(args):
    try:
        if args.command == "disable":
            return disable_release(args.catalog_dir, args.method, approver=args.approver, reason=args.reason)
        if args.command == "prepare-release":
            return prepare_release(args.catalog_dir, args.method, args.package, args.work_dir,
                                   qgis_process=args.qgis_process, timeout=args.timeout)
        approval = _approval(args.approve, args.approver, args.reason)
        if args.command == "rollback":
            return rollback_release(args.catalog_dir, args.source, args.target, approval=approval)
        return publish_release(args.catalog_dir, args.method, args.package, args.work_dir, qgis_process=args.qgis_process,
                               timeout=args.timeout, review_path=args.review, approval=approval)
    except OSError as error:
        raise ReleaseError("RELEASE_STORAGE_FAILED", "release", str(error)) from error
