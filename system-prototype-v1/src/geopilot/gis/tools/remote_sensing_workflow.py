"""Run the versioned stages of the two-period NDVI method."""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio

from geopilot.data.fingerprint import capture_file_state, fingerprint_file
from geopilot.domain.step_io import StepFileRef
from geopilot.domain.tool_contract import ToolRef
from geopilot.gis.raster.window_io import _check_destination
from geopilot.gis.tools.tool_command import run_tool_command
from geopilot.methods.land_use_overlay import _stage_outputs
from geopilot.remote_sensing.alignment import align_ndvi
from geopilot.remote_sensing.band_mapper import ProductBandMapping, map_product_bands
from geopilot.remote_sensing.change import write_ndvi_change
from geopilot.remote_sensing.classification import classify_ndvi_change
from geopilot.remote_sensing.config_models import SensorConfigRef, SensorProductConfig, SensorProductKey
from geopilot.remote_sensing.config_registry import SensorConfigLookupError, SensorConfigRegistry
from geopilot.remote_sensing.indices import write_ndvi
from geopilot.remote_sensing.product_metadata import ProductMetadata, parse_product_metadata, read_product_metadata
from geopilot.remote_sensing.quality_mask import write_quality_mask
from geopilot.remote_sensing.scaling import scale_product_reflectance


PROVENANCE_DOMAIN = "GEOPILOT_REMOTE_SENSING"
_PERIODS = ("first", "second")
_PRODUCT_ROLES = ("first_product", "second_product")


@dataclass(frozen=True, slots=True)
class _Product:
    path: Path
    source: dict[str, str]
    mapping: ProductBandMapping
    registry: SensorConfigRegistry


def _plain_path(reference: dict) -> Path:
    if reference.get("layer") is not None:
        raise ValueError("Raster and JSON files cannot select a vector layer")
    return Path(reference["path"])


def _canonical(document: object) -> str:
    return json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _require_match(actual: object, expected: object, field: str) -> None:
    if _canonical(actual) != _canonical(expected):
        raise ValueError(f"{field} disagrees with the source evidence")


def _write_report(outputs: dict, role: str, document: dict) -> None:
    path = _plain_path(outputs[role])
    if path.suffix.lower() != ".json":
        raise ValueError(f"{role} requires a .json output")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _read_report(request: dict, role: str) -> dict:
    document = json.loads(_plain_path(request["inputs"][role]).read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict) or document.keys() != set(_PRODUCT_ROLES)
        or not all(isinstance(record, dict) for record in document.values())
    ):
        raise ValueError(f"inputs.{role} requires evidence for first_product and second_product")
    return document


def _source_record(reference: dict) -> dict[str, str]:
    path = _plain_path(reference)
    relative = StepFileRef.model_validate(reference).path
    return {"path": relative, "fingerprint": fingerprint_file(path).digest}


def _validate_source(source: object) -> dict[str, str]:
    if (
        not isinstance(source, dict) or source.keys() != {"path", "fingerprint"}
        or not isinstance(source["fingerprint"], str) or re.fullmatch(r"[0-9a-f]{64}", source["fingerprint"]) is None
    ):
        raise ValueError("Expected a source path and file group fingerprint")
    StepFileRef(path=source["path"])
    return source


def _identity(metadata: ProductMetadata) -> dict:
    return {
        "product_key": metadata.key.model_dump(mode="json"),
        "product_id": metadata.product_id,
        "granule_id": metadata.granule_id,
    }


def _metadata_record(source: dict[str, str], metadata: ProductMetadata) -> dict:
    return {
        "source": source, **_identity(metadata), "profile": metadata.profile,
        "spacecraft": metadata.spacecraft, "processing_level": metadata.processing_level,
        "metadata_xml": metadata.xml, "band_sources": {code: asdict(band) for code, band in metadata.band_sources.items()},
    }


def _configuration_record(source: dict[str, str], metadata: ProductMetadata, configuration: SensorProductConfig) -> dict:
    return {"source": source, **_identity(metadata), "configuration": configuration.model_dump(mode="json")}


def _mapping_record(product: _Product) -> dict:
    mapping = product.mapping
    return {
        "source": product.source, **_identity(mapping.metadata),
        "configuration": mapping.configuration.ref.model_dump(mode="json"),
        "bands": {role: asdict(band) for role, band in mapping.bands.items()},
    }


