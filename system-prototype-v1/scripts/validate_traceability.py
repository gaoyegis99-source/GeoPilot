"""Check the forty V2.0 chapter 3 entries and their local traceability links."""

import re
from collections import Counter
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_MATRIX = _ROOT / "docs" / "traceability" / "requirements-matrix.md"
_GROUPS = ("DATA", "CTX", "MTH", "TOOL", "PLAN", "EXEC", "CHECK", "ART", "UI", "MODEL")
_EXPECTED = {f"{group}-{number:03}" for group in _GROUPS for number in range(1, 5)}
_STATUSES = ("PASS", "PARTIAL", "NOT_IMPLEMENTED")
_HEADER = "| 需求 ID | V2.0 验收目标 | 状态 | 实现 | 测试证据 | 缺口 |"
_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _check_links(cell: str, directory: Path, *, implementation: bool) -> list[str]:
    targets = _LINK.findall(cell)
    if not targets:
        return ["requires a local Markdown link"]

    problems = []
    line_links = 0
    for target in targets:
        path, _, fragment = target.partition("#")
        location = (directory / path).resolve()
        if not location.is_relative_to(_ROOT) or not location.is_file():
            problems.append(f"unresolved local link {target}")
        elif match := re.fullmatch(r"L([1-9][0-9]*)", fragment):
            with location.open(encoding="utf-8") as source:
                if int(match.group(1)) > sum(1 for _ in source):
                    problems.append(f"line outside file {target}")
                else:
                    line_links += 1
    if implementation and not line_links:
        problems.append("requires a code link with a #L line anchor")
    return problems


def validate_matrix(content: str, *, directory: Path = _MATRIX.parent) -> tuple[Counter[str], list[str]]:
    """Return status totals and all structural or broken-link errors."""
    lines = content.splitlines()
    if _HEADER not in lines:
        return Counter(), ["missing requirements table header"]
    seen: set[str] = set()
    statuses: Counter[str] = Counter()
    problems = []
    start = lines.index(_HEADER) + 2  # Skip the header and its Markdown divider.
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        identifier = cells[0]
        if identifier in seen:
            problems.append(f"{identifier}: duplicate requirement")
        seen.add(identifier)
        if identifier not in _EXPECTED:
            problems.append(f"{identifier}: unexpected requirement")
        if len(cells) != 6 or not all(cells):
            problems.append(f"{identifier}: requires six nonempty columns")
            continue
        _, _, status, implementation, evidence, gap = cells
        if status not in _STATUSES:
            problems.append(f"{identifier}: unknown status {status}")
        else:
            statuses[status] += 1
        if (status == "PASS") != (gap == "—"):
            problems.append(f"{identifier}: PASS requires — and incomplete entries require a described gap")
        for label, cell in (("implementation", implementation), ("evidence", evidence)):
            for problem in _check_links(cell, directory, implementation=label == "implementation"):
                problems.append(f"{identifier} {label}: {problem}")
    missing = sorted(_EXPECTED - seen)
    if missing:
        problems.append(f"missing requirements: {', '.join(missing)}")
    return statuses, problems


def main() -> int:
    try:
        content = _MATRIX.read_text(encoding="utf-8")
    except OSError as error:
        print(f"Traceability failed: {error}")
        return 1
    statuses, problems = validate_matrix(content)
    if problems:
        for problem in problems:
            print(problem)
        return 1
    counts = ", ".join(f"{status}={statuses[status]}" for status in _STATUSES)
    print(f"Traceability verified: {sum(statuses.values())}/{len(_EXPECTED)} requirements ({counts})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
