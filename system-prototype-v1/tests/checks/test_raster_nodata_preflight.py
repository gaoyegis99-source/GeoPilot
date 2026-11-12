"""Exercise the contract gate with actual GeoTIFFs and the normal child runner."""

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
from pydantic import ValidationError
from rasterio.shutil import copy as copy_raster
from rasterio.transform import from_origin

from geopilot.checks.input_checks import CoverageRequirement, preflight_step_inputs
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.python_runner import run_python
from geopilot.execution.runner import run_step
from geopilot.gis.tools.remote_sensing_workflow import PROVENANCE_DOMAIN
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
CRS = "EPSG:32650"
TRANSFORM = from_origin(500000, 4400000, 10, 10)
DEM = "python-dem-unit-check"
NDVI = "python-remote-sensing-calculate-ndvi"


def _document(tool_id):
    path = ROOT / "config" / "tools" / f"{tool_id}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dem_contract(policy=None):
    document = _document(DEM)
    if policy is not None:
        document["raster_nodata"] = {"dem": policy}
    return ToolContract.model_validate(document)


def _write_tiff(path, samples, *, nodata=None, mask=None, internal_mask=True, names=None, unit="meter", lineage=None):
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=internal_mask):
        with rasterio.open(
            path, "w", driver="GTiff", dtype=samples.dtype, count=samples.shape[0],
            height=samples.shape[1], width=samples.shape[2], transform=TRANSFORM, crs=CRS, nodata=nodata,
            tiled=True, blockxsize=128, blockysize=128,
        ) as dataset:
            dataset.write(samples)
            for band in dataset.indexes:
                if names is not None:
                    dataset.set_band_description(band, names[band - 1])
                if unit is not None:
                    dataset.set_band_unit(band, unit)
            if mask is not None:
                dataset.write_mask(np.asarray(mask, dtype="uint8") * 255)
            if lineage is not None:
                dataset.update_tags(ns=PROVENANCE_DOMAIN, SOURCE=json.dumps(lineage))


def _run(
    workdir, contract, inputs, outputs, *, parameters=None, input_preflight=None, cancel_requested=None, low_level=False,
):
    (workdir / "step").mkdir()
    (workdir / "outputs").mkdir()
    worker_name = contract.implementation.removeprefix("geopilot.")
    worker = ROOT / "src" / "geopilot" / "gis" / "tools" / f"{worker_name}.py"
    registry = ToolRegistry(python_implementations=(contract.implementation,))
    registry.register(contract)
    request = StepRequest(
        run_id="raster-nodata", step_key="check", tool=contract.ref, workdir_ref="raster-nodata",
        inputs={role: {"path": path} for role, path in inputs.items()}, parameters=parameters or {},
        output_targets={role: {"path": f"outputs/{path}"} for role, path in outputs.items()},
        log_path="step/process.log", cancel_path="cancel.flag",
    )
    implementations = {contract.implementation: worker}
    if low_level:
        result = StepResult.model_validate(run_python(
            request.model_dump(mode="json", exclude_unset=True), registry=registry, workdir=workdir,
            implementations=implementations,
        ))
    else:
        result = run_step(
            request, registry=registry, workdir=workdir, python_implementations=implementations,
            input_preflight=input_preflight, cancel_requested=cancel_requested,
        )
    assert StepResult.model_validate_json((workdir / "step/result.json").read_text()) == result
    log = (workdir / "step/process.log").read_text(encoding="utf-8")
    checks = [
        json.loads(line.removeprefix("input_check: "))
        for line in log.splitlines() if line.startswith("input_check: ")
    ]
    return result, log, checks


def _run_dem(workdir, policy=None, *, band=None, input_preflight=None, cancel_requested=None, low_level=False):
    parameters = {"z_factor": 1}
    if band is not None:
        parameters["band"] = band
    return _run(
        workdir, _dem_contract(policy), {"dem": "dem.tif"}, {"unit_report": "units.json"},
        parameters=parameters, input_preflight=input_preflight, cancel_requested=cancel_requested, low_level=low_level,
    )


def _assert_blocked(workdir, result, log, field, *, code="METHOD_PRECONDITION_FAILED"):
    assert result.status == "FAILED" and result.exit_code is None
    assert (result.error.code, result.error.phase, result.error.field) == (code, "preflight", field)
    assert not any(line.startswith("$ ") for line in log.splitlines())
    assert not (workdir / "step/python-result.json").exists()
    assert not list((workdir / "outputs").iterdir())


