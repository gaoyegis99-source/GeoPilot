"""Load local, versioned method definitions independently of tool discovery."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from stat import S_ISDIR, S_ISREG

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from geopilot.methods.models import MethodDefinition, MethodRef
from geopilot.methods.release_ledger import LEDGER_NAME, ReleaseError, ReleaseState, ledger_state, read_ledger


@dataclass(frozen=True)
class MethodLoadIssue:
    path: Path
    field: str
    message: str


class MethodCatalog:
    """A snapshot of method definitions and any errors found while loading them."""

    def __init__(
        self, methods: list[MethodDefinition], issues: tuple[MethodLoadIssue, ...],
        *, disabled: frozenset[MethodRef] = frozenset(),
    ) -> None:
        self._methods: dict[str, dict[int, MethodDefinition]] = {}
        for method in methods:
            self._methods.setdefault(method.id, {})[method.version] = method
        self._issues = issues
        self._disabled = frozenset((ref.id, ref.version) for ref in disabled)

    @property
    def issues(self) -> tuple[MethodLoadIssue, ...]:
        return self._issues

    def get(self, method_id: str, version: int | None = None) -> MethodDefinition:
        """Return a copy of the exact version, or the highest accepted version."""
        versions = self._methods[method_id]
        if version is None:
            version = max(versions)
        elif type(version) is not int:
            raise KeyError((method_id, version))
        return versions[version].model_copy(deep=True)

    def list(self) -> list[MethodDefinition]:
        """Return copies of every accepted definition in ID and version order."""
        return self._list(frozenset())

    def get_enabled(self, method_id: str, version: int | None = None) -> MethodDefinition:
        """Return an enabled version for a new draft, suggestion or confirmation."""
        if version is None:
            version = max(
                (entry for entry in self._methods[method_id] if (method_id, entry) not in self._disabled),
                default=None,
            )
        if version is None or (method_id, version) in self._disabled:
            raise KeyError((method_id, version))
        return self.get(method_id, version)

    def list_enabled(self) -> list[MethodDefinition]:
        """List methods available for new selections."""
        return self._list(self._disabled)

    def _list(self, excluded: frozenset[tuple[str, int]]) -> list[MethodDefinition]:
        return [
            versions[version].model_copy(deep=True)
            for method_id, versions in sorted(self._methods.items())
            for version in sorted(versions)
            if (method_id, version) not in excluded
        ]


class _UniqueKeySafeLoader(yaml.SafeLoader):
    def construct_object(self, node, deep=False):
        try:
            return super().construct_object(node, deep=deep)
        except (AttributeError, IndexError, KeyError, OverflowError, ValueError) as error:
            # Some invalid scalar tags raise native exceptions in SafeLoader.
            tag = node.tag.removeprefix("tag:yaml.org,2002:")
            raise ConstructorError(None, None, f"invalid value for YAML tag {tag!r}", node.start_mark) from error

    def flatten_mapping(self, node):
        # Merge keys are removed by SafeLoader, so check their uniqueness first.
        merge_keys = [key for key, _ in node.value if key.tag == "tag:yaml.org,2002:merge"]
        if len(merge_keys) > 1:
            raise ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found duplicate key '<<'", merge_keys[1].start_mark,
            )
        super().flatten_mapping(node)

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, MappingNode):
            return super().construct_mapping(node, deep=deep)

        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, Hashable):
                raise ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "found unhashable key", key_node.start_mark,
                )
            if key in mapping:
                raise ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key {key!r}", key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def parse_method_yaml(content: str) -> MethodDefinition:
    return MethodDefinition.model_validate(yaml.load(content, Loader=_UniqueKeySafeLoader))


def released_methods(state: ReleaseState) -> list[MethodDefinition]:
    methods = []
    for ref, entry in state.published.items():
        definition = parse_method_yaml(entry.candidate_yaml)
        if definition.ref != ref:
            raise ReleaseError("METHOD_REFERENCE_MISMATCH", "method", "The stored definition and event differ")
        methods.append(definition)
    return methods


def load_method_catalog(directory: str | PathLike[str], *, include_releases: bool = True) -> MethodCatalog:
    """Load local YAML definitions and an atomic snapshot of the release ledger."""
    directory = Path(directory)
    candidates: dict[tuple[str, int], list[tuple[Path, MethodDefinition]]] = defaultdict(list)
    issues: list[MethodLoadIssue] = []
    for path in sorted(directory.iterdir()):
        if path.suffix not in {".yaml", ".yml"}:
            continue
        try:
            mode = path.stat().st_mode
            if S_ISDIR(mode):
                continue
            if not S_ISREG(mode):
                raise OSError("Expected a regular method configuration file")
            method = parse_method_yaml(path.read_text(encoding="utf-8"))
        except ValidationError as error:
            for detail in error.errors(include_url=False, include_context=False, include_input=False):
                field = ".".join(map(str, detail["loc"])) or "$"
                issues.append(MethodLoadIssue(path, field, detail["msg"]))
        except (OSError, ValueError, RecursionError, yaml.YAMLError) as error:
            issues.append(MethodLoadIssue(path, "$", str(error)))
        else:
            candidates[method.id, method.version].append((path, method))

    methods = []
    for (method_id, version), definitions in candidates.items():
        if len(definitions) == 1:
            methods.append(definitions[0][1])
            continue
        locations = ", ".join(str(path) for path, _ in definitions)
        message = f"Method {method_id!r} version {version} is defined in multiple files: {locations}"
        issues.extend(MethodLoadIssue(path, "version", message) for path, _ in definitions)

    disabled = frozenset()
    if include_releases and not issues:
        try:
            state = ledger_state(read_ledger(directory), {method.ref: method for method in methods})
            released = released_methods(state)
        except (ValueError, RecursionError, yaml.YAMLError) as error:
            issues.append(MethodLoadIssue(directory / LEDGER_NAME, getattr(error, "field", "$"), str(error)))
        else:
            methods.extend(released)
            disabled = state.disabled
    return MethodCatalog(methods, tuple(sorted(issues, key=lambda issue: issue.path)), disabled=disabled)
