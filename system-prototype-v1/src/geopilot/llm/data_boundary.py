"""Validate the complete serialized model request at the outbound boundary."""

import json
import math
import re
import unicodedata
from collections.abc import Iterable
from html import unescape
from typing import NoReturn, get_args

from geopilot.application.task_context import InputReadiness
from geopilot.llm.context_builder import (
    _CONNECTION,
    _COORDINATE_LABEL,
    _COORDINATES,
    _FIELD_NAME,
    _FILENAME,
    _GEOMETRY_TYPES,
    _NUMBER,
    _ROLE_NAME,
    _URI,
    _WKT,
    _FieldType,
)


_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}")
_ENCODED_ESCAPE = re.compile(r"%(?:[0-9a-f]{2}|u[0-9a-f]{4})", re.IGNORECASE)
_SLASH_PATH = re.compile(r"/\S")
_DRIVE_PATH = re.compile(r"(?<![\w])[A-Za-z]:\S")
_FILE_EXTENSION = re.compile(
    r"(?<![\w.])[\w.-]+\.(?:gdb|parquet|hdf[45]?|nc|dbf|xml|yaml|yml|jsonl|txt|log|py|bin|las|laz|vrt|pdf|xlsx|gz)(?![\w.])",
    re.IGNORECASE,
)
_EXTENDED_WKT = re.compile(
    r"\b(?:CIRCULARSTRING|COMPOUNDCURVE|CURVEPOLYGON|MULTICURVE|MULTISURFACE|POLYHEDRALSURFACE|TIN|TRIANGLE)"
    r"\s*(?:ZM|Z|M)?\s*(?:\(|EMPTY\b)", re.IGNORECASE,
)
_COORDINATE_TUPLE = re.compile(rf"\(\s*{_NUMBER}(?:[\s;；]+{_NUMBER}){{1,3}}\s*\)")
_DECIMAL = r"[-+]?(?:[0-9]+\.[0-9]+|\.[0-9]+)(?:[eE][-+]?[0-9]+)?"
_DECIMAL_PAIR = re.compile(rf"(?<![\w.]){_DECIMAL}\s+{_DECIMAL}(?![\w.])")
_COORDINATE_VALUES = re.compile(
    rf"(?:\b(?:coordinates?|bbox|bounds)\b|坐标)\s*[:：=]\s*{_NUMBER}(?:[\s,，;；]+{_NUMBER})+",
    re.IGNORECASE,
)
_LOCAL_SCHEME = re.compile(r"\b(?:file|data|postgres(?:ql)?|sqlite|odbc|mysql):(?=\S)", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"\b(?:host|hostaddr|dbname|database|username|password|passwd|pwd|passfile|sslkey|"
    r"api[_-]?key|access[_-]?token|token|secret|source_feature_id|asset_id)\s*[:：=]",
    re.IGNORECASE,
)
_RECORD_TUPLE = re.compile(r"\((?=[^()]*[,，])(?=[^()]*[\"'])[^()]*\)")
_FORBIDDEN_TEXT = (
    _ENCODED_ESCAPE, _SLASH_PATH, _DRIVE_PATH, _URI, _LOCAL_SCHEME, _FILENAME, _FILE_EXTENSION,
    _CONNECTION, _SENSITIVE_ASSIGNMENT, _WKT, _EXTENDED_WKT, _COORDINATES,
    _COORDINATE_LABEL, _COORDINATE_TUPLE, _DECIMAL_PAIR, _COORDINATE_VALUES, _RECORD_TUPLE,
)
_READINESS = frozenset(status.value for status in InputReadiness)
_GEOMETRIES = frozenset(_GEOMETRY_TYPES | {"Other"})
_FIELD_TYPES = frozenset(get_args(_FieldType))
_ASSET_KINDS = frozenset({"vector", "table", "raster"})
_OPTION_TYPES = frozenset({"enum", "number", "integer", "boolean", "string", "json", "reference", "user_defined", "user_selected"})
_BOUNDS = ("minimum", "maximum", "minimum_exclusive", "maximum_exclusive")
_OPTIONAL_CONSTRAINTS = ("values", *_BOUNDS, "unit", "required_for")