@pytest.mark.parametrize(("role", "policy"), [
    ("absent", {"mode": "allow", "bands": [1]}),
    ("scope", {"mode": "allow", "bands": [1]}),
    ("dem", {"mode": "unknown", "bands": [1]}),
    ("dem", {"mode": "allow", "bands": [], "nodata_declaration": "none"}),
    ("dem", {"mode": "allow", "bands": [0]}),
    ("dem", {"mode": "allow", "bands": ["1"]}),
    ("dem", {"mode": "allow", "bands": [True]}),
    ("dem", {"mode": "allow", "bands": [1, 1]}),
    ("dem", {"mode": "allow", "bands": [1], "band_parameter": "band"}),
    ("dem", {"mode": "allow"}),
    ("dem", {"mode": "allow", "band_parameter": "missing"}),
    ("dem", {"mode": "allow", "band_parameter": "vertical_unit"}),
    ("dem", {"mode": "allow", "band_parameter": "optional_band"}),
    ("dem", {"mode": "allow", "bands": [1], "nodata_declaration": "0"}),
])
def test_contract_rejects_ambiguous_roles_selectors_and_policies(role, policy):
    document = _document(DEM)
    document["inputs"]["scope"] = "vector_polygon"
    document["parameters"]["optional_band"] = {"type": "integer", "required": False}
    document["raster_nodata"] = {role: policy}

    with pytest.raises(ValidationError, match="raster_nodata"):
        ToolContract.model_validate(document)


@pytest.mark.parametrize("explicit", [False, True])
def test_legacy_default_and_explicit_allow_preserve_completely_empty_dem(tmp_path, explicit):
    source = tmp_path / "dem.tif"
    _write_tiff(source, np.full((1, 2, 2), np.nan), nodata=np.nan)
    policy = {"mode": "allow", "bands": "all"} if explicit else None
    contract = _dem_contract(policy)
    assert bool(contract.raster_nodata) is explicit
    assert ToolContract.model_validate_json(contract.model_dump_json()) == contract
    assert ("raster_nodata" in contract.model_dump()) is explicit

    result, log, checks = _run_dem(tmp_path, policy)

    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    assert "exit_code: 0" in log and (tmp_path / "step/python-result.json").is_file()
    assert len(checks) == int(explicit)
    if explicit:
        assert checks[0]["observed"]["nodata"] == "nan"
        assert checks[0]["observed"]["scanned_pixels"] == 0
        assert checks[0]["observed"]["valid_pixels"] is None


@pytest.mark.parametrize("low_level", [False, True], ids=("unified-runner", "python-adapter"))
def test_selected_band_blocks_finite_sentinel_in_late_window(tmp_path, low_level):
    source = tmp_path / "dem.tif"
    samples = np.zeros((2, 257, 257), dtype="int16")
    samples[0, :, :] = -9999
    samples[1, 256, 256] = -9999
    _write_tiff(source, samples, nodata=-9999)
    original = fingerprint_file(source)

    result, log, checks = _run_dem(
        tmp_path, {"mode": "reject_invalid_samples", "band_parameter": "band"}, band=2, low_level=low_level,
    )

    _assert_blocked(tmp_path, result, log, "inputs.dem.bands.2.nodata")
    assert fingerprint_file(source) == original
    assert len(checks) == 1 and checks[0]["rule"] == "raster_nodata"
    observed = result.error.observed
    assert observed == checks[0]["observed"]
    assert observed["input_type"] == "raster_elevation" and observed["dtype"] == "int16"
    assert observed["nodata"] == -9999 and observed["mask_flags"] == ["nodata"]
    assert observed["scanned_pixels"] == 257 * 257 and observed["valid_pixels"] == 257 * 257 - 1
    assert observed["first_invalid"] == {"row": 256, "column": 256, "reason": "gdal_mask"}