def _read_metadata(request: dict, outputs: dict) -> None:
    records = {}
    for role in _PRODUCT_ROLES:
        reference = request["inputs"][role]
        path = _plain_path(reference)
        before = capture_file_state(path)
        metadata = read_product_metadata(path)
        records[role] = _metadata_record(_source_record(reference), metadata)
        if capture_file_state(path) != before:
            raise ValueError(f"inputs.{role} changed while reading product metadata")
    _write_report(outputs, "product_metadata", records)


def _lookup_configuration(request: dict, outputs: dict) -> None:
    report = _read_report(request, "product_metadata")
    records = {}
    for period, role in zip(_PERIODS, _PRODUCT_ROLES, strict=True):
        record = report[role]
        xml, profile, granule_id = record["metadata_xml"], record["profile"], record["granule_id"]
        if not isinstance(xml, str) or not isinstance(profile, str) or (granule_id is not None and not isinstance(granule_id, str)):
            raise ValueError(f"inputs.product_metadata.{role} requires native XML, a profile and an optional granule ID")
        metadata = parse_product_metadata(xml, profile=profile, granule_id=granule_id)
        source = _validate_source(record["source"])
        _require_match(record, _metadata_record(source, metadata), f"inputs.product_metadata.{role}")
        parameter = f"{period}_configuration"
        configuration = SensorProductConfig.model_validate(request["parameters"][parameter])
        registry = SensorConfigRegistry((configuration,))
        try:
            matched = registry.lookup(metadata.key)
        except SensorConfigLookupError as error:
            raise ValueError(f"{error.code} parameters.{parameter}: {error}") from error
        records[role] = _configuration_record(source, metadata, matched)
    _write_report(outputs, "sensor_configuration", records)


@contextmanager
def _checked_products(request: dict, *, verify_mapping: bool) -> Iterator[dict[str, _Product]]:
    configurations = _read_report(request, "sensor_configuration")
    previous = _read_report(request, "band_mapping") if verify_mapping else None
    states, products = {}, {}
    for role in _PRODUCT_ROLES:
        reference = request["inputs"][role]
        path = _plain_path(reference)
        states[role] = capture_file_state(path)
        source = _source_record(reference)
        record = configurations[role]
        if source != _validate_source(record["source"]):
            raise ValueError(f"inputs.{role} path or fingerprint differs from the recorded product source")
        registry = SensorConfigRegistry((SensorProductConfig.model_validate(record["configuration"]),))
        try:
            mapping = map_product_bands(path, registry=registry)
        except SensorConfigLookupError as error:
            raise ValueError(f"{error.code} inputs.{role}: {error}") from error
        _require_match(record, _configuration_record(source, mapping.metadata, mapping.configuration),
                       f"inputs.sensor_configuration.{role}")
        product = _Product(path, source, mapping, registry)
        if previous is not None:
            _require_match(previous[role], _mapping_record(product), f"inputs.band_mapping.{role}")
        products[role] = product
    yield products
    for role, product in products.items():
        if capture_file_state(product.path) != states[role]:
            raise ValueError(f"inputs.{role} changed during remote-sensing processing")


def _map_bands(request: dict, outputs: dict) -> None:
    with _checked_products(request, verify_mapping=False) as products:
        _write_report(outputs, "band_mapping", {role: _mapping_record(product) for role, product in products.items()})


def _lineage(period: str, product: _Product) -> dict:
    mapping = product.mapping
    return {
        "period": period, "source": product.source, **_identity(mapping.metadata),
        "configuration": mapping.configuration.ref.model_dump(mode="json"),
    }


def _write_lineage(path: Path, record: dict) -> None:
    with rasterio.open(path, "r+") as dataset:
        dataset.update_tags(ns=PROVENANCE_DOMAIN, SOURCE=_canonical(record))
    if any(file != path for file in path.parent.iterdir()):
        raise ValueError("The source evidence must be embedded in the output GeoTIFF")


def _read_lineage(path: Path, *, period: str, role: str) -> dict:
    try:
        with rasterio.open(path, driver="GTiff") as dataset:
            record = json.loads(dataset.tags(ns=PROVENANCE_DOMAIN)["SOURCE"])
        if (
            not isinstance(record, dict) or record.keys() != {
                "period", "source", "product_key", "product_id", "granule_id", "configuration",
            }
            or record["period"] != period or not isinstance(record["product_id"], str) or not record["product_id"]
            or (record["granule_id"] is not None and not isinstance(record["granule_id"], str))
        ):
            raise ValueError("Expected source evidence for the requested period")
        _validate_source(record["source"])
        SensorProductKey.model_validate(record["product_key"])
        SensorConfigRef.model_validate(record["configuration"])
    except (KeyError, ValueError) as error:
        raise ValueError(f"inputs.{role}.source: {error}") from error
    return record


