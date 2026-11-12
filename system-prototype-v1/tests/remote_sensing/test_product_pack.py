import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from product_fixtures import GRANULE, LANDSAT, SENTINEL
from rasterio.enums import MaskFlags
from rasterio.transform import from_origin

from geopilot.data.assets import register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.resolver import resolve_asset
from geopilot.persistence.schema import initialize_database
from geopilot.remote_sensing.band_mapper import map_product_bands
from geopilot.remote_sensing.config_registry import SensorConfigLookupError
from geopilot.remote_sensing.product_metadata import (
    PRODUCT_METADATA_DOMAIN,
    parse_product_metadata,
    read_product_metadata,
)
from geopilot.remote_sensing.product_pack import ProductPackError, pack_native_product
from geopilot.remote_sensing.quality_mask import write_quality_mask
from geopilot.remote_sensing.scaling import scale_product_reflectance


ROOT = Path(__file__).resolve().parents[2]
CRS = "EPSG:32632"
ORIGIN = (500000, 5100000)
LANDSAT_DN = {
    "SR_B4": [[0, 10000, 65535], [13000, 11000, 12000]],
    "SR_B5": [[20000, 20000, 21000], [22000, 20000, 23000]],
    "QA_PIXEL": [[0, 0, 8], [0, 0, 0]],
    "QA_RADSAT": [[0, 0, 0], [16, 0, 0]],
}
SENTINEL_DN = {
    "B04": np.arange(24).reshape(4, 6) + 3000,
    "B08": np.arange(24).reshape(4, 6) + 7000,
    "SCL": [[4, 9, 0], [6, 12, 5]],
}


def _native_layout(directory, xml, profile):
    directory.mkdir()
    metadata_path = directory / ("MTD_MSIL2A.xml" if profile == SENTINEL else "product_MTL.xml")
    metadata_path.write_bytes(xml.encode("utf-8"))
    metadata = parse_product_metadata(xml, profile=profile)
    paths = {code: directory / band.source_filename for code, band in metadata.band_sources.items()}
    return metadata_path, paths


def _write_native(path, values, *, pixel_m, dtype="uint16", origin=ORIGIN, nodata=None, mask=None,
                  internal_mask=True, driver=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.asarray(values, dtype=dtype)
    driver = driver or ("JP2OpenJPEG" if path.suffix == ".jp2" else "GTiff")
    options = {"REVERSIBLE": "YES", "QUALITY": 100, "YCBCR420": "NO"} if driver == "JP2OpenJPEG" else {}
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=internal_mask), rasterio.open(
        path, "w", driver=driver, width=data.shape[1], height=data.shape[0], count=1, dtype=dtype,
        crs=CRS, transform=from_origin(*origin, pixel_m, pixel_m), nodata=nodata, **options,
    ) as output:
        output.write(data, 1)
        if mask is not None:
            output.write_mask(np.asarray(mask, dtype=bool))
    return path


def _write_landsat_inputs(paths):
    for code, path in paths.items():
        _write_native(path, LANDSAT_DN[code], pixel_m=30, nodata=0 if code in {"SR_B4", "SR_B5"} else None)


def _write_sentinel_inputs(paths):
    for code, path in paths.items():
        _write_native(path, SENTINEL_DN[code], pixel_m=20 if code == "SCL" else 10,
                      dtype="uint8" if code == "SCL" else "uint16")


def _write_external_mask(path, mask):
    companion = path.with_name(f"{path.name}.msk")
    with rasterio.open(path) as source, rasterio.open(
        companion, "w", driver="GTiff", width=source.width, height=source.height, count=1,
        dtype="uint8", crs=source.crs, transform=source.transform,
    ) as output:
        output.write(np.where(mask, 255, 0).astype("uint8"), 1)
        output.update_tags(INTERNAL_MASK_FLAGS_1=str(int(MaskFlags.per_dataset)))
    return companion


