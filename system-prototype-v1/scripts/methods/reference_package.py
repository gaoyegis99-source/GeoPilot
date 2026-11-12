"""Read an immutable P14 fixture and compare its independent expected results."""

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
import pandas as pd
import pyogrio
import rasterio
import shapely
from pydantic import BaseModel, ConfigDict, Field, JsonValue, PrivateAttr, StringConstraints, model_validator
from pyproj import CRS
from pyproj.exceptions import CRSError
from rasterio.errors import RasterioError
from shapely.errors import GEOSException

from geopilot.data.fingerprint import capture_file_state
from geopilot.methods.models import MethodRef


_Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
_Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_EPSG = Annotated[str, StringConstraints(pattern=r"^EPSG:[1-9][0-9]*$")]
_Number = int | float
_Scalar = str | int | float | bool | None
_Absolute = Annotated[float, Field(ge=0)]
_Positive = Annotated[int, Field(ge=1)]
_PixelType = Literal["uint8", "uint16", "int16", "uint32", "int32", "float32", "float64"]
_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_INPUT_SUFFIXES = {".gpkg", ".geojson", ".tif", ".tiff"}
_OUTPUT_SUFFIXES = {"vector": {".gpkg", ".geojson"}, "raster": {".tif", ".tiff"}, "json": {".json"}}
_MAX_INLINE_PIXELS = 2_000_000


class ReferencePackageError(ValueError):
    def __init__(self, message: str, *, code: str = "INVALID_REFERENCE_PACKAGE", field: str = "$"):
        super().__init__(message)
        self.code = code
        self.field = field


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False)


class _HashedFile(_Record):
    path: _Text
    sha256: _SHA256


class InputFile(_HashedFile):
    layer: _Text | None = None


@dataclass(frozen=True, slots=True)
class OutputFile:
    path: str | Path
    layer: str | None = None


class _ExpectedFailure(_Record):
    phase: Literal["draft", "confirm"]
    code: _Text


class FailureCase(_Record):
    id: _Identifier
    inputs: dict[_Identifier, InputFile] = Field(default_factory=dict)
    critical_options: dict[_Text, JsonValue] = Field(default_factory=dict)
    expected: _ExpectedFailure


class _ExpectedOutput(_Record):
    kind: Literal["vector", "raster", "json"]
    reference: _HashedFile
    basis: _Text


class ReferencePackage(_Record):
    schema_version: Annotated[int, Field(ge=1, le=1)]
    case_id: _Identifier
    method: MethodRef
    environment: Annotated[dict[_Text, _Text], Field(min_length=1)]
    inputs: Annotated[dict[_Identifier, InputFile], Field(min_length=1)]
    critical_options: dict[_Text, JsonValue]
    outputs: Annotated[dict[_Identifier, _ExpectedOutput], Field(min_length=1)]
    failure_cases: list[FailureCase] = Field(default_factory=list)
    _root: Path = PrivateAttr()
    _files: dict[Path, str] = PrivateAttr(default_factory=dict)
    _references: dict[str, "_VectorReference | _RasterReference | _JsonReference"] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def valid_failures(self):
        ids = {self.case_id}
        for failure in self.failure_cases:
            if failure.id in ids or not failure.inputs.keys() <= self.inputs.keys():
                raise ValueError("Failure cases require distinct IDs and may override only existing input roles")
            if not (failure.inputs or failure.critical_options):
                raise ValueError("Each failure case must override at least one input or option")
            ids.add(failure.id)
        return self

    @property
    def root(self) -> Path:
        return self._root

    def _failure(self, case_id: str | None) -> FailureCase | None:
        if case_id is None or case_id == self.case_id:
            return None
        for case in self.failure_cases:
            if case.id == case_id:
                return case
        raise ReferencePackageError("Unknown failure case ID", field="failure_cases")

    def case_inputs(self, case_id: str | None = None) -> dict[str, InputFile]:
        case = self._failure(case_id)
        return {**self.inputs, **(case.inputs if case else {})}

    def case_options(self, case_id: str | None = None) -> dict[str, JsonValue]:
        case = self._failure(case_id)
        return deepcopy({**self.critical_options, **(case.critical_options if case else {})})

    def resolve_input(self, role: str, case_id: str | None = None) -> Path:
        inputs = self.case_inputs(case_id)
        if role not in inputs:
            raise ReferencePackageError("Unknown fixture input role", field=f"inputs.{role}")
        return _checked_file(self.root, inputs[role], _INPUT_SUFFIXES, f"inputs.{role}")


class _VectorFeature(_Record):
    wkt: _Text
    properties: dict[_Text, _Scalar]


