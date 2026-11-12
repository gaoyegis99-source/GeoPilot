"""Store and verify the append-only local method release history."""

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StringConstraints, ValidationError

from geopilot.methods.models import MethodDefinition, MethodRef


LEDGER_NAME = "release-ledger.json"
LOCK_NAME = ".release-ledger.lock"
_Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_CaseId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]


class ReleaseError(ValueError):
    def __init__(self, code: str, field: str, message: str, *, exit_code: int = 1):
        super().__init__(message)
        self.code, self.field, self.exit_code = code, field, exit_code


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)


class CaseVerdict(_Record):
    case_id: _CaseId
    verdict: Literal["positive_match", "expected_failure_matched"]
    phase: Literal["reference", "draft", "confirm"]
    code: _Text | None = None
    outputs: dict[str, JsonValue] = Field(default_factory=dict)


class ReleaseSummary(_Record):
    method: MethodRef
    candidate_sha256: _Digest
    manifest_sha256: _Digest
    config: dict[str, _Digest]
    source_sha256: _Digest
    qgis_entrypoint_sha256: _Digest | None
    environment: dict[str, JsonValue]
    cases: Annotated[list[CaseVerdict], Field(min_length=1)]


class ReportReceipt(_Record):
    case_id: _CaseId
    path: _Text
    sha256: _Digest


class ReleaseReview(_Record):
    schema_version: Literal[1] = 1
    summary: ReleaseSummary
    reports: Annotated[list[ReportReceipt], Field(min_length=1)]

    @property
    def digest(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class Approval(_Record):
    approver: _Text
    reason: _Text
    sha256: _Digest


class _Event(_Record):
    method: MethodRef
    created_at: AwareDatetime
    approval: Approval


class Published(_Event):
    action: Literal["publish"] = "publish"
    candidate_yaml: _Text
    review: ReleaseReview
    review_path: _Text
    replay_path: _Text
    replay: ReleaseReview


class Disabled(_Event):
    action: Literal["disable"] = "disable"


class RolledBack(_Event):
    action: Literal["rollback"] = "rollback"
    target: MethodRef


ReleaseEvent = Annotated[Published | Disabled | RolledBack, Field(discriminator="action")]


class ReleaseLedger(_Record):
    schema_version: Literal[1] = 1
    events: list[ReleaseEvent] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReleaseState:
    published: dict[MethodRef, Published]
    disabled: frozenset[MethodRef]


def fingerprint(value: JsonValue) -> str:
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _ordered_receipts(review: ReleaseReview) -> bool:
    cases = review.summary.cases
    ids = [case.case_id for case in cases]
    return (
        cases[0].verdict == "positive_match" and cases[0].phase == "reference" and cases[0].code is None
        and bool(cases[0].outputs)
        and all(case.verdict == "expected_failure_matched" and case.phase in {"draft", "confirm"}
                and case.code and not case.outputs for case in cases[1:])
        and len(set(ids)) == len(ids) and ids == [item.case_id for item in review.reports]
        and all(item.path == f"cases/{item.case_id}/report.json" for item in review.reports)
    )


def ledger_state(ledger: ReleaseLedger, builtins: Mapping[MethodRef, MethodDefinition]) -> ReleaseState:
    """Check the event chain and derive exact definitions and selection state."""
    published = {}
    disabled = set()
    for event in ledger.events:
        ref = event.method
        if isinstance(event, Published):
            if ref in published or ref in builtins:
                raise ReleaseError("METHOD_VERSION_EXISTS", "method", "The exact method version already exists")
            summary = event.review.summary
            if (
                summary.method != ref or event.approval.sha256 != event.review.digest
                or hashlib.sha256(event.candidate_yaml.encode("utf-8")).hexdigest() != summary.candidate_sha256
                or summary != event.replay.summary
                or not _ordered_receipts(event.review) or not _ordered_receipts(event.replay)
            ):
                raise ReleaseError("INVALID_RELEASE_EVIDENCE", "review", "The publication evidence is inconsistent")
            published[ref] = event
            continue
        if ref not in published or ref in disabled:
            raise ReleaseError("METHOD_NOT_ENABLED", "method", "The source must be an enabled published version")
        if isinstance(event, Disabled):
            if event.approval.sha256 != published[ref].approval.sha256:
                raise ReleaseError("APPROVAL_MISMATCH", "approve", "Disable must reference the source approval")
        else:
            target = event.target
            if ref.id != target.id or ref.version <= target.version:
                raise ReleaseError("INVALID_ROLLBACK_TARGET", "to", "Select an earlier exact version of the same method")
            if event.approval.sha256 != target_digest(target, builtins, published):
                raise ReleaseError("APPROVAL_MISMATCH", "approve", "Rollback must reference the exact target approval")
            disabled.discard(target)
        disabled.add(ref)
    return ReleaseState(published, frozenset(disabled))


def target_digest(
    ref: MethodRef, builtins: Mapping[MethodRef, MethodDefinition], published: Mapping[MethodRef, Published],
) -> str:
    if ref in builtins:
        return fingerprint(builtins[ref].model_dump(mode="json"))
    if ref in published:
        return published[ref].approval.sha256
    raise ReleaseError("METHOD_NOT_FOUND", "to", "The rollback target does not exist")


def _unique_object(items):
    value = dict(items)
    if len(value) != len(items):
        raise ValueError("Duplicate JSON key in release evidence")
    return value


def read_local_json(path: Path, model: type[_Record]):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError(f"Expected a regular JSON file: {path}")
        data = stream.read()
    json.loads(data, object_pairs_hook=_unique_object)
    return model.model_validate_json(data)


def read_ledger(directory: Path) -> ReleaseLedger:
    try:
        return read_local_json(directory / LEDGER_NAME, ReleaseLedger)
    except FileNotFoundError:
        return ReleaseLedger()
    except (OSError, ValidationError, ValueError, RecursionError) as error:
        raise ReleaseError("INVALID_RELEASE_LEDGER", "catalog_dir", str(error)) from error


def write_ledger(directory: Path, ledger: ReleaseLedger) -> None:
    """Replace a fully validated ledger while the caller holds the catalog lock."""
    with NamedTemporaryFile(dir=directory, prefix=".release-ledger-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write((ledger.model_dump_json(indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, directory / LEDGER_NAME)
        finally:
            temporary.unlink(missing_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
