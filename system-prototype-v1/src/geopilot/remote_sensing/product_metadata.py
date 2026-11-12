"""Read the native XML carried by a registered, packed remote-sensing product."""

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from xml.etree import ElementTree as ET

import rasterio
from rasterio.errors import RasterioError
from rasterio.io import DatasetReader

from geopilot.remote_sensing.config_models import BandMetadataFieldRef, MetadataFieldRef, SensorProductKey


PRODUCT_METADATA_DOMAIN = "GEOPILOT_PRODUCT"
LANDSAT_PROFILE = "landsat-c2-level-2"
SENTINEL_PROFILE = "sentinel-2-level-2a"
_LANDSAT_ROOT = "LANDSAT_METADATA_FILE"
_SENTINEL_NAMESPACE = "https://psd-15.sentinel2.eo.esa.int/PSD/User_Product_Level-2A.xsd"
_SENTINEL_ROOT = f"{{{_SENTINEL_NAMESPACE}}}Level-2A_User_Product"
_LANDSAT_PLATFORMS = {"LANDSAT_8": "landsat-8-oli-tirs", "LANDSAT_9": "landsat-9-oli-tirs"}
_LANDSAT_BANDS = {
    "red": ("SR_B4", "FILE_NAME_BAND_4"),
    "nir": ("SR_B5", "FILE_NAME_BAND_5"),
    "quality": ("QA_PIXEL", "FILE_NAME_QUALITY_L1_PIXEL"),
    "saturation": ("QA_RADSAT", "FILE_NAME_QUALITY_L1_RADIOMETRIC_SATURATION"),
}
_SENTINEL_BANDS = {"red": ("B04", 10), "nir": ("B08", 10), "quality": ("SCL", 20)}
_SENTINEL_SPECTRAL = {"B04": ("B4", 3), "B08": ("B8", 7)}
_PRODUCT_INFO = ("General_Info", "Product_Info")
_SPECTRAL_INFO = ("General_Info", "Product_Image_Characteristics", "Spectral_Information_List")
_GRANULE_LIST = (*_PRODUCT_INFO, "Product_Organisation", "Granule_List")
_IMAGE_FILE_FIELD = ".".join(("product_metadata", *_GRANULE_LIST, "Granule", "IMAGE_FILE"))


class ProductMetadataError(ValueError):
    code = "PRODUCT_METADATA_INVALID"

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class NativeProductBand:
    """One native file declaration and its original sampling resolution."""

    role: str
    code: str
    native_resolution_m: float
    source_filename: str
    declared_filename: str
    metadata_field: str
    band_id: int | None = None


@dataclass(frozen=True, slots=True)
class ProductMetadata:
    """Product identity and source evidence from the original XML."""

    profile: str
    key: SensorProductKey
    product_id: str
    spacecraft: str
    processing_level: str
    xml: str
    granule_id: str | None
    band_sources: Mapping[str, NativeProductBand]