def test_landsat_exact_native_bytes_raw_qa_zero_and_p1_registration(tmp_path, landsat_product_xml, sensor_registry):
    xml = landsat_product_xml().replace("\n", "\r\n")
    metadata_path, paths = _native_layout(tmp_path / "landsat", xml, LANDSAT)
    _write_landsat_inputs(paths)
    mask = [[True] * 3, [True, True, False]]
    _write_native(paths["QA_PIXEL"], LANDSAT_DN["QA_PIXEL"], pixel_m=30, mask=mask)
    expected_fingerprints = {code: fingerprint_file(path).digest for code, path in paths.items()}
    target = tmp_path / "packed.tif"

    result = pack_native_product(metadata_path, target, profile=LANDSAT, registry=sensor_registry)

    mapping = map_product_bands(target, registry=sensor_registry)
    assert result.path == target and result.product.xml == mapping.metadata.xml == xml
    assert result.configuration_ref == mapping.configuration.ref
    assert result.metadata.band_names == ("SR_B4", "SR_B5", "QA_PIXEL", "QA_RADSAT")
    assert result.metadata_source.fingerprint == fingerprint_file(metadata_path).digest
    assert {code: source.fingerprint for code, source in result.native_sources.items()} == expected_fingerprints
    with rasterio.open(target) as dataset:
        assert dataset.nodatavals == (None,) * 4 and dataset.dtypes == ("uint16",) * 4
        assert dataset.transform == from_origin(*ORIGIN, 30, 30) and str(dataset.crs) == CRS
        assert dataset.files == [str(target)]
        assert all(MaskFlags.per_dataset in flags for flags in dataset.mask_flag_enums)
        for index, code in enumerate(dataset.descriptions, 1):
            np.testing.assert_array_equal(dataset.read(index), LANDSAT_DN[code])
            np.testing.assert_array_equal(dataset.read_masks(index), 255 * np.asarray(mask))
        tags = dataset.tags(ns=PRODUCT_METADATA_DOMAIN)
        provenance = json.loads(tags["NATIVE_SOURCES"])
        assert str(tmp_path) not in tags["NATIVE_SOURCES"]
        assert provenance["metadata"] == {"relative_filename": metadata_path.name, "fingerprint": result.metadata_source.fingerprint}
        for code, source in provenance["bands"].items():
            assert source["relative_filename"] == paths[code].name and source["fingerprint"] == expected_fingerprints[code]
        assert provenance["bands"]["SR_B4"]["mask_flags"] == ["nodata"]
        assert provenance["bands"]["QA_PIXEL"]["mask_flags"] == ["per_dataset"]
        assert provenance["bands"]["QA_RADSAT"]["mask_flags"] == ["all_valid"]

    reflectance, quality = tmp_path / "reflectance.tif", tmp_path / "quality.tif"
    scale_product_reflectance(target, reflectance, registry=sensor_registry)
    write_quality_mask(target, quality, registry=sensor_registry)
    with rasterio.open(reflectance) as output:
        assert np.isnan(output.read(1)[0, 0]) and output.read(2)[0, 0] == pytest.approx(0.35)
    with rasterio.open(quality) as output:
        assert output.read(1).tolist() == [[0, 1, 0], [0, 1, 0]]

    assert {code: fingerprint_file(path).digest for code, path in paths.items()} == expected_fingerprints
    database = tmp_path / "geopilot.sqlite"
    initialize_database(database)
    asset = register_asset(database, target)
    metadata_path.parent.rename(tmp_path / "moved-native")
    assert resolve_asset(database, asset.ref) == asset
    assert read_product_metadata(asset.source.path).xml == xml


def test_sentinel_real_lossless_jp2_and_native_half_resolution_nearest(tmp_path, sentinel_product_xml, sensor_registry):
    xml = sentinel_product_xml()
    metadata_path, paths = _native_layout(tmp_path / "sentinel.SAFE", xml, SENTINEL)
    _write_sentinel_inputs(paths)
    for code, source in paths.items():
        with rasterio.open(source) as dataset:
            assert dataset.driver == "JP2OpenJPEG" and dataset.files == [str(source)]
            np.testing.assert_array_equal(dataset.read(1), SENTINEL_DN[code])
    scl_mask = np.asarray([[True, True, True], [True, False, True]])
    companion = _write_external_mask(paths["SCL"], scl_mask)
    with rasterio.open(paths["SCL"]) as dataset:
        np.testing.assert_array_equal(dataset.read_masks(1), 255 * scl_mask)
    expected_fingerprints = {code: fingerprint_file(path).digest for code, path in paths.items()}
    target = tmp_path / "packed.tif"

    result = pack_native_product(metadata_path, target, profile=SENTINEL, granule_id=GRANULE, registry=sensor_registry)

    mapping = map_product_bands(target, registry=sensor_registry)
    assert mapping.metadata.xml == xml and mapping.metadata.granule_id == GRANULE
    assert result.metadata.band_names == ("B04", "B08", "SCL")
    assert mapping.bands["quality"].native_resolution_m == 20 and mapping.bands["quality"].resampling == "nearest"
    with rasterio.open(target) as output:
        assert output.nodatavals == (None,) * 3 and output.dtypes == ("uint16",) * 3
        assert output.transform == from_origin(*ORIGIN, 10, 10) and output.shape == (4, 6)
        np.testing.assert_array_equal(output.read(1), SENTINEL_DN["B04"])
        np.testing.assert_array_equal(output.read(2), SENTINEL_DN["B08"])
        reference = np.asarray(SENTINEL_DN["SCL"]).repeat(2, axis=0).repeat(2, axis=1)
        np.testing.assert_array_equal(output.read(3), reference)
        np.testing.assert_array_equal(output.dataset_mask(), 255 * scl_mask.repeat(2, axis=0).repeat(2, axis=1))
        provenance = json.loads(output.tags(ns=PRODUCT_METADATA_DOMAIN)["NATIVE_SOURCES"])
        assert provenance["bands"]["SCL"]["relative_filename"].endswith("_SCL_20m.jp2")
        assert provenance["bands"]["SCL"]["driver"] == "JP2OpenJPEG"
        assert provenance["bands"]["SCL"]["resolution"] == [20, 20]
        assert provenance["bands"]["SCL"]["resampling"] == "nearest"
        assert provenance["bands"]["SCL"]["companions"] == [{
            "relative_filename": companion.relative_to(metadata_path.parent).as_posix(),
            "fingerprint": fingerprint_file(companion).digest,
        }]
    assert {code: fingerprint_file(path).digest for code, path in paths.items()} == expected_fingerprints