def _scale_reflectance(request: dict, outputs: dict) -> None:
    with _checked_products(request, verify_mapping=True) as products:
        report = {}
        for period, role in zip(_PERIODS, _PRODUCT_ROLES, strict=True):
            product = products[role]
            path = _plain_path(outputs[f"{period}_reflectance"])
            scaled = scale_product_reflectance(product.path, path, registry=product.registry)
            _write_lineage(path, _lineage(period, product))
            report[role] = {"source": product.source, **scaled.evidence}
        _write_report(outputs, "calibration_report", report)


def _build_quality_mask(request: dict, outputs: dict) -> None:
    with _checked_products(request, verify_mapping=True) as products:
        for period, role in zip(_PERIODS, _PRODUCT_ROLES, strict=True):
            product = products[role]
            path = _plain_path(outputs[f"{period}_quality_mask"])
            write_quality_mask(product.path, path, registry=product.registry)
            _write_lineage(path, _lineage(period, product))


def _calculate_ndvi(request: dict, outputs: dict) -> None:
    inputs = request["inputs"]
    for period in _PERIODS:
        reflectance_role, quality_role = f"{period}_reflectance", f"{period}_quality_mask"
        reflectance, quality = _plain_path(inputs[reflectance_role]), _plain_path(inputs[quality_role])
        sources = {reflectance_role: reflectance, quality_role: quality}
        states = {role: capture_file_state(path) for role, path in sources.items()}
        source = _read_lineage(reflectance, period=period, role=reflectance_role)
        quality_source = _read_lineage(quality, period=period, role=quality_role)
        _require_match(quality_source, source, f"inputs.{quality_role}.source")
        path = _plain_path(outputs[f"{period}_ndvi"])
        result = write_ndvi(reflectance, quality, path)
        for role, source_path in sources.items():
            if capture_file_state(source_path) != states[role]:
                raise ValueError(f"inputs.{role} changed while verifying the source and calculating NDVI")
        _write_lineage(path, source)
        print(f"{period}_ndvi: {_canonical(asdict(result.summary))}")


def _align_dates(request: dict, outputs: dict) -> None:
    inputs = request["inputs"]
    extent = inputs["analysis_extent"]
    result = align_ndvi(
        _plain_path(inputs["first_ndvi"]), _plain_path(inputs["second_ndvi"]), _plain_path(outputs["aligned_ndvi"]),
        resampling=request["parameters"]["resampling"], analysis_extent=extent["path"], analysis_layer=extent.get("layer"),
    )
    _write_report(outputs, "alignment_report", asdict(result.summary))


def _calculate_change(request: dict, outputs: dict) -> None:
    delta = _plain_path(outputs["change_raster"])
    write_ndvi_change(_plain_path(request["inputs"]["aligned_ndvi"]), delta)
    parameters = request["parameters"]
    classified = classify_ndvi_change(
        delta, _plain_path(outputs["change_classes"]), threshold=parameters["change_threshold"], area_crs=parameters["area_crs"],
    )
    _write_report(outputs, "area_statistics", classified.statistics)


_OPERATIONS = {
    "python-remote-sensing-read-metadata": _read_metadata,
    "python-remote-sensing-lookup-configuration": _lookup_configuration,
    "python-remote-sensing-map-bands": _map_bands,
    "python-remote-sensing-scale-reflectance": _scale_reflectance,
    "python-remote-sensing-quality-mask": _build_quality_mask,
    "python-remote-sensing-calculate-ndvi": _calculate_ndvi,
    "python-remote-sensing-align-dates": _align_dates,
    "python-remote-sensing-calculate-change": _calculate_change,
}


def remote_sensing_workflow(request: dict) -> dict:
    """Dispatch the exact registered stage and publish its named outputs together."""
    tool = ToolRef.model_validate(request["tool"])
    operation = _OPERATIONS.get(tool.id)
    if tool.version != 1 or operation is None:
        raise ValueError(f"Unknown remote-sensing tool {tool.id}@{tool.version}")
    print("remote_sensing_environment: " + _canonical({
        "tool": tool.id, "version": tool.version,
        "rasterio": rasterio.__version__, "gdal": rasterio.__gdal_version__, "numpy": np.__version__,
    }))
    outputs = request["output_targets"]
    for reference in outputs.values():
        _check_destination(_plain_path(reference))
    with _stage_outputs(outputs) as temporary:
        operation(request, temporary)
    return outputs


if __name__ == "__main__":
    raise SystemExit(run_tool_command(remote_sensing_workflow, description=__doc__))