class _ProductTreeBuilder(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ET.ParseError("Product metadata cannot contain a document type declaration")


def _xml_root(xml: str) -> ET.Element:
    try:
        return ET.fromstring(xml, parser=ET.XMLParser(target=_ProductTreeBuilder()))
    except (ET.ParseError, ValueError) as error:
        raise ProductMetadataError("product_metadata.METADATA_XML", f"Invalid product XML: {error}") from error


def _metadata_field(path: Sequence[str], band_id: int | None = None) -> str:
    field = ".".join(("product_metadata", *path))
    return field if band_id is None else f"{field}[band_id={band_id}]"


def _only(elements: Sequence[ET.Element], field: str) -> ET.Element:
    if len(elements) != 1:
        raise ProductMetadataError(field, f"Expected one XML element at {field}, found {len(elements)}")
    return elements[0]


def _element(root: ET.Element, path: Sequence[str], *, band_id: int | None = None) -> ET.Element:
    element = root
    for index, name in enumerate(path):
        # PSD 15 qualifies the root and its direct groups. Their descendants use local elements.
        tag = f"{{{_SENTINEL_NAMESPACE}}}{name}" if index == 0 and root.tag == _SENTINEL_ROOT else name
        matches = [child for child in element if child.tag == tag]
        selector = band_id if index == len(path) - 1 else None
        if selector is not None:
            matches = [child for child in matches if child.get("band_id") == str(selector)]
        element = _only(matches, _metadata_field(path[:index + 1], selector))
    return element


def _text(element: ET.Element, field: str) -> str:
    value = (element.text or "").strip()
    if len(element) or not value:
        raise ProductMetadataError(field, f"Expected a nonempty XML text value at {field}")
    return value


def _value(root: ET.Element, path: Sequence[str], *, band_id: int | None = None) -> str:
    return _text(_element(root, path, band_id=band_id), _metadata_field(path, band_id))


def resolve_metadata_field(metadata: ProductMetadata, field: MetadataFieldRef) -> str:
    """Resolve one configured field by its native XML path and optional band ID."""
    band_id = field.band_id if isinstance(field, BandMetadataFieldRef) else None
    return _value(_xml_root(metadata.xml), field.path, band_id=band_id)


def _landsat_metadata(root: ET.Element, xml: str) -> ProductMetadata:
    product = _value(root, ("PRODUCT_CONTENTS", "LANDSAT_PRODUCT_ID"))
    level = _value(root, ("PRODUCT_CONTENTS", "PROCESSING_LEVEL"))
    collection = _value(root, ("PRODUCT_CONTENTS", "COLLECTION_NUMBER"))
    spacecraft = _value(root, ("IMAGE_ATTRIBUTES", "SPACECRAFT_ID"))
    sensor = _value(root, ("IMAGE_ATTRIBUTES", "SENSOR_ID"))
    if spacecraft not in _LANDSAT_PLATFORMS or sensor != "OLI_TIRS":
        raise ProductMetadataError("product_metadata.IMAGE_ATTRIBUTES", "Expected a Landsat 8 or 9 OLI/TIRS product")
    if level not in {"L2SP", "L2SR"}:
        raise ProductMetadataError("product_metadata.PRODUCT_CONTENTS.PROCESSING_LEVEL", "Expected Landsat L2SP or L2SR")
    expected_id = rf"LC0{spacecraft[-1]}_{level}_\d{{6}}_\d{{8}}_\d{{8}}_{re.escape(collection)}_[A-Z0-9]+"
    if re.fullmatch(r"[0-9]{2}", collection) is None or re.fullmatch(expected_id, product) is None:
        raise ProductMetadataError(
            "product_metadata.PRODUCT_CONTENTS.LANDSAT_PRODUCT_ID",
            "Landsat product identifier disagrees with the spacecraft, level or collection",
        )
    sources = {}
    for role, (code, field_name) in _LANDSAT_BANDS.items():
        path = ("PRODUCT_CONTENTS", field_name)
        source = _value(root, path)
        field = _metadata_field(path)
        if source[:-4] != f"{product}_{code}" or source[-4:].lower() != ".tif":
            raise ProductMetadataError(field, f"Expected the {code} file belonging to {product}")
        sources[code] = NativeProductBand(role, code, 30, source, source, field)
    key = SensorProductKey(sensor=_LANDSAT_PLATFORMS[spacecraft], product_level=level, processing_version=collection)
    return ProductMetadata(LANDSAT_PROFILE, key, product, spacecraft, level, xml, None, MappingProxyType(sources))


def _sentinel_granule(root: ET.Element, selector: str | None) -> tuple[str, ET.Element]:
    field = "product_metadata.GRANULE_ID"
    by_id = {}
    for granule in _element(root, _GRANULE_LIST).findall("Granule"):
        identifier = granule.get("granuleIdentifier")
        if not identifier or identifier != identifier.strip() or identifier in by_id:
            raise ProductMetadataError(field, "Each Sentinel granule requires a distinct native identifier")
        by_id[identifier] = granule
    if not by_id:
        raise ProductMetadataError(field, "No native Sentinel granules are declared")
    if selector is None:
        if len(by_id) != 1:
            raise ProductMetadataError(field, "Specify GRANULE_ID when Sentinel metadata has multiple granules")
        selector = next(iter(by_id))
    if selector not in by_id:
        raise ProductMetadataError(field, "GRANULE_ID does not identify a granule in the product XML")
    return selector, by_id[selector]


def _sentinel_band_id(spectral_info: ET.Element, code: str, resolution: int) -> int:
    physical_band, band_id = _SENTINEL_SPECTRAL[code]
    field = _metadata_field((*_SPECTRAL_INFO, "Spectral_Information"))
    candidates = [
        element for element in spectral_info.findall("Spectral_Information")
        if element.get("physicalBand") == physical_band or element.get("bandId") == str(band_id)
    ]
    element = _only(candidates, field)
    resolution_field = f"{field}.RESOLUTION"
    declared_resolution = _text(_only(element.findall("RESOLUTION"), resolution_field), resolution_field)
    if (element.get("physicalBand"), element.get("bandId"), declared_resolution) != (physical_band, str(band_id), str(resolution)):
        raise ProductMetadataError(field, f"Sentinel spectral identity for {code} disagrees with the native band")
    return band_id


def _sentinel_sources(root: ET.Element, granule: ET.Element) -> Mapping[str, NativeProductBand]:
    declared = [_text(element, _IMAGE_FILE_FIELD) for element in granule.findall("IMAGE_FILE")]
    spectral_info = _element(root, _SPECTRAL_INFO)
    sources = {}
    granule_locations = set()
    for role, (code, resolution) in _SENTINEL_BANDS.items():
        suffix = f"_{code}_{resolution}m"
        matches = [entry for entry in declared if entry.removesuffix(".jp2").endswith(suffix)]
        if len(matches) != 1:
            raise ProductMetadataError(_IMAGE_FILE_FIELD, f"Expected one Sentinel {code} file at {resolution} m, found {len(matches)}")
        entry = matches[0]
        stem = entry.removesuffix(".jp2")
        parts = stem.split("/")
        if (
            len(parts) != 5 or parts[0] != "GRANULE" or parts[2:4] != ["IMG_DATA", f"R{resolution}m"]
            or re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]) is None
            or re.fullmatch(r"T[0-9]{2}[A-Z]{3}_\d{8}T\d{6}", parts[4].removesuffix(suffix)) is None
        ):
            raise ProductMetadataError(_IMAGE_FILE_FIELD, f"Invalid native Sentinel {code} image path: {entry}")
        granule_locations.add((parts[1], parts[4].removesuffix(suffix)))
        band_id = _sentinel_band_id(spectral_info, code, resolution) if code in _SENTINEL_SPECTRAL else None
        sources[code] = NativeProductBand(role, code, resolution, f"{stem}.jp2", entry, _IMAGE_FILE_FIELD, band_id)
    if len(granule_locations) != 1:
        raise ProductMetadataError(_IMAGE_FILE_FIELD, "Sentinel source bands must come from the same tile and granule")
    return MappingProxyType(sources)