def test_external_gdal_source_mask_is_preserved_and_fingerprinted(tmp_path, landsat_product_xml, sensor_registry):
    metadata_path, paths = _native_layout(tmp_path / "landsat", landsat_product_xml(), LANDSAT)
    _write_landsat_inputs(paths)
    qa = paths["QA_RADSAT"]
    mask = [[False, True, True], [True, True, True]]
    _write_native(qa, LANDSAT_DN["QA_RADSAT"], pixel_m=30, mask=mask, internal_mask=False)
    companion = qa.with_name(f"{qa.name}.msk")
    assert companion.is_file()
    target = tmp_path / "packed.tif"

    pack_native_product(metadata_path, target, profile=LANDSAT, registry=sensor_registry)

    with rasterio.open(target) as output:
        assert output.files == [str(target)]
        np.testing.assert_array_equal(output.dataset_mask(), 255 * np.asarray(mask))
        provenance = json.loads(output.tags(ns=PRODUCT_METADATA_DOMAIN)["NATIVE_SOURCES"])
        assert provenance["bands"]["QA_RADSAT"]["companions"] == [
            {"relative_filename": companion.name, "fingerprint": fingerprint_file(companion).digest},
        ]


@pytest.mark.parametrize(
    ("fault", "field"),
    [("missing", "bands.nir.path"), ("shifted", "bands.nir.grid"), ("qa_zero_nodata", "bands.quality.nodata"),
     ("hardlink_alias", "bands.nir.path")],
)
def test_rejects_bad_landsat_sources_without_publishing(tmp_path, landsat_product_xml, sensor_registry, fault, field):
    metadata_path, paths = _native_layout(tmp_path / "landsat", landsat_product_xml(), LANDSAT)
    _write_landsat_inputs(paths)
    if fault == "missing":
        paths["SR_B5"].unlink()
    elif fault == "shifted":
        _write_native(paths["SR_B5"], LANDSAT_DN["SR_B5"], pixel_m=30, origin=(ORIGIN[0] + 30, ORIGIN[1]))
    elif fault == "qa_zero_nodata":
        _write_native(paths["QA_PIXEL"], LANDSAT_DN["QA_PIXEL"], pixel_m=30, nodata=0)
    elif fault == "hardlink_alias":
        paths["SR_B5"].unlink()
        paths["SR_B5"].hardlink_to(paths["SR_B4"])
    target = tmp_path / "packed.tif"

    with pytest.raises(ProductPackError) as failure:
        pack_native_product(metadata_path, target, profile=LANDSAT, registry=sensor_registry)
    assert failure.value.field == field
    assert set(tmp_path.iterdir()) == {metadata_path.parent}


@pytest.mark.parametrize(
    ("fault", "field"), [("wrong_driver", "bands.quality.driver"), ("misaligned_scl", "bands.quality.grid"),
                         ("escaped_symlink", "bands.red.path")],
)
def test_rejects_malformed_sentinel_native_sources(tmp_path, sentinel_product_xml, sensor_registry, fault, field):
    metadata_path, paths = _native_layout(tmp_path / "sentinel.SAFE", sentinel_product_xml(), SENTINEL)
    _write_sentinel_inputs(paths)
    if fault == "wrong_driver":
        paths["SCL"].unlink()
        _write_native(paths["SCL"], SENTINEL_DN["SCL"], pixel_m=20, dtype="uint8", driver="GTiff")
    elif fault == "misaligned_scl":
        paths["SCL"].unlink()
        _write_native(paths["SCL"], SENTINEL_DN["SCL"], pixel_m=20, dtype="uint8", origin=(ORIGIN[0] + 10, ORIGIN[1]))
    elif fault == "escaped_symlink":
        outside = tmp_path / "outside.jp2"
        paths["B04"].rename(outside)
        paths["B04"].symlink_to(outside)
    target = tmp_path / "packed.tif"

    with pytest.raises(ProductPackError) as failure:
        pack_native_product(metadata_path, target, profile=SENTINEL, registry=sensor_registry)
    assert failure.value.field == field and not target.exists()
    assert not any(tmp_path.glob(".product-pack-*"))


