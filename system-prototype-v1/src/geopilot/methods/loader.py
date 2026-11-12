"""Load local, versioned method definitions independently of tool discovery."""

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

from geopilot.methods.models import MethodDefinition


@dataclass(frozen=True)
class MethodLoadIssue:
    path: Path
    field: str
    message: str


class MethodCatalog:
    """A snapshot of method definitions and any errors found while loading them."""

    def __init__(self, methods: list[MethodDefinition], issues: tuple[MethodLoadIssue, ...]) -> None:
        self._methods: dict[str, dict[int, MethodDefinition]] = {}
        for method in methods:
            self._methods.setdefault(method.id, {})[method.version] = method
        self._issues = issues

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
        return [
            versions[version].model_copy(deep=True)
            for _, versions in sorted(self._methods.items())
            for version in sorted(versions)
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


def load_method_catalog(directory: str | PathLike[str]) -> MethodCatalog:
    """Read the immediate YAML files in a local directory and record file errors."""
    candidates: dict[tuple[str, int], list[tuple[Path, MethodDefinition]]] = defaultdict(list)
    issues: list[MethodLoadIssue] = []
    for path in sorted(Path(directory).iterdir()):
        if path.suffix not in {".yaml", ".yml"}:
            continue
        try:
            mode = path.stat().st_mode
            if S_ISDIR(mode):
                continue
            if not S_ISREG(mode):
                raise OSError("Expected a regular method configuration file")
            document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
            method = MethodDefinition.model_validate(document)
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

    return MethodCatalog(methods, tuple(sorted(issues, key=lambda issue: issue.path)))
