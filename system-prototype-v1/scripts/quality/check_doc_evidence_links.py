"""Inventory Markdown links and evidence paths against a committed Git tree."""

import argparse
import csv
import posixpath
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlsplit


DESTINATION = r"""(?:<(?P<angle>[^<>\n]*)>|(?P<plain>(?:\\.|[^\s()\\]|\([^()\n]*\))*))"""
TITLE = r"""(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\)))?"""
LINK = re.compile(
    r"(?<!\\)\[(?:\\.|[^\[\]\\]|\[[^\[\]]*\])*\]\(\s*" + DESTINATION + TITLE + r"\s*\)"
)
DEFINITION = re.compile(r"^ {0,3}\[[^]\n]+\]:[ \t]*" + DESTINATION + TITLE + r"\s*$")
INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.*?)\1(?!`)")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
BARE_PATH = re.compile(
    r"""(?<![\w./\\:-])(?P<path>(?:\.{1,2}/|…/|/?(?:artifacts|docs|plans(?:-round2)?|"""
    r"""tests|scripts|frontend|config|src|local-data|\.environments)/|"""
    r"""/(?:tmp|var/tmp|home|mnt|private/tmp)/)[\w.\-/@%+#*{}]+)"""
)
GENERATED = (
    "artifacts/smoke", "artifacts/reproduction", "artifacts/test-results",
    ".environments", "local-data",
)
MACHINE = ("/tmp/", "/var/tmp/", "/private/tmp/", "/home/", "/mnt/", "~/")
LINE_ANCHOR = re.compile(r"L([1-9]\d*)(?:-L([1-9]\d*))?")


def git(root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False
    )
    if process.returncode:
        raise ValueError(process.stderr.decode(errors="replace").strip())
    return process.stdout.decode("utf-8", errors="surrogateescape")


@dataclass(frozen=True)
class Reference:
    source: str
    line: int
    kind: str
    target: str


@dataclass(frozen=True)
class Finding:
    commit: str
    source: str
    line: int
    kind: str
    target: str
    status: str
    resolved: str
    note: str


def markdown_lines(content: str):
    """Yield source lines with a flag for fenced and indented code."""
    fence = None
    for number, line in enumerate(content.splitlines(), 1):
        match = FENCE.match(line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                fence = None
            else:
                yield number, line, True
        elif match:
            fence = match[1]
        else:
            yield number, line, line.startswith(("    ", "\t"))


def mask(line: str, matches) -> str:
    for match in reversed(list(matches)):
        line = line[:match.start()] + " " * len(match[0]) + line[match.end():]
    return line


def references(source: str, content: str):
    for number, line, code in markdown_lines(content):
        if not code:
            prose = mask(line, INLINE_CODE.finditer(line))
            definition = DEFINITION.fullmatch(prose)
            matches = [definition] if definition else list(LINK.finditer(prose))
            for match in matches:
                target = match["angle"] if match["angle"] is not None else match["plain"]
                yield Reference(source, number, "markdown", target)
            line = mask(line, matches)
        for match in BARE_PATH.finditer(line):
            target = match["path"].rstrip(".")
            if target:
                yield Reference(source, number, "bare_path", target)


@lru_cache(maxsize=None)
def heading_anchors(content: str) -> frozenset[str]:
    anchors = set()
    previous = ""
    for _, line, code in markdown_lines(content):
        if code:
            previous = ""
            continue
        anchors.update(re.findall(r"""<(?:a|[a-z][a-z0-9]*)\b[^>]*\b(?:id|name)=["']([^"']+)["']""", line))
        heading = re.match(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", line)
        if heading:
            name = heading[1]
        elif previous and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", line):
            name = previous
        else:
            previous = line.strip()
            continue
        previous = ""
        name = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", name)
        name = re.sub(r"<[^>]+>", "", name).lower()
        base = re.sub(r"[^\w -]", "", name).replace(" ", "-")
        candidate = base
        suffix = 0
        while candidate in anchors:
            suffix += 1
            candidate = f"{base}-{suffix}"
        anchors.add(candidate)
    return frozenset(anchors)


class GitSnapshot:
    def __init__(self, root: Path, ref: str):
        self.repo = Path(git(root, "rev-parse", "--show-toplevel").strip())
        self.prefix = root.relative_to(self.repo).as_posix()
        if self.prefix == ".":
            self.prefix = ""
        self.commit = git(self.repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()
        tree = git(self.repo, "ls-tree", "-r", "-t", "-z", "--full-tree", self.commit)
        self.entries = {}
        for entry in filter(None, tree.split("\0")):
            metadata, path = entry.split("\t", 1)
            self.entries[path] = metadata.split()[1]
        self.removed, self.renamed = self.path_history()

    def path_history(self):
        entries = iter(git(
            self.repo, "log", "--format=%H", "--name-status", "-z", "-M",
            "--diff-filter=DR", self.commit,
        ).split("\0"))
        removed, renamed = {}, {}
        commit = ""
        for entry in entries:
            status = entry.strip()
            if status == "D":
                removed.setdefault(next(entries), commit)
            elif re.fullmatch(r"R\d+", status):
                old, new = next(entries), next(entries)
                renamed.setdefault(old, new)
            elif re.fullmatch(r"[0-9a-f]{40,64}", status):
                commit = status
        return removed, renamed

    @lru_cache(maxsize=None)
    def read(self, path: str) -> str:
        return git(self.repo, "show", f"{self.commit}:{path}")

    def documents(self):
        prefix = self.prefix + "/" if self.prefix else ""
        return sorted(
            path for path, kind in self.entries.items()
            if kind == "blob" and path.startswith(prefix) and path.lower().endswith(".md")
        )

    def moved_to(self, path: str) -> str | None:
        seen = {path}
        while destination := self.renamed.get(path):
            if destination in self.entries:
                return destination
            if destination in seen:
                break
            seen.add(destination)
            path = destination
        return None

    def generated_path(self, path: str) -> bool:
        relative = posixpath.relpath(path, self.prefix or ".")
        return any(relative == entry or relative.startswith(entry + "/") for entry in GENERATED)

    def resolve_path(self, ref: Reference, path: str) -> str:
        if path.startswith("/"):
            return posixpath.normpath(path.lstrip("/"))
        directory = posixpath.dirname(ref.source)
        if ref.kind == "markdown" or path.startswith("./"):
            return posixpath.normpath(posixpath.join(directory, path))
        if path.startswith("…/"):
            path = path[2:]
        frontend = posixpath.join(self.prefix, "frontend")
        bases = (directory, self.prefix, frontend) if path.startswith("../") else (
            self.prefix, frontend, directory
        )
        candidates = [posixpath.normpath(posixpath.join(base, path)) for base in bases]
        for candidate in candidates:
            if (candidate in self.entries or candidate in self.removed
                    or self.moved_to(candidate) or self.generated_path(candidate)):
                return candidate
        return candidates[0]

    def classify(self, ref: Reference) -> tuple[str, str, str]:
        try:
            url = urlsplit(ref.target)
        except ValueError as error:
            return "invalid_target", "", str(error)
        if url.scheme == "file":
            return "machine_local", unquote(url.path), "requires original machine or a rerun"
        if url.scheme or url.netloc:
            return "external", "", "external resource; availability not checked"
        path = unquote(url.path)
        fragment = unquote(url.fragment)
        path = re.sub(r"\\([() ])", r"\1", path)
        if path.startswith(MACHINE):
            return "machine_local", path, "requires original machine or a rerun"
        if not path:
            if not fragment:
                return "invalid_target", ref.source, "empty link destination"
            candidate = ref.source
        else:
            candidate = self.resolve_path(ref, path)
        if candidate == ".." or candidate.startswith("../"):
            return "outside_repository", candidate, "target resolves beyond the Git root"
        if candidate in self.entries or candidate == ".":
            if fragment:
                return self.check_anchor(candidate, fragment)
            return "tracked", candidate, ""
        if moved := self.moved_to(candidate):
            return "moved", moved, f"Git rename from {candidate}"
        if removed := self.removed.get(candidate):
            return "removed_from_tree", candidate, f"removed in {removed}; inspect {removed}^"
        if self.generated_path(candidate):
            return "untracked_run_output", candidate, "historical output path absent from committed tree; rerun for new evidence"
        if ref.kind == "bare_path" and ("..." in path.split("/") or any(char in path for char in "*{}")):
            return "template_path", candidate, "path pattern requires expansion before verification"
        return "missing", candidate, "no committed target or recorded rename"

    def check_anchor(self, path: str, fragment: str) -> tuple[str, str, str]:
        if self.entries.get(path) == "blob":
            if match := LINE_ANCHOR.fullmatch(fragment):
                first = int(match[1])
                last = int(match[2] or first)
                if first <= last <= len(self.read(path).splitlines()):
                    return "anchor", path, fragment
                return "missing_anchor", path, fragment
            if path.lower().endswith(".md"):
                status = "anchor" if fragment in heading_anchors(self.read(path)) else "missing_anchor"
                return status, path, fragment
        return "anchor_unchecked", path, fragment


def audit(snapshot: GitSnapshot) -> list[Finding]:
    findings = []
    for source in snapshot.documents():
        for ref in references(source, snapshot.read(source)):
            status, resolved, note = snapshot.classify(ref)
            findings.append(Finding(snapshot.commit, ref.source, ref.line, ref.kind,
                                    ref.target, status, resolved, note))
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="scan Markdown under this Git directory")
    parser.add_argument("--ref", default="HEAD", help="commit or ref to scan")
    parser.add_argument("--report", required=True, type=Path, help="output CSV path")
    args = parser.parse_args()
    try:
        snapshot = GitSnapshot(args.root.resolve(strict=True), args.ref)
        findings = audit(snapshot)
        with args.report.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(Finding.__dataclass_fields__), lineterminator="\n")
            writer.writeheader()
            writer.writerows(map(asdict, findings))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Evidence scan failed: {error}\n")
    counts = Counter(finding.status for finding in findings)
    print(f"{snapshot.commit}: {len(snapshot.documents())} Markdown files, {len(findings)} references")
    print(", ".join(f"{status}={count}" for status, count in sorted(counts.items())))


if __name__ == "__main__":
    main()