class _VectorReference(_Record):
    crs: _EPSG
    id_field: _Text
    attribute_fields: list[_Text]
    features: dict[_Text, _VectorFeature]

    @model_validator(mode="after")
    def fields_and_topology(self):
        fields = set(self.attribute_fields)
        if len(fields) != len(self.attribute_fields) or self.id_field in fields:
            raise ValueError("Expected distinct attribute fields excluding the ID")
        for row in self.features.values():
            geometry = shapely.from_wkt(row.wkt)
            if not _valid_2d(geometry) or row.properties.keys() != fields:
                raise ValueError("Expected valid nonempty 2D WKT and exact reference attributes")
        return self


class _RasterReference(_Record):
    crs: _EPSG
    transform: Annotated[list[_Number], Field(min_length=6, max_length=6)]
    resolution: Annotated[list[Annotated[float, Field(gt=0)]], Field(min_length=2, max_length=2)]
    width: _Positive
    height: _Positive
    dtypes: Annotated[list[_PixelType], Field(min_length=1)]
    nodata: list[_Number | Literal["nan"] | None]
    pixels: list[list[list[_Number | None]]]
    absolute_tolerance: _Absolute
    tolerance_basis: _Text

    @model_validator(mode="after")
    def grid_shape(self):
        a, b, _, d, e, _ = self.transform
        if a * e == b * d or self.resolution != [math.hypot(a, d), math.hypot(b, e)]:
            raise ValueError("The transform is degenerate or contradicts the resolution")
        if len(self.pixels) != len(self.dtypes) or len(self.nodata) != len(self.dtypes):
            raise ValueError("Pixel, dtype and NoData band counts must agree")
        if self.width * self.height * len(self.dtypes) > _MAX_INLINE_PIXELS:
            raise ValueError("Version 1 inline raster references are limited to 2,000,000 cells across all bands")
        if any(len(band) != self.height or any(len(row) != self.width for row in band) for band in self.pixels):
            raise ValueError("Reference pixel rows must match the exact declared grid")
        return self


class _NumericTolerance(_Record):
    absolute: _Absolute
    basis: _Text


class _JsonReference(_Record):
    value: JsonValue
    numeric_tolerances: dict[str, _NumericTolerance] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_pointers(self):
        numbers = {pointer for pointer, value in _json_nodes(self.value) if type(value) in (int, float)}
        if not self.numeric_tolerances.keys() <= numbers:
            raise ValueError("Each numeric tolerance must identify an existing reference number by JSON Pointer")
        return self


def _valid_2d(geometry) -> bool:
    return geometry is not None and not geometry.is_empty and geometry.is_valid and not geometry.has_z and not geometry.has_m


def _pointer(parent: str, key: str | int) -> str:
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _json_nodes(value, path=""):
    yield path, value
    if isinstance(value, dict):
        for key, member in value.items():
            yield from _json_nodes(member, _pointer(path, key))
    elif isinstance(value, list):
        for index, member in enumerate(value):
            yield from _json_nodes(member, _pointer(path, index))


def _safe_file(root: Path, name: str | Path, suffixes: set[str], field: str) -> Path:
    relative = os.fspath(name)
    parts = relative.split("/")
    if any(part in {"", ".", ".."} or not _COMPONENT.fullmatch(part) for part in parts):
        raise ReferencePackageError("Expected a canonical project-relative path", code="UNSAFE_PATH", field=field)
    path = root
    for part in parts:
        path /= part
        if path.is_symlink():
            raise ReferencePackageError("Symlinks are forbidden in a fixture path", code="UNSAFE_PATH", field=field)
    if not path.is_file() or path.suffix.lower() not in suffixes or not path.resolve(strict=True).is_relative_to(root):
        raise ReferencePackageError("Expected an existing file of the declared type inside the root", code="UNSAFE_PATH", field=field)
    return path


def _state(path: Path):
    parts = capture_file_state(path)
    if len(parts) != 1:
        raise ReferencePackageError("Version 1 uses self-contained files without sidecars", code="UNSAFE_PATH")
    if path.suffix.lower() == ".gpkg":
        shm = path.with_name(path.name + "-shm")
        with path.open("rb") as stream:
            header = stream.read(20)
        if not header.startswith(b"SQLite format 3\0") or header[18:20] != b"\x01\x01" or shm.exists() or shm.is_symlink():
            raise ReferencePackageError("Use a quiet rollback-journal GeoPackage without sidecars", code="UNSAFE_PATH")
    return parts


def _sha256(path: Path) -> str:
    before = _state(path)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if before != _state(path):
        raise ReferencePackageError("The file changed during fingerprinting", code="FILE_INTEGRITY_MISMATCH")
    return digest


