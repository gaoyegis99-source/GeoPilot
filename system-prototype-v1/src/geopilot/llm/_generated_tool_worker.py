"""Validate and execute one numeric candidate in the restricted container."""

import ast
import json
import math
import os
import re
import resource
import sys
from pathlib import Path


MAX_SOURCE_BYTES = 4096
MAX_AST_NODES = 256
MAX_VALUES = 4096
MAX_OUTPUT_BYTES = 1024
FUNCTION_NAME = "count_threshold"
ARGUMENTS = ("values", "threshold")
FIELDS = frozenset({"valid_count", "selected_count"})
CONTAINER_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C.UTF-8", "TZ": "UTC"}
CONTAINER_UID = 65534
MAX_OPEN_FILES = 64
_LOCAL_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_NODES = frozenset({
    ast.Assign, ast.AugAssign, ast.Return, ast.If, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.List, ast.Dict,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.UnaryOp, ast.UAdd, ast.USub, ast.Not, ast.BoolOp, ast.And, ast.Or,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
})


class SourceRejected(ValueError):
    def __init__(self, message, line=None):
        super().__init__(message)
        self.line = line


def _reject(node, message):
    raise SourceRejected(message, getattr(node, "lineno", None))


def validate_source(source: bytes) -> ast.Module:
    """Allow one function and expressions over numeric data and local variables."""
    if not 0 < len(source) <= MAX_SOURCE_BYTES:
        raise SourceRejected(f"Source must contain 1–{MAX_SOURCE_BYTES} UTF-8 bytes")
    try:
        tree = ast.parse(source.decode("utf-8"), feature_version=(3, 12))
    except (UnicodeError, ValueError, SyntaxError, RecursionError) as error:
        raise SourceRejected("Invalid UTF-8 Python function syntax", getattr(error, "lineno", None)) from None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise SourceRejected(f"Supply exactly one function named {FUNCTION_NAME}")
    function = tree.body[0]
    args = function.args
    if (
        function.name != FUNCTION_NAME or function.decorator_list or function.returns is not None
        or getattr(function, "type_params", ()) or args.posonlyargs or args.kwonlyargs
        or args.vararg or args.kwarg or args.defaults or tuple(arg.arg for arg in args.args) != ARGUMENTS
        or any(arg.annotation is not None for arg in args.args)
    ):
        _reject(function, f"Use exactly def {FUNCTION_NAME}(values, threshold)")

    nodes = [node for statement in function.body for node in ast.walk(statement)]
    if len(nodes) + 5 > MAX_AST_NODES:
        _reject(function, f"Source must use at most {MAX_AST_NODES} AST nodes")
    locals_ = {*ARGUMENTS, *(node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))}
    keys = set()
    for node in nodes:
        if isinstance(node, ast.Dict):
            names = [key.value for key in node.keys if isinstance(key, ast.Constant) and type(key.value) is str]
            if len(names) != len(node.keys) or not set(names) <= FIELDS or len(names) != len(set(names)):
                _reject(node, "Dictionaries may use each result field at most once")
            keys.update(id(key) for key in node.keys)
    for node in nodes:
        if type(node) not in _NODES:
            _reject(node, f"Disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and (
            not _LOCAL_NAME.fullmatch(node.id) or node.id not in locals_
            or (isinstance(node.ctx, ast.Store) and node.id in ARGUMENTS)
        ):
            _reject(node, "Use only local variables and read-only function parameters")
        if isinstance(node, ast.Constant) and id(node) not in keys:
            value = node.value
            if type(value) is not bool and (
                type(value) not in (int, float) or abs(value) > 1_000_000_000 or not math.isfinite(value)
            ):
                _reject(node, "Use only finite numeric or Boolean literals")
    try:
        compile(tree, "<candidate>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError) as error:
        raise SourceRejected("Invalid function control flow", getattr(error, "lineno", None)) from None
    return tree


def _numeric(values):
    return all(type(value) in (int, float) and abs(value) <= 1_000_000_000 and math.isfinite(value) for value in values)


def valid_result(result, maximum: int) -> bool:
    return (
        type(result) is dict and result.keys() == FIELDS
        and all(type(value) is int for value in result.values())
        and 0 <= result["selected_count"] <= result["valid_count"] <= maximum
    )


def _runtime():
    status = {}
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        field, separator, value = line.partition(":")
        if separator and field in {"CapEff", "NoNewPrivs", "Seccomp"}:
            status[field] = value.strip()
    return {
        "python": ".".join(map(str, sys.version_info[:3])),
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "effective_capabilities": int(status["CapEff"], 16),
        "no_new_privileges": status["NoNewPrivs"] == "1",
        "seccomp_filter": status["Seccomp"] == "2",
        "clean_environment": dict(os.environ) == CONTAINER_ENV,
        "open_files": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
        "read_only_root": bool(os.statvfs("/").f_flag & os.ST_RDONLY),
        "read_only_stage": bool(os.statvfs("/sandbox").f_flag & os.ST_RDONLY),
    }


def runtime_matches(runtime) -> bool:
    expected = {
        "uid": CONTAINER_UID, "gid": CONTAINER_UID, "effective_capabilities": 0, "no_new_privileges": True,
        "seccomp_filter": True, "clean_environment": True, "open_files": [MAX_OPEN_FILES, MAX_OPEN_FILES],
        "read_only_root": True, "read_only_stage": True,
    }
    return (
        type(runtime) is dict and runtime.keys() == expected.keys() | {"python"}
        and type(runtime["python"]) is str and re.fullmatch(r"3\.12\.[0-9]+", runtime["python"]) is not None
        and all(type(runtime[key]) is type(value) and runtime[key] == value for key, value in expected.items())
    )


def main() -> int:
    stage = Path("/sandbox")
    try:
        runtime = _runtime()
        if not runtime_matches(runtime):
            return 4
        tree = validate_source((stage / "candidate.py").read_bytes())
        payload = json.loads((stage / "sample.json").read_bytes())
        values, thresholds = payload["values"], payload["thresholds"]
        if not (
            type(values) is list and type(thresholds) is list and 0 < len(values) <= MAX_VALUES
            and 0 < len(thresholds) <= 8 and _numeric(values) and _numeric(thresholds)
        ):
            return 4
    except (OSError, TypeError, ValueError, KeyError, OverflowError):
        return 4

    scope = {"__builtins__": {}}
    results = []
    try:
        exec(compile(tree, "<candidate>", "exec", dont_inherit=True), scope)
        for threshold in thresholds:
            result = scope[FUNCTION_NAME](values.copy(), threshold)
            if not valid_result(result, len(values)):
                return 2
            results.append(result)
    except MemoryError:
        return 5
    except (ArithmeticError, LookupError, NameError, TypeError, ValueError, RuntimeError):
        return 3
    print(json.dumps({"runtime": runtime, "results": results}, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