def test_selected_default_band_and_valid_zero_pass_with_spatial_preflight(tmp_path):
    samples = np.zeros((2, 2, 3), dtype="int16")
    samples[1, :, :] = -9999
    _write_tiff(tmp_path / "dem.tif", samples, nodata=-9999)
    required = CoverageRequirement((500000, 4399980, 500030, 4400000), CRS)

    result, log, checks = _run_dem(
        tmp_path, {"mode": "reject_invalid_samples", "band_parameter": "band"},
        input_preflight=lambda step: preflight_step_inputs(step, required_coverage={"dem": required}),
    )

    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    assert checks[-1]["field"] == "inputs.dem.bands.1.nodata"
    assert checks[-1]["observed"]["valid_pixels"] == 6
    assert {"source_is_readable", "crs_is_known", "data_covers_required_extent", "raster_nodata"} <= {
        row["rule"] for row in checks
    }
    assert all(row["passed"] for row in checks)
    assert log.index('"rule":"raster_nodata"') < log.index("$ ")
    unit_report = json.loads((tmp_path / "outputs/units.json").read_text())
    assert unit_report["band"] == 1 and unit_report["z_factor"] == 1


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf])
def test_nonfinite_values_without_a_nodata_tag_are_invalid(tmp_path, value):
    _write_tiff(tmp_path / "dem.tif", np.array([[[0, value]]], dtype="float64"))

    result, log, checks = _run_dem(tmp_path, {"mode": "reject_invalid_samples", "bands": [1]})

    _assert_blocked(tmp_path, result, log, "inputs.dem.bands.1.nodata")
    assert checks[0]["observed"]["nodata"] is None
    assert checks[0]["observed"]["first_invalid"] == {"row": 0, "column": 1, "reason": "nonfinite"}


@pytest.mark.parametrize("internal", [False, True])
def test_real_gdal_mask_has_priority_over_the_numeric_tag(tmp_path, internal):
    source = tmp_path / "dem.tif"
    samples = np.array([[[-9999, 5]]], dtype="int16")
    _write_tiff(source, samples, nodata=-9999, mask=[[True, False]], internal_mask=internal)
    assert source.with_suffix(".tif.msk").exists() is not internal
    original = fingerprint_file(source)

    result, log, checks = _run_dem(tmp_path, {"mode": "reject_invalid_samples", "bands": [1]})

    _assert_blocked(tmp_path, result, log, "inputs.dem.bands.1.nodata")
    assert checks[0]["observed"]["mask_flags"] == ["per_dataset"]
    assert checks[0]["observed"]["valid_pixels"] == 1
    assert checks[0]["observed"]["first_invalid"] == {"row": 0, "column": 1, "reason": "gdal_mask"}
    assert fingerprint_file(source) == original


@pytest.mark.parametrize("empty_second_band", [False, True])
def test_at_least_one_valid_sample_is_required_per_selected_band(tmp_path, empty_second_band):
    samples = np.full((2, 257, 2), np.nan)
    samples[0, 256, 1] = 0
    if not empty_second_band:
        samples[1, 256, 1] = 42
    _write_tiff(tmp_path / "dem.tif", samples, nodata=np.nan)

    result, log, checks = _run_dem(tmp_path, {"mode": "require_valid_samples", "bands": "all"})

    assert [row["observed"]["scanned_pixels"] for row in checks] == [514, 514]
    assert [row["observed"]["valid_pixels"] for row in checks] == [1, int(not empty_second_band)]
    assert [row["passed"] for row in checks] == [True, not empty_second_band]
    if empty_second_band:
        _assert_blocked(tmp_path, result, log, "inputs.dem.bands.2.nodata")
        assert "no finite samples" in result.error.message
    else:
        assert result.status == "SUCCEEDED" and result.exit_code == 0, result


def test_missing_band_and_unreadable_file_stop_before_the_child(tmp_path):
    for label in ("missing-band", "not-a-raster"):
        workdir = tmp_path / label
        workdir.mkdir()
        path = workdir / "dem.tif"
        if label == "missing-band":
            _write_tiff(path, np.ones((1, 2, 2), dtype="int16"))
            field, code = "inputs.dem.bands.2", "METHOD_PRECONDITION_FAILED"
        else:
            path.write_text("This is not a GeoTIFF", encoding="utf-8")
            field, code = "inputs.dem.path", "SOURCE_UNREADABLE"
        result, log, _ = _run_dem(workdir, {"mode": "reject_invalid_samples", "bands": [2]})
        _assert_blocked(workdir, result, log, field, code=code)