def _sentinel_metadata(root: ET.Element, xml: str, granule_id: str | None) -> ProductMetadata:
    product = _value(root, (*_PRODUCT_INFO, "PRODUCT_URI"))
    level = _value(root, (*_PRODUCT_INFO, "PROCESSING_LEVEL"))
    product_type = _value(root, (*_PRODUCT_INFO, "PRODUCT_TYPE"))
    baseline = _value(root, (*_PRODUCT_INFO, "PROCESSING_BASELINE"))
    spacecraft = _value(root, (*_PRODUCT_INFO, "Datatake", "SPACECRAFT_NAME"))
    if level != "Level-2A" or product_type != "S2MSI2A":
        raise ProductMetadataError(_metadata_field(_PRODUCT_INFO), "Expected a Sentinel-2 MSI Level-2A product")
    if spacecraft not in {"Sentinel-2A", "Sentinel-2B", "Sentinel-2C", "Sentinel-2D"}:
        raise ProductMetadataError(
            _metadata_field((*_PRODUCT_INFO, "Datatake", "SPACECRAFT_NAME")), "Unrecognized Sentinel-2 spacecraft",
        )
    parts = product.removesuffix(".SAFE").split("_")
    if (
        re.fullmatch(r"[0-9]{2}\.[0-9]{2}", baseline) is None or len(parts) != 7
        or parts[:2] != [f"S2{spacecraft[-1]}", "MSIL2A"] or parts[3] != f"N{baseline.replace('.', '')}"
    ):
        raise ProductMetadataError(
            _metadata_field((*_PRODUCT_INFO, "PRODUCT_URI")),
            "Sentinel product identifier disagrees with the spacecraft, level or processing baseline",
        )
    selected, granule = _sentinel_granule(root, granule_id)
    sources = _sentinel_sources(root, granule)
    key = SensorProductKey(sensor="sentinel-2-msi", product_level="L2A", processing_version=baseline)
    return ProductMetadata(SENTINEL_PROFILE, key, product, spacecraft, level, xml, selected, sources)