def _checked_file(root: Path, item: _HashedFile, suffixes: set[str], field: str) -> Path:
    path = _safe_file(root, item.path, suffixes, field)
    try:
        digest = _sha256(path)
    except OSError as error:
        raise ReferencePackageError(str(error), code="FILE_INTEGRITY_MISMATCH", field=field) from error
    if digest != item.sha256:
        raise ReferencePackageError("SHA-256 differs from the reference package", code="FILE_INTEGRITY_MISMATCH", field=field)
    return path


def _read_json(path: Path):
    def unique(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError(f"Duplicate JSON key {key!r}")
            result[key] = value
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("JSON numbers must be finite")
        return number

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique, parse_float=finite, parse_constant=finite)


def load_reference_package(manifest_path: str | Path) -> ReferencePackage:
    """Validate package structure, declared references and stable input fingerprints."""
    try:
        requested = Path(manifest_path)
        root = requested.parent.resolve(strict=True)
        path = _safe_file(root, requested.name, {".json"}, "manifest")
        digest = _sha256(path)
        package = ReferencePackage.model_validate(_read_json(path))
        package._root = root
        package._files[path] = digest
        for role, item in package.inputs.items():
            source = package.resolve_input(role)
            package._files[source] = item.sha256
        for case in package.failure_cases:
            for role, item in case.inputs.items():
                source = package.resolve_input(role, case.id)
                package._files[source] = item.sha256
        models = {"vector": _VectorReference, "raster": _RasterReference, "json": _JsonReference}
        for role, output in package.outputs.items():
            expected = _checked_file(root, output.reference, {".json"}, f"outputs.{role}.reference")
            reference = models[output.kind].model_validate(_read_json(expected))
            if isinstance(reference, (_VectorReference, _RasterReference)):
                CRS.from_user_input(reference.crs)
            package._files[expected] = output.reference.sha256
            package._references[role] = reference
        _assert_package_intact(package)
        return package
    except ReferencePackageError:
        raise
    except (OSError, ValueError, TypeError, OverflowError, CRSError, GEOSException, RecursionError) as error:
        raise ReferencePackageError(str(error)) from error


def _assert_package_intact(package: ReferencePackage) -> None:
    for path, digest in package._files.items():
        relative = path.relative_to(package.root).as_posix()
        _checked_file(package.root, _HashedFile(path=relative, sha256=digest), {path.suffix.lower()}, relative)


def _json_difference(expected, observed, tolerances, path=""):
    if path in tolerances:
        if type(observed) not in (int, float) or abs(observed - expected) > tolerances[path].absolute:
            return path or "$"
    elif type(expected) is not type(observed):
        return path or "$"
    elif isinstance(expected, dict):
        if expected.keys() != observed.keys():
            return path or "$"
        for key in sorted(expected):
            if difference := _json_difference(expected[key], observed[key], tolerances, _pointer(path, key)):
                return difference
    elif isinstance(expected, list):
        if len(expected) != len(observed):
            return path or "$"
        for index, (left, right) in enumerate(zip(expected, observed, strict=True)):
            if difference := _json_difference(left, right, tolerances, _pointer(path, index)):
                return difference
    elif expected != observed:
        return path or "$"
    return None


def _mismatch(message: str) -> None:
    raise ReferencePackageError(message, code="OUTPUT_MISMATCH")


def _compare_vector(path: Path, layer: str | None, expected: _VectorReference) -> dict:
    if layer is None and len(pyogrio.list_layers(path)) != 1:
        _mismatch("Select an explicit layer for a multi-layer vector output")
    info = pyogrio.read_info(path, layer=layer, force_feature_count=True)
    driver = "GPKG" if path.suffix.lower() == ".gpkg" else "GeoJSON"
    if info["driver"] != driver or info["geometry_type"] is None:
        _mismatch("Expected a spatial GeoPackage or GeoJSON layer")
    if not info["crs"] or not CRS.from_user_input(info["crs"]).equals(CRS.from_user_input(expected.crs)):
        _mismatch("Vector CRS differs from the independent reference")
    if set(info["fields"]) != {expected.id_field, *expected.attribute_fields}:
        _mismatch("Vector ID and attribute fields differ")
    if info["features"] != len(expected.features):
        _mismatch("Vector feature count differs")
    frame = pyogrio.read_dataframe(path, layer=layer)
    ids = frame[expected.id_field].tolist()
    if any(not isinstance(identifier, str) or not identifier for identifier in ids):
        _mismatch("Observed feature IDs must be nonempty strings")
    if len(ids) != len(set(ids)) or set(ids) != expected.features.keys():
        _mismatch("Vector IDs are duplicated or differ from the independent reference")
    attributes = frame[expected.attribute_fields].to_dict(orient="records")
    for identifier, geometry, record in zip(ids, frame.geometry, attributes, strict=True):
        row = expected.features[identifier]
        if not _valid_2d(geometry) or not geometry.equals(shapely.from_wkt(row.wkt)):
            _mismatch(f"Vector topology differs for ID {identifier}")
        cleaned = {key: None if pd.isna(value) else value for key, value in record.items()}
        if pointer := _json_difference(row.properties, cleaned, {}):
            _mismatch(f"Vector attributes differ for ID {identifier} at {pointer}")
    return {"feature_count": len(ids), "crs": expected.crs}


