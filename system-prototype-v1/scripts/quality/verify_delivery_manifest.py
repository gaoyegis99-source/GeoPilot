"""Compare the original GeoPilot delivery manifest with Git and the working tree."""

import argparse
import csv
import hashlib
import json
import re
import runpy
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from build_source_inventory import git


ROOT = Path(__file__).resolve().parents[2]
BASELINE = "docs/round2/audit/baseline.json"
LINK_AUDIT = "docs/round2/audit/evidence-link-audit.csv"
RUN_OUTPUTS = ("artifacts/smoke/", "artifacts/reproduction/", "artifacts/test-results/")
SHA256 = re.compile(r"[0-9a-f]{64}")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def read_records(rows: object, label: str) -> dict[str, str]:
    if not isinstance(rows, list):
        raise ValueError(f"{label} must be an array of file records")
    records = {}
    for row in rows:
        if not isinstance(row, dict) or row.keys() != {"path", "sha256"}:
            raise ValueError(f"{label} contains a malformed file record")
        path, digest = row["path"], row["sha256"]
        if not isinstance(path, str) or not path or not path.isprintable():
            raise ValueError(f"{label} contains an invalid file path")
        posix = PurePosixPath(path)
        if (posix.is_absolute() or PureWindowsPath(path).drive or "\\" in path
                or ".." in posix.parts or not posix.parts or posix.as_posix() != path):
            raise ValueError(f"{label} contains an unsafe relative path: {path}")
        if path in records or not is_sha256(digest):
            raise ValueError(f"{label} contains a duplicate path or invalid SHA-256: {path}")
        records[path] = digest
    return records


def read_manifest(data: bytes) -> tuple[dict, dict[str, str], dict[str, str], dict[str, str]]:
    manifest = json.loads(data)
    if not isinstance(manifest, dict) or not all(
        isinstance(manifest.get(key), str) and manifest[key] for key in ("release", "acceptance_date")
    ):
        raise ValueError("Manifest requires release and acceptance_date strings")
    if not is_sha256(manifest.get("source_sha256")):
        raise ValueError("Manifest requires a valid source_sha256")
    source = read_records(manifest.get("source_files"), "source_files")
    groups = manifest.get("evidence")
    if not isinstance(groups, dict):
        raise ValueError("Manifest requires an evidence group object")
    evidence = {}
    labels = {}
    for group, rows in groups.items():
        records = read_records(rows, f"evidence.{group}")
        for path, digest in records.items():
            if path in evidence:
                raise ValueError(f"Manifest evidence path occurs in multiple groups: {path}")
            evidence[path] = digest
            labels[path] = group
    return manifest, source, evidence, labels


def nul_entries(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="surrogateescape").rstrip("\0").split("\0") if data else []


def committed_files(root: Path, commit: str) -> dict[str, tuple[str, str]]:
    files = {}
    for entry in nul_entries(git(root, "ls-tree", "-rz", commit, "--", ".")):
        metadata, path = entry.split("\t", 1)
        mode, _, oid = metadata.split()
        files[path] = (mode, oid)
    return files


@dataclass(frozen=True)
class FileState:
    sha256: str | None = None
    problem: str | None = None


def committed_file_states(root: Path, files: dict, paths: set[str]) -> dict[str, FileState]:
    states = {}
    digests = {}
    for path in sorted(files.keys() & paths):
        mode, oid = files[path]
        if mode not in {"100644", "100755"}:
            states[path] = FileState(problem=f"Git mode {mode} is not a regular file")
            continue
        if oid not in digests:
            digests[oid] = sha256(git(root, "cat-file", "blob", oid))
        states[path] = FileState(sha256=digests[oid])
    return states


def worktree_file_state(root: Path, path: str) -> FileState | None:
    location = root
    for part in PurePosixPath(path).parts:
        location = location / part
        if location.is_symlink():
            return FileState(problem="Path contains a symlink")
    if not location.exists():
        return None
    if not location.is_file():
        return FileState(problem="Path is not a regular file")
    with location.open("rb") as source:
        return FileState(sha256=hashlib.file_digest(source, "sha256").hexdigest())


def source_paths(paths: set[str], scope: dict) -> set[str]:
    prefixes = tuple(name + "/" for name in scope["SOURCE_DIRECTORIES"])
    return set(scope["SOURCE_FILES"]) | {
        path for path in paths
        if path.startswith(prefixes) and "__pycache__" not in PurePosixPath(path).parts
        and PurePosixPath(path).suffix not in {".pyc", ".pyo"}
    }


def link_references(path: Path, commit: str, prefix: str) -> dict[str, list[dict]]:
    references: dict[str, list[dict]] = {}
    with path.open(encoding="utf-8", newline="") as stream:
        rows = csv.DictReader(stream)
        required = {"commit", "source", "line", "status", "resolved"}
        if not required <= set(rows.fieldnames or ()):
            raise ValueError("Evidence link audit CSV has missing columns")
        for row in rows:
            if row["commit"] != commit or not (row["resolved"] or "").startswith(prefix):
                continue
            resolved = row["resolved"].removeprefix(prefix)
            references.setdefault(resolved, []).append({
                key: row[key] for key in ("source", "line", "status")
            })
    return references