def parse_product_metadata(xml: str, *, profile: str, granule_id: str | None = None) -> ProductMetadata:
    """Parse one explicitly identified Landsat MTL or Sentinel-2 product XML."""
    expected_roots = {LANDSAT_PROFILE: _LANDSAT_ROOT, SENTINEL_PROFILE: _SENTINEL_ROOT}
    if profile not in expected_roots:
        raise ProductMetadataError("product_metadata.PROFILE", f"Unsupported product profile: {profile!r}")
    if not xml.strip():
        raise ProductMetadataError("product_metadata.METADATA_XML", "Original product XML is required")
    root = _xml_root(xml)
    if root.tag != expected_roots[profile]:
        raise ProductMetadataError("product_metadata.PROFILE", f"Product XML does not match profile {profile!r}")
    if profile == SENTINEL_PROFILE:
        return _sentinel_metadata(root, xml, granule_id)
    if granule_id is not None:
        raise ProductMetadataError("product_metadata.GRANULE_ID", "GRANULE_ID applies to the Sentinel product profile")
    return _landsat_metadata(root, xml)


@contextmanager
def _open_product_raster(path: str | Path) -> Iterator[DatasetReader]:
    try:
        source = Path(path).expanduser().resolve(strict=True)
        if source.as_posix().startswith("/vsi") or not source.is_file():
            raise ValueError("Expected a regular local GeoTIFF file")
        dataset = rasterio.open(source, mode="r", driver="GTiff")
    except (OSError, RasterioError, RuntimeError, ValueError) as error:
        raise ProductMetadataError("source.path", f"Unable to open packed product GeoTIFF {path!s}: {error}") from error
    with dataset:
        if dataset.driver != "GTiff":
            raise ProductMetadataError("source.path", "The packed product must use the GDAL GTiff driver")
        yield dataset


def _read_product_metadata(dataset: DatasetReader) -> ProductMetadata:
    tags = dataset.tags(ns=PRODUCT_METADATA_DOMAIN)
    return parse_product_metadata(
        tags.get("METADATA_XML", ""), profile=tags.get("PROFILE", ""), granule_id=tags.get("GRANULE_ID"),
    )


def read_product_metadata(path: str | Path) -> ProductMetadata:
    """Open the original packed GeoTIFF and read its embedded XML without reading pixels."""
    with _open_product_raster(path) as dataset:
        return _read_product_metadata(dataset)
