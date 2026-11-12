"""Bind a second-round delivery to immutable source and validation evidence."""

import gzip
import io
import json
import posixpath
import re
import tarfile
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from build_source_inventory import git
from check_doc_evidence_links import heading_anchors, references
from verify_delivery_manifest import committed_file_states, committed_files, read_records, sha256, worktree_file_state


MANIFEST = Path("artifacts/reports/final/round2-delivery-manifest.json")
RECEIPT = "artifacts/reports/final/round2-validation.json"
_SOURCE_DIRECTORIES = ("src", "config", "frontend", "scripts", "tests", "benchmarks", "artifacts/schemas")
_FIXED_GIS_MAPPING = "docs/round2/audit/p12-07-mapping.json"
_SOURCE_FILES = ("README.md", "pyproject.toml", "requirements.txt", "requirements-test.txt",
                 "artifacts/reports/final/delivery_manifest.py", _FIXED_GIS_MAPPING)
_WORKFLOW = ".github/workflows/ci.yml"
_REQUIRED_SUITES = {"backend", "frontend", "fixed_gis"}
_FIXED_CASES = {f"T{number:02}" for number in range(1, 17)}
_HASH = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("JSON contains a duplicate key")
    return result


def _resolve_commit(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode("ascii").strip()


def _file_path(value: object) -> str:
    if not isinstance(value, str) or not value or not value.isprintable():
        raise ValueError("Expected a printable repository-relative file path")
    path = PurePosixPath(value)
    if (
        path.is_absolute() or not path.parts or ".." in path.parts or path.as_posix() != value
        or PureWindowsPath(value).drive or "\\" in value
    ):
        raise ValueError(f"Unsafe repository-relative file path: {value}")
    return value


def _scope(tree: dict, prefix: str) -> set[str]:
    directories = tuple(prefix + name + "/" for name in _SOURCE_DIRECTORIES)
    files = {prefix + name for name in _SOURCE_FILES} | {_WORKFLOW}
    return files | {path for path in tree if path.startswith(directories)}


def _read_file(repo: Path, tree: dict, path: str) -> bytes:
    mode, oid = tree.get(path, ("", ""))
    if mode not in {"100644", "100755"}:
        raise ValueError(f"Delivery requires a committed regular file: {path}")
    return git(repo, "cat-file", "blob", oid)


def _records(repo: Path, tree: dict, paths: set[str]) -> list[dict]:
    states = committed_file_states(repo, tree, paths)
    records = []
    for path in sorted(paths):
        state = states.get(path)
        if state is None or state.problem:
            raise ValueError(f"Delivery requires a committed regular file: {path}")
        records.append({"path": path, "sha256": state.sha256})
    return records


def _count_cases(document: ElementTree.Element) -> dict[str, int]:
    counts = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    for case in document.iter("testcase"):
        counts["tests"] += 1
        kinds = {child.tag for child in case if child.tag in {"failure", "error", "skipped"}}
        if len(kinds) > 1:
            raise ValueError("JUnit test case declares multiple outcomes")
        if kinds:
            counts[{"failure": "failures", "error": "errors", "skipped": "skipped"}[kinds.pop()]] += 1
    for key, value in counts.items():
        if key in document.attrib and int(document.attrib[key]) != value:
            raise ValueError(f"JUnit aggregate differs from its test cases: {key}")
    return {"passed": counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"], **counts}


def _junit_report(data: bytes) -> tuple[ElementTree.Element, dict[str, int]]:
    try:
        document = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise ValueError("Invalid JUnit test report") from error
    if document.tag not in {"testsuite", "testsuites"}:
        raise ValueError("Expected a JUnit test report")
    counts = _count_cases(document)
    if not counts["tests"]:
        raise ValueError("A release suite requires test cases")
    for suite in document.iter("testsuite"):
        if suite is not document:
            _count_cases(suite)
    return document, counts


def _check_fixed_gis(document: ElementTree.Element, mapping: bytes) -> None:
    content = json.loads(mapping, object_pairs_hook=_json_object)
    cases = content.get("fixed_cases") if isinstance(content, dict) else None
    if not isinstance(cases, dict) or cases.keys() != _FIXED_CASES:
        raise ValueError("Fixed GIS mapping must cover T01 through T16")
    observed = Counter((node.get("classname"), node.get("name")) for node in document.iter("testcase"))
    for case_id, case in sorted(cases.items()):
        nodes = case.get("nodes") if isinstance(case, dict) else None
        if not isinstance(nodes, list) or not nodes:
            raise ValueError(f"Fixed GIS mapping has no nodes for {case_id}")
        for node in nodes:
            node_id = node.get("id") if isinstance(node, dict) else None
            if not isinstance(node_id, str):
                raise ValueError(f"Invalid fixed GIS node for {case_id}")
            file, separator, test = node_id.partition("::")
            if not separator or not file.startswith("tests/") or not file.endswith(".py") or not test:
                raise ValueError(f"Invalid fixed GIS node for {case_id}: {node_id}")
            module = file.removesuffix(".py").replace("/", ".")
            test_function, bracket, parameters = test.partition("[")
            test_class, _, function_name = test_function.rpartition("::")
            classname = f"{module}.{test_class.replace('::', '.')}" if test_class else module
            name = function_name + bracket + parameters
            if observed[classname, name] != 1:
                raise ValueError(f"Missing or duplicate fixed GIS node for {case_id}: {node_id}")


def _observed_suite(name: str, junit: bytes, log: bytes, source_commit: str, fixed_mapping: bytes) -> dict:
    log_lines = log.decode("utf-8").splitlines()
    if f"commit {source_commit}" not in log_lines or "exit 0" not in log_lines:
        raise ValueError(f"Test log does not bind the successful suite to the source commit: {name}")
    document, counts = _junit_report(junit)
    if counts["failures"] or counts["errors"] or not counts["passed"] or (name == "fixed_gis" and counts["skipped"]):
        raise ValueError(f"Release suite has failures, errors or incomplete fixed GIS coverage: {name}")
    if name == "fixed_gis":
        _check_fixed_gis(document, fixed_mapping)
    return counts


def _suites(repo: Path, tree: dict, receipt: dict, source_commit: str, fixed_mapping: bytes) -> tuple[list[dict], set[str]]:
    entries = receipt.get("suites")
    if not isinstance(entries, list):
        raise ValueError("Validation requires a suites array")
    results, evidence = {}, set()
    for suite in entries:
        if not isinstance(suite, dict) or set(suite) != {"name", "command", "environment", "exit_code", "junit", "log"}:
            raise ValueError("Invalid release validation suite")
        name, command, environment, exit_code = (suite[key] for key in ("name", "command", "environment", "exit_code"))
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name in results:
            raise ValueError("Release suite names must be unique identifiers")
        if (
            not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command)
            or not isinstance(environment, dict) or not environment
            or not all(isinstance(item, str) and item.strip() for item in environment.values())
            or type(exit_code) is not int or exit_code != 0
        ):
            raise ValueError(f"Incomplete command, environment or successful exit evidence for suite {name}")
        junit, log = _file_path(suite["junit"]), _file_path(suite["log"])
        if junit == log or {junit, log} & evidence:
            raise ValueError(f"Release suites must use distinct reports and logs: {name}")
        counts = _observed_suite(name, _read_file(repo, tree, junit), _read_file(repo, tree, log), source_commit, fixed_mapping)
        results[name] = {**suite, "counts": counts}
        evidence.update((junit, log))
    if not _REQUIRED_SUITES <= results.keys():
        raise ValueError("Release validation requires backend, frontend and fixed_gis suites")
    return [results[name] for name in sorted(results)], evidence


def _paths(value: object, label: str) -> set[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"Validation requires a nonempty {label} array")
    paths = {_file_path(item) for item in value}
    if len(paths) != len(value):
        raise ValueError(f"Duplicate validation paths in {label}")
    return paths


def _reproduction_links(repo: Path, tree: dict, documents: set[str]) -> set[str]:
    linked = set()
    for source in sorted(documents):
        if not source.endswith(".md"):
            raise ValueError(f"Expected a Markdown reproduction document: {source}")
        content = _read_file(repo, tree, source).decode("utf-8")
        for ref in references(source, content):
            if ref.kind != "markdown":
                continue
            url = urlsplit(ref.target)
            if url.scheme in {"http", "https"}:
                continue
            if url.scheme or url.netloc:
                raise ValueError(f"Unsupported reproduction link: {source}:{ref.line}")
            path = unquote(url.path)
            target = posixpath.normpath(posixpath.join(posixpath.dirname(source), path)) if path else source
            blob = _read_file(repo, tree, _file_path(target))
            anchor = unquote(url.fragment)
            if anchor and target.endswith(".md") and anchor not in heading_anchors(blob.decode("utf-8")):
                raise ValueError(f"Missing reproduction anchor: {source}:{ref.line} {ref.target}")
            linked.add(target)
    return linked


def build_round2_manifest(root: Path, source_ref: str, evidence_ref: str) -> dict:
    root = root.resolve(strict=True)
    repo = Path(git(root, "rev-parse", "--show-toplevel").decode().strip())
    relative = root.relative_to(repo).as_posix()
    prefix = "" if relative == "." else relative + "/"
    source_commit, evidence_commit = (_resolve_commit(repo, ref) for ref in (source_ref, evidence_ref))
    source_tree, evidence_tree = (committed_files(repo, commit) for commit in (source_commit, evidence_commit))
    source_paths = _scope(source_tree, prefix)
    if source_paths != _scope(evidence_tree, prefix) or any(
        source_tree.get(path) != evidence_tree.get(path) for path in source_paths
    ):
        raise ValueError("Validation evidence commit contains different release source")
    if any(not any(path.startswith(prefix + directory + "/") for path in source_paths) for directory in _SOURCE_DIRECTORIES):
        raise ValueError("A required release source directory is absent")
    receipt_path = prefix + RECEIPT
    receipt = json.loads(_read_file(repo, evidence_tree, receipt_path), object_pairs_hook=_json_object)
    if (
        not isinstance(receipt, dict) or set(receipt) != {"schema_version", "source_commit", "suites", "evidence", "reproduction"}
        or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
        or receipt["source_commit"] != source_commit
    ):
        raise ValueError("The validation receipt must bind to the exact source commit")
    mapping_path = prefix + _FIXED_GIS_MAPPING
    suites, suite_evidence = _suites(repo, evidence_tree, receipt, source_commit, _read_file(repo, source_tree, mapping_path))
    reproduction = _paths(receipt["reproduction"], "reproduction")
    selected = _paths(receipt["evidence"], "evidence")
    source = _records(repo, source_tree, source_paths)
    index = "".join(f"{entry['sha256']}  {entry['path']}\n" for entry in source)
    evidence_paths = {receipt_path} | suite_evidence | reproduction | selected
    evidence_paths.update(_reproduction_links(repo, evidence_tree, reproduction) - source_paths)
    if prefix + MANIFEST.as_posix() in evidence_paths:
        raise ValueError("A delivery manifest cannot include itself as evidence")
    return {
        "schema_version": 1, "release": "GeoPilot round2 candidate",
        "source_commit": source_commit, "evidence_commit": evidence_commit,
        "source_sha256": sha256(index.encode("utf-8")), "source_files": source,
        "validation_suites": suites, "fixed_gis_mapping": mapping_path, "reproduction": sorted(reproduction),
        "evidence": _records(repo, evidence_tree, evidence_paths),
    }


def verify_round2_manifest(root: Path, path: Path) -> dict:
    document = json.loads(path.read_bytes(), object_pairs_hook=_json_object)
    _manifest_records(document)
    rebuilt = build_round2_manifest(root, document["source_commit"], document["evidence_commit"])
    if json.dumps(document, sort_keys=True) != json.dumps(rebuilt, sort_keys=True):
        raise ValueError("Release manifest differs from the committed source and validation evidence")
    return {
        "verification": "matched", "source_commit": rebuilt["source_commit"], "evidence_commit": rebuilt["evidence_commit"],
        "source_files": len(rebuilt["source_files"]), "evidence_files": len(rebuilt["evidence"]),
        "validation_suites": {entry["name"]: entry["counts"] for entry in rebuilt["validation_suites"]},
    }


def _manifest_records(manifest: object) -> tuple[dict[str, str], dict[str, str]]:
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError("Expected a second-round delivery manifest")
    if not all(isinstance(ref := manifest.get(key), str) and _HASH.fullmatch(ref)
               for key in ("source_commit", "evidence_commit")):
        raise ValueError("A release manifest requires exact source and evidence commits")
    source = read_records(manifest.get("source_files"), "source_files")
    evidence = read_records(manifest.get("evidence"), "evidence")
    mapping_path = _file_path(manifest.get("fixed_gis_mapping"))
    if mapping_path not in source or not (mapping_path == _FIXED_GIS_MAPPING or mapping_path.endswith("/" + _FIXED_GIS_MAPPING)):
        raise ValueError("Release manifest requires the fixed GIS mapping as a source file")
    index = "".join(f"{source[path]}  {path}\n" for path in sorted(source))
    if not source or not evidence or sha256(index.encode("utf-8")) != manifest.get("source_sha256"):
        raise ValueError("Release source index does not match its manifest")
    if any(source[path] != evidence[path] for path in source.keys() & evidence.keys()):
        raise ValueError("Source and evidence disagree on a file digest")
    return source, evidence


def write_round2_archive(root: Path, manifest: dict, output: Path) -> None:
    """Export only the verified committed files, with deterministic archive metadata."""
    repo = Path(git(root, "rev-parse", "--show-toplevel").decode().strip())
    relative = root.resolve(strict=True).relative_to(repo).as_posix()
    manifest_path = posixpath.join(relative, MANIFEST.as_posix()).removeprefix("./")
    source, evidence = _manifest_records(manifest)
    source_tree, evidence_tree = (committed_files(repo, manifest[key]) for key in ("source_commit", "evidence_commit"))

    def add(archive, path, data, mode):
        entry = tarfile.TarInfo(path)
        entry.size = len(data)
        entry.mode = mode
        archive.addfile(entry, io.BytesIO(data))

    stream = output.open("xb")
    try:
        with stream, gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for path, digest in sorted((source | evidence).items()):
                    tree = source_tree if path in source else evidence_tree
                    data = _read_file(repo, tree, path)
                    if sha256(data) != digest:
                        raise ValueError(f"Committed delivery bytes differ from manifest: {path}")
                    add(archive, path, data, 0o755 if tree[path][0] == "100755" else 0o644)
                data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                add(archive, manifest_path, data, 0o644)
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def verify_round2_files(root: Path, manifest_path: Path) -> dict:
    """Verify an extracted package against the provided manifest without Git."""
    manifest = json.loads(manifest_path.read_bytes(), object_pairs_hook=_json_object)
    source, evidence = _manifest_records(manifest)
    root = root.resolve(strict=True)
    for path, digest in sorted((source | evidence).items()):
        state = worktree_file_state(root, path)
        if state is None or state.problem or state.sha256 != digest:
            raise ValueError(f"Extracted delivery bytes differ from manifest: {path}")
    suites = manifest.get("validation_suites")
    if not isinstance(suites, list):
        raise ValueError("A release manifest requires validation suites")
    fixed_mapping = (root / manifest["fixed_gis_mapping"]).read_bytes()
    observed = {}
    for suite in suites:
        if not isinstance(suite, dict) or not isinstance(name := suite.get("name"), str) or name in observed:
            raise ValueError("Invalid release validation suite")
        junit, log = _file_path(suite.get("junit")), _file_path(suite.get("log"))
        if junit not in evidence or log not in evidence or type(suite.get("exit_code")) is not int or suite["exit_code"] != 0:
            raise ValueError(f"Release suite evidence is incomplete: {name}")
        counts = _observed_suite(name, (root / junit).read_bytes(), (root / log).read_bytes(),
                                 manifest["source_commit"], fixed_mapping)
        if json.dumps(counts, sort_keys=True) != json.dumps(suite.get("counts"), sort_keys=True):
            raise ValueError(f"Extracted suite counts differ from manifest: {name}")
        observed[name] = counts
    if not _REQUIRED_SUITES <= observed.keys():
        raise ValueError("Release validation requires backend, frontend and fixed_gis suites")
    return {
        "verification": "matched", "source_commit": manifest["source_commit"], "evidence_commit": manifest["evidence_commit"],
        "source_files": len(source), "evidence_files": len(evidence),
        "validation_suites": observed,
    }