def test_actual_cog_uses_its_stored_mask_and_sample_type(tmp_path):
    source = tmp_path / "source.tif"
    cog = tmp_path / "dem.tif"
    _write_tiff(source, np.array([[[0, 255, 2]]], dtype="uint8"), nodata=255)
    copy_raster(source, cog, driver="COG", blocksize=128)
    with rasterio.open(cog) as dataset:
        assert dataset.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"

    result, log, checks = _run_dem(tmp_path, {"mode": "reject_invalid_samples", "bands": [1]})

    _assert_blocked(tmp_path, result, log, "inputs.dem.bands.1.nodata")
    assert checks[0]["observed"]["dtype"] == "uint8" and checks[0]["observed"]["valid_pixels"] == 2


def test_cancellation_is_observed_between_raster_windows(tmp_path):
    _write_tiff(tmp_path / "dem.tif", np.ones((1, 257, 257), dtype="int16"))
    cancellations = iter((False, False, True))

    result, log, _ = _run_dem(
        tmp_path, {"mode": "reject_invalid_samples", "bands": [1]},
        cancel_requested=lambda: next(cancellations),
    )

    assert result.status == "CANCELLED" and result.exit_code is None
    assert (result.error.code, result.error.phase) == ("PROCESS_CANCELLED", "preflight")
    assert not any(line.startswith("$ ") for line in log.splitlines())
    assert not (tmp_path / "step/python-result.json").exists()


def _ndvi_inputs(workdir, *, bad_metadata):
    inputs = {}
    for period in ("first", "second"):
        lineage = {
            "period": period, "source": {"path": f"inputs/{period}.tif", "fingerprint": "a" * 64},
            "product_id": period, "granule_id": None,
            "product_key": {"sensor": "landsat-8-oli-tirs", "product_level": "L2SP", "processing_version": "02"},
            "configuration": {"id": "landsat-8-l2sp-c2", "version": 1},
        }
        reflectance_role = f"{period}_reflectance"
        quality_role = f"{period}_quality_mask"
        reflectance = np.array([[[0.2, 0.2, np.nan, 0.2]], [[0.6, 0.6, 0.6, 0.6]]])
        quality = np.array([[[1, 0, 1, 1] if period == "first" else [0, 0, 0, 0]]], dtype="uint8")
        for role in (reflectance_role, quality_role):
            inputs[role] = f"{role}.tif"
        _write_tiff(
            workdir / inputs[reflectance_role], reflectance, names=("red", "nir"), unit="reflectance",
            nodata=None if bad_metadata == reflectance_role else np.nan, lineage=lineage,
        )
        _write_tiff(
            workdir / inputs[quality_role], quality, names=("quality_mask",), unit=None,
            nodata=0 if bad_metadata == quality_role else None,
            mask=[[True, True, True, False]] if period == "first" else None, lineage=lineage,
        )
    return inputs


@pytest.mark.parametrize("bad_metadata", [None, "first_reflectance", "second_quality_mask"])
def test_production_ndvi_validates_tags_and_preserves_its_mask_semantics(tmp_path, bad_metadata):
    contract = ToolContract.model_validate(_document(NDVI))
    inputs = _ndvi_inputs(tmp_path, bad_metadata=bad_metadata)
    outputs = {"first_ndvi": "first.tif", "second_ndvi": "second.tif"}

    result, log, checks = _run(tmp_path, contract, inputs, outputs, input_preflight=preflight_step_inputs)

    nodata_checks = [row for row in checks if row["rule"] == "raster_nodata"]
    assert len(nodata_checks) == 6
    assert all(row["observed"]["scanned_pixels"] == 0 for row in nodata_checks)
    assert all(row["observed"]["valid_pixels"] is None for row in nodata_checks)
    if bad_metadata:
        _assert_blocked(tmp_path, result, log, f"inputs.{bad_metadata}.bands.1.nodata")
        assert result.error.observed["input_type"] == contract.inputs[bad_metadata]
    else:
        assert all(row["passed"] for row in nodata_checks)
        assert result.status == "SUCCEEDED" and result.exit_code == 0, result
        with rasterio.open(tmp_path / "outputs/first.tif") as first:
            np.testing.assert_allclose(first.read(1), [[0.5, np.nan, np.nan, np.nan]])
        with rasterio.open(tmp_path / "outputs/second.tif") as second:
            assert np.isnan(second.read(1)).all()