def test_unknown_product_configuration_blocks_before_source_reads(tmp_path, sentinel_product_xml, sensor_registry):
    metadata_path, _ = _native_layout(tmp_path / "unknown.SAFE", sentinel_product_xml(baseline="05.12"), SENTINEL)
    target = tmp_path / "packed.tif"
    with pytest.raises(SensorConfigLookupError) as failure:
        pack_native_product(metadata_path, target, profile=SENTINEL, registry=sensor_registry)
    assert failure.value.code == "SENSOR_CONFIG_NOT_FOUND" and not target.exists()


def test_source_xml_modified_during_real_read_blocks_and_leaves_no_partial_target(
    tmp_path, landsat_product_xml, sensor_registry, monkeypatch,
):
    metadata_path, paths = _native_layout(tmp_path / "landsat", landsat_product_xml(), LANDSAT)
    _write_landsat_inputs(paths)
    target = tmp_path / "packed.tif"
    actual_open = rasterio.open

    def track_reads(path, *args, **kwargs):
        dataset = actual_open(path, *args, **kwargs)
        if Path(path) == paths["SR_B4"]:
            actual_read = dataset.read

            def read(*args, **kwargs):
                values = actual_read(*args, **kwargs)
                with metadata_path.open("ab") as stream:
                    stream.write(b"\n")
                return values

            dataset.read = read
        return dataset

    with monkeypatch.context() as patch, pytest.raises(ProductPackError) as failure:
        patch.setattr(rasterio, "open", track_reads)
        pack_native_product(metadata_path, target, profile=LANDSAT, registry=sensor_registry)
    assert (failure.value.code, failure.value.field) == ("PRODUCT_SOURCE_CHANGED", "metadata_xml")
    assert set(tmp_path.iterdir()) == {metadata_path.parent}


def test_detects_jp2_companion_added_during_native_read(tmp_path, sentinel_product_xml, sensor_registry, monkeypatch):
    metadata_path, paths = _native_layout(tmp_path / "sentinel.SAFE", sentinel_product_xml(), SENTINEL)
    _write_sentinel_inputs(paths)
    scl = paths["SCL"]
    before = fingerprint_file(scl).digest
    target = tmp_path / "packed.tif"
    actual_open = rasterio.open

    def track_reads(path, *args, **kwargs):
        dataset = actual_open(path, *args, **kwargs)
        if Path(path) == scl:
            actual_read = dataset.read

            def read(*args, **kwargs):
                data = actual_read(*args, **kwargs)
                _write_external_mask(scl, [[True, True, True], [True, False, True]])
                return data

            dataset.read = read
        return dataset

    with monkeypatch.context() as patch, pytest.raises(ProductPackError) as failure:
        patch.setattr(rasterio, "open", track_reads)
        pack_native_product(metadata_path, target, profile=SENTINEL, registry=sensor_registry)
    assert (failure.value.code, failure.value.field) == ("PRODUCT_SOURCE_CHANGED", "bands.quality.path")
    assert fingerprint_file(scl).digest == before
    assert set(tmp_path.iterdir()) == {metadata_path.parent}


@pytest.mark.parametrize("name", ["packed.tif", "packed.tif.msk"])
def test_preserves_existing_destination_and_companion(tmp_path, landsat_product_xml, sensor_registry, name):
    metadata_path, paths = _native_layout(tmp_path / "landsat", landsat_product_xml(), LANDSAT)
    _write_landsat_inputs(paths)
    existing = tmp_path / name
    existing.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        pack_native_product(metadata_path, tmp_path / "packed.tif", profile=LANDSAT, registry=sensor_registry)
    assert existing.read_bytes() == b"preserve" and set(tmp_path.iterdir()) == {metadata_path.parent, existing}


def test_cli_packs_real_local_product_and_prints_its_exact_identity(tmp_path, landsat_product_xml):
    metadata_path, paths = _native_layout(tmp_path / "landsat", landsat_product_xml(), LANDSAT)
    _write_landsat_inputs(paths)
    target = tmp_path / "cli-output.tif"
    completed = subprocess.run(
        [sys.executable, "-m", "geopilot.remote_sensing.product_pack", str(metadata_path), str(target),
         "--profile", LANDSAT, "--config-dir", str(ROOT / "config" / "sensors")],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    assert evidence["path"] == str(target)
    assert evidence["configuration"] == {"id": "landsat-8-c2-l2sp", "version": 1}
    assert evidence["product_id"] == read_product_metadata(target).product_id