def compare(
    expected: dict[str, str], paths: set[str], states: dict[str, FileState],
) -> dict:
    counts = Counter({status: 0 for status in ("matched", "added", "missing", "changed", "invalid")})
    differences = []
    for path in sorted(paths):
        before, after = expected.get(path), states.get(path)
        if after is None:
            status = "missing"
        elif after.problem:
            status = "invalid"
        elif before is None:
            status = "added"
        elif after.sha256 != before:
            status = "changed"
        else:
            status = "matched"
        counts[status] += 1
        if status != "matched":
            entry = {"path": path, "status": status, "manifest_sha256": before,
                     "observed_sha256": after.sha256 if after else None}
            if after and after.problem:
                entry["problem"] = after.problem
            differences.append(entry)
    return {"counts": dict(counts), "differences": differences}


def evidence_report(
    recorded: dict, paths: set[str], states: dict, labels: dict, references: dict, pinned_files: dict,
) -> dict:
    report = compare(recorded, paths, states)
    for entry in report["differences"]:
        path = entry["path"]
        entry["group"] = labels.get(path, "unlisted_run_output")
        if entry["status"] == "missing" and path not in pinned_files and path.startswith(RUN_OUTPUTS):
            entry["historical_classification"] = "run_output_absent_from_pinned_commit"
            entry["p12_03_references"] = references.get(path, [])
    return report


def audit(root: Path, ref: str, links: Path | None) -> dict:
    # Load the in-repository V1 declarations, independent of the checkout being inspected.
    scope = runpy.run_path(str(ROOT / "artifacts/reports/final/delivery_manifest.py"))
    manifest_path = scope["MANIFEST"].as_posix()
    commit = git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    tree = committed_files(root, commit)
    mode, oid = tree.get(manifest_path, ("", ""))
    if mode not in {"100644", "100755"}:
        raise ValueError(f"Commit {commit} has no regular delivery manifest at {manifest_path}")
    raw = git(root, "cat-file", "blob", oid)
    manifest, source, evidence, labels = read_manifest(raw)
    stored_index = manifest["source_sha256"]
    computed_index = sha256("".join(f"{digest}  {path}\n" for path, digest in source.items()).encode())
    manifest_sha = sha256(raw)
    labels = {path: group for group, paths in scope["EVIDENCE"].items() for path in paths} | labels
    prefix = git(root, "rev-parse", "--show-prefix").decode().strip()
    references = link_references(links, commit, prefix) if links is not None else {}

    pinned_source = source_paths(set(tree), scope) | source.keys()
    pinned_evidence = set(labels) | {path for path in tree if path.startswith(RUN_OUTPUTS)}
    pinned_states = committed_file_states(root, tree, pinned_source | pinned_evidence)
    pinned = {
        "commit": commit,
        "source": compare(source, pinned_source, pinned_states),
        "evidence": evidence_report(evidence, pinned_evidence, pinned_states, labels, references, tree),
    }

    tracked = set(nul_entries(git(root, "ls-files", "--cached", "-z", "--", ".")))
    untracked = set(nul_entries(git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", ".")))
    live_source = source_paths(tracked | untracked, scope) | source.keys()
    live_evidence = set(labels) | {path for path in tracked if path.startswith(RUN_OUTPUTS)}
    live_states = {}
    for path in sorted(live_source | live_evidence | {manifest_path}):
        state = worktree_file_state(root, path)
        if state is not None:
            live_states[path] = state
    worktree = {
        "head": git(root, "rev-parse", "HEAD").decode().strip(),
        "dirty": bool(git(root, "status", "--porcelain=v1", "--", ".")),
        "manifest": compare({manifest_path: manifest_sha}, {manifest_path}, live_states),
        "source": compare(source, live_source, live_states),
        "evidence": evidence_report(evidence, live_evidence, live_states, labels, references, tree),
    }
    index_matches = stored_index == computed_index
    verified = index_matches and not any(
        section["differences"]
        for section in (pinned["source"], pinned["evidence"], worktree["source"],
                        worktree["evidence"], worktree["manifest"])
    )
    return {
        "verification": "matched" if verified else "different",
        "manifest": {
            "path": manifest_path, "release": manifest["release"],
            "acceptance_date": manifest["acceptance_date"], "sha256": manifest_sha,
            "source_index": {"stored": stored_index, "computed": computed_index, "matches": index_matches},
        },
        "pinned": pinned,
        "worktree": worktree,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="prototype directory")
    parser.add_argument("--ref", help="commit to audit; defaults to the P12-01 baseline")
    parser.add_argument("--evidence-audit", type=Path, help="P12-03 CSV used to annotate missing evidence")
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        ref = args.ref
        if ref is None:
            baseline = json.loads((root / BASELINE).read_bytes())
            if not isinstance(baseline, dict):
                raise ValueError("Audit baseline must be a JSON object")
            ref = baseline.get("commit")
        if not isinstance(ref, str) or not ref:
            raise ValueError("Missing audit commit")
        links = args.evidence_audit
        if links is None and (root / LINK_AUDIT).is_file():
            links = root / LINK_AUDIT
        report = audit(root, ref, links)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(2, f"Delivery manifest audit failed: {error}\n")
    print(json.dumps(report, indent=2))
    return int(report["verification"] != "matched")


if __name__ == "__main__":
    raise SystemExit(main())