def _compare_raster(path: Path, expected: _RasterReference) -> dict:
    with rasterio.Env(GDAL_PAM_ENABLED=False), rasterio.open(path) as dataset:
        nodata = ["nan" if value is not None and math.isnan(value) else value for value in dataset.nodatavals]
        if (
            dataset.driver != "GTiff" or dataset.crs is None
            or not CRS.from_user_input(dataset.crs).equals(CRS.from_user_input(expected.crs))
            or list(dataset.transform[:6]) != expected.transform or list(dataset.res) != expected.resolution
            or (dataset.width, dataset.height, dataset.count) != (expected.width, expected.height, len(expected.dtypes))
            or list(dataset.dtypes) != expected.dtypes or nodata != expected.nodata
        ):
            _mismatch("Raster format, CRS, transform, resolution, size, dtype or NoData differs")
        actual = dataset.read(masked=True)
        pixels = np.asarray(expected.pixels, dtype="float64")
        expected_mask = np.isnan(pixels)
        if not np.array_equal(np.ma.getmaskarray(actual), expected_mask):
            _mismatch("Raster pixel mask differs")
        if not np.allclose(actual.data[~expected_mask], pixels[~expected_mask], rtol=0, atol=expected.absolute_tolerance):
            _mismatch("Raster unmasked pixel values differ")
    return {"valid_pixels": int((~expected_mask).sum()), "masked_pixels": int(expected_mask.sum()), "crs": expected.crs}


def compare_outputs(package: ReferencePackage, outputs: Mapping[str, OutputFile], *, workspace: Path) -> dict:
    """Compare regular workspace outputs, leaving fixtures and outputs unchanged."""
    _assert_package_intact(package)
    try:
        root = Path(workspace).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Expected an existing workspace directory")
    except (OSError, ValueError) as error:
        raise ReferencePackageError(str(error), code="UNSAFE_PATH", field="workspace") from error
    report = {"schema_version": 1, "case_id": package.case_id, "status": "PASS", "outputs": {}, "issues": []}
    if outputs.keys() != package.outputs.keys():
        report["issues"].append({
            "code": "OUTPUT_ROLE_SET_MISMATCH", "field": "outputs",
            "message": f"Missing {sorted(package.outputs.keys() - outputs.keys())}; unexpected {sorted(outputs.keys() - package.outputs.keys())}",
        })
    for role in sorted(package.outputs.keys() & outputs.keys()):
        expected = package.outputs[role]
        observed = outputs[role]
        field = f"outputs.{role}"
        try:
            if not isinstance(observed, OutputFile):
                raise ReferencePackageError("Expected an OutputFile mapping value", code="UNSAFE_PATH", field=field)
            path = _safe_file(root, observed.path, _OUTPUT_SUFFIXES[expected.kind], field)
            if path.is_relative_to(package.root) or any(path.samefile(source) for source in package._files):
                raise ReferencePackageError("Output aliases a reference package file", code="UNSAFE_PATH", field=field)
            if observed.layer is not None and expected.kind != "vector":
                raise ReferencePackageError("Only a vector result can name a layer", code="UNSAFE_PATH", field=field)
            before = _state(path)
            reference = package._references[role]
            if isinstance(reference, _VectorReference):
                details = _compare_vector(path, observed.layer, reference)
            elif isinstance(reference, _RasterReference):
                details = _compare_raster(path, reference)
            else:
                if pointer := _json_difference(reference.value, _read_json(path), reference.numeric_tolerances):
                    _mismatch(f"JSON differs at {pointer}")
                details = {}
            digest = _sha256(path)
            if before != _state(path):
                raise ReferencePackageError("Output changed while comparing", code="FILE_INTEGRITY_MISMATCH", field=field)
            report["outputs"][role] = {"status": "PASS", "kind": expected.kind, "sha256": digest, "checks": details}
        except ReferencePackageError as error:
            report["issues"].append({"code": error.code, "field": field, "message": str(error)})
            report["outputs"][role] = {"status": "FAIL", "kind": expected.kind}
        except (OSError, ValueError, TypeError, OverflowError, RuntimeError, RasterioError, CRSError, GEOSException) as error:
            report["issues"].append({"code": "OUTPUT_READ_ERROR", "field": field, "message": str(error)})
            report["outputs"][role] = {"status": "FAIL", "kind": expected.kind}
    _assert_package_intact(package)
    if report["issues"]:
        report["status"] = "FAIL"
    return report