class DataBoundaryError(ValueError):
    """An outbound payload failed validation at a known schema location."""

    code = "OUTBOUND_DATA_REJECTED"

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"{self.code} {field}")


def _reject(field: str) -> NoReturn:
    raise DataBoundaryError(field) from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _nonfinite(_literal: str) -> NoReturn:
    raise ValueError


def _finite_float(literal: str) -> float:
    value = float(literal)
    if not math.isfinite(value):
        raise ValueError
    return value


def _parse(serialized: str | bytes) -> object:
    if type(serialized) not in (str, bytes):
        _reject("$")
    try:
        text = serialized.decode("utf-8") if isinstance(serialized, bytes) else serialized
        return json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_nonfinite, parse_float=_finite_float,
        )
    except (ValueError, RecursionError, OverflowError):
        pass
    _reject("$")


def _object(value: object, field: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict[str, object]:
    if not isinstance(value, dict) or set(required) - value.keys() or value.keys() - set(required + optional):
        _reject(field)
    return value


def _items(value: object, field: str) -> Iterable[tuple[str, object]]:
    if not isinstance(value, list):
        _reject(field)
    for index, item in enumerate(value):
        yield f"{field}[{index}]", item


def _integer(value: object, field: str, *, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        _reject(field)


def _number(value: object, field: str) -> int | float:
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        _reject(field)
    return value


def _canonical_text(value: str) -> str:
    return unicodedata.normalize("NFKC", unescape(value))


def _protected(literals: Iterable[str]) -> tuple[str, ...]:
    try:
        values = (literals,) if isinstance(literals, str) else tuple(literals)
        if all(isinstance(value, str) for value in values):
            return tuple(_canonical_text(value).casefold() for value in values if value)
    except Exception:
        pass
    _reject("$")


class _RequestBoundary:
    def __init__(self, protected: tuple[str, ...]) -> None:
        self.protected = protected

    def text(
        self, value: object, field: str, *, pattern: re.Pattern[str] | None = None,
        allowed: frozenset[str] | None = None, maximum: int | None = None,
    ) -> str:
        if not isinstance(value, str) or not value.strip():
            _reject(field)
        if maximum is not None and len(value) > maximum:
            _reject(field)
        if pattern is not None and pattern.fullmatch(value) is None:
            _reject(field)
        if allowed is not None and value not in allowed:
            _reject(field)

        canonical = _canonical_text(value)
        if (any(unicodedata.category(char).startswith("C") for char in value + canonical)
                or any(char in canonical for char in "\\{}[]")
                or any(detector.search(canonical) for detector in _FORBIDDEN_TEXT)):
            _reject(field)
        folded = canonical.casefold()
        if any(literal in folded for literal in self.protected):
            _reject(field)
        return value

    def versioned(self, value: object, field: str) -> None:
        entry = _object(value, field, ("id", "version"))
        self.text(entry["id"], f"{field}.id", pattern=_TOKEN)
        _integer(entry["version"], f"{field}.version", minimum=1)

    def asset(self, value: object, field: str) -> None:
        if not isinstance(value, dict):
            _reject(field)
        kind = self.text(value.get("kind"), f"{field}.kind", allowed=_ASSET_KINDS)
        if kind == "raster":
            asset = _object(value, field, ("kind", "width", "height", "band_count", "resolution_x", "resolution_y"))
            for name in ("width", "height", "band_count"):
                _integer(asset[name], f"{field}.{name}", minimum=1)
            for name in ("resolution_x", "resolution_y"):
                if _number(asset[name], f"{field}.{name}") <= 0:
                    _reject(f"{field}.{name}")
            return

        if kind == "vector":
            asset = _object(value, field, ("kind", "geometry_type", "feature_count", "fields"))
            self.text(asset["geometry_type"], f"{field}.geometry_type", allowed=_GEOMETRIES)
            _integer(asset["feature_count"], f"{field}.feature_count", minimum=0)
        else:
            asset = _object(value, field, ("kind", "row_count", "fields"))
            _integer(asset["row_count"], f"{field}.row_count", minimum=0)
        for location, item in _items(asset["fields"], f"{field}.fields"):
            entry = _object(item, location, ("name", "type"), ("missing_ratio",))
            self.text(entry["name"], f"{location}.name", pattern=_FIELD_NAME)
            self.text(entry["type"], f"{location}.type", allowed=_FIELD_TYPES)
            if "missing_ratio" in entry and not 0 <= _number(entry["missing_ratio"], f"{location}.missing_ratio") <= 1:
                _reject(f"{location}.missing_ratio")

    def context(self, value: object, field: str) -> None:
        context = _object(value, field, ("goal", "roles"))
        self.text(context["goal"], f"{field}.goal", maximum=1200)
        for location, item in _items(context["roles"], f"{field}.roles"):
            role = _object(item, location, ("role", "readiness"), ("asset",))
            self.text(role["role"], f"{location}.role", pattern=_ROLE_NAME)
            self.text(role["readiness"], f"{location}.readiness", allowed=_READINESS)
            if "asset" in role:
                self.asset(role["asset"], f"{location}.asset")

    def option(self, value: object, field: str) -> None:
        option = _object(value, field, ("name", "type", "required"), _OPTIONAL_CONSTRAINTS)
        self.text(option["name"], f"{field}.name", pattern=_TOKEN)
        kind = self.text(option["type"], f"{field}.type", allowed=_OPTION_TYPES)
        if type(option["required"]) is not bool:
            _reject(f"{field}.required")
        if kind == "enum":
            if not option.get("values"):
                _reject(f"{field}.values")
            for location, item in _items(option["values"], f"{field}.values"):
                self.text(item, location, pattern=_TOKEN)
        elif "values" in option:
            _reject(f"{field}.values")
        for name in _BOUNDS:
            if name in option:
                if kind not in {"number", "integer"}:
                    _reject(f"{field}.{name}")
                _number(option[name], f"{field}.{name}")
        if "unit" in option:
            if kind not in {"number", "integer"}:
                _reject(f"{field}.unit")
            self.text(option["unit"], f"{field}.unit", pattern=_TOKEN)
        if "required_for" in option:
            if kind != "reference":
                _reject(f"{field}.required_for")
            self.text(option["required_for"], f"{field}.required_for", pattern=_TOKEN)

    def method(self, value: object, field: str) -> None:
        method = _object(value, field, ("id", "version", "inputs", "options", "steps"))
        self.text(method["id"], f"{field}.id", pattern=_TOKEN)
        _integer(method["version"], f"{field}.version", minimum=1)
        for location, item in _items(method["inputs"], f"{field}.inputs"):
            role = _object(item, location, ("role", "type"))
            self.text(role["role"], f"{location}.role", pattern=_TOKEN)
            self.text(role["type"], f"{location}.type", pattern=_TOKEN)
        for location, item in _items(method["options"], f"{field}.options"):
            self.option(item, location)
        for location, item in _items(method["steps"], f"{field}.steps"):
            step = _object(item, location, ("key", "tool"))
            self.text(step["key"], f"{location}.key", pattern=_TOKEN)
            self.versioned(step["tool"], f"{location}.tool")

    def request(self, value: object) -> None:
        envelope = _object(value, "$", ("payload",))
        payload = _object(envelope["payload"], "$.payload", ("context", "candidate_methods", "candidate_tools"))
        self.context(payload["context"], "$.payload.context")
        for location, item in _items(payload["candidate_methods"], "$.payload.candidate_methods"):
            self.method(item, location)
        for location, item in _items(payload["candidate_tools"], "$.payload.candidate_tools"):
            self.versioned(item, location)


def check_outbound_request(serialized: str | bytes, *, protected_literals: Iterable[str] = ()) -> None:
    """Reject unapproved structure or local data in the final serialized request."""
    document = _parse(serialized)
    _RequestBoundary(_protected(protected_literals)).request(document)
