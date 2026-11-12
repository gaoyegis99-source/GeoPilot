import json
import math
from pathlib import Path

import pytest
import rasterio
import yaml

from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.step_io import StepRequest
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.gis.raster.units import DemUnitError, resolve_dem_units
from geopilot.gis.validation.crs_validator import CRSValidationError
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[3]
DEM = ROOT / "tests/fixtures/raster/dem-plane.tif"
WORKER = ROOT / "src/geopilot/gis/tools/check_dem_units.py"
CONTRACT = ROOT / "config/tools/python-dem-unit-check.yaml"


def _real_dem(target, *, crs, unit=None):
    with rasterio.open(DEM) as source:
        profile = source.profile.copy()
        profile.update(crs=crs)
        with rasterio.open(target, "w", **profile) as destination:
            destination.write(source.read())
            if unit is not None:
                destination.set_band_unit(1, unit)
    return read_raster_metadata(target)


def test_p0_dem_has_both_metre_units():
    units = resolve_dem_units(read_raster_metadata(DEM))
    assert units.horizontal_unit == "metre"
    assert units.vertical_unit == "meter" and units.vertical_source == "band"
    assert units.horizontal_metres_per_unit == units.vertical_metres_per_unit == units.z_factor == 1


def test_real_foot_grid_and_height_unit_have_explicit_ratio(tmp_path):
    metadata = _real_dem(tmp_path / "feet.tif", crs="EPSG:2230", unit="m")
    result = resolve_dem_units(metadata)
    assert result.horizontal_metres_per_unit == pytest.approx(1200 / 3937)
    assert result.vertical_metres_per_unit == 1
    assert result.z_factor == pytest.approx(3937 / 1200)


def test_compound_crs_declares_elevation_unit(tmp_path):
    metadata = _real_dem(tmp_path / "compound.tif", crs="EPSG:7405")
    result = resolve_dem_units(metadata)
    assert result.vertical_source == "vertical_crs"
    assert result.z_factor == 1


def test_unknown_units_require_explicit_input_and_conflicts_fail(tmp_path):
    metadata = _real_dem(tmp_path / "unknown.tif", crs="EPSG:32650")
    with pytest.raises(DemUnitError) as missing:
        resolve_dem_units(metadata)
    assert missing.value.code == "DEM_VERTICAL_UNIT_REQUIRED"
    units = resolve_dem_units(metadata, vertical_unit="foot")
    assert units.z_factor == pytest.approx(0.3048) and units.vertical_source == "explicit"
    with pytest.raises(DemUnitError) as conflict:
        resolve_dem_units(read_raster_metadata(DEM), vertical_unit="foot")
    assert conflict.value.code == "DEM_UNIT_CONFLICT"
    with pytest.raises(DemUnitError):
        resolve_dem_units(metadata, vertical_unit="degree")
    with pytest.raises(DemUnitError):
        resolve_dem_units(metadata, band=True)


@pytest.mark.parametrize("crs", [None, "EPSG:4326", "EPSG:3857"])
def test_unknown_angular_or_web_mercator_grid_is_blocked(tmp_path, crs):
    metadata = _real_dem(tmp_path / "invalid.tif", crs=crs, unit="m")
    with pytest.raises(CRSValidationError) as failure:
        resolve_dem_units(metadata)
    assert failure.value.field == "inputs.dem.crs"


@pytest.mark.parametrize("matching", [True, False])
def test_actual_unit_worker_audits_confirmed_conversion(tmp_path, matching):
    for folder in ("inputs", "intermediate", "steps/check_units"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    metadata = _real_dem(tmp_path / "inputs/dem.tif", crs="EPSG:32650", unit="ft")
    expected = resolve_dem_units(metadata)
    ratio = expected.z_factor if matching else 1.0
    tool = ToolContract.model_validate(yaml.safe_load(CONTRACT.read_text()))
    registry = ToolRegistry(python_implementations={tool.implementation: WORKER})
    registry.register(tool)
    request = StepRequest(
        run_id="dem-units-smoke", step_key="check_units", tool=tool.ref, workdir_ref="dem-units-smoke",
        inputs={"dem": {"path": "inputs/dem.tif"}},
        parameters={"band": 1, "vertical_unit": "metadata", "z_factor": ratio},
        output_targets={"unit_report": {"path": "intermediate/units.json"}},
        log_path="steps/check_units/process.log", cancel_path="cancel.flag",
    )
    result = run_step(request, registry=registry, workdir=tmp_path, python_implementations={tool.implementation: WORKER})
    recorded = json.loads((tmp_path / "steps/check_units/request.json").read_text())
    assert math.isclose(recorded["parameters"]["z_factor"], ratio)
    log = (tmp_path / result.log_path).read_text()
    if matching:
        assert result.status == "SUCCEEDED" and result.exit_code == 0
        report = json.loads((tmp_path / result.outputs["unit_report"].path).read_text())
        assert report["z_factor"] == pytest.approx(0.3048)
        assert report["vertical_unit"] == "foot"
        assert '"z_factor": 0.3048' in log
    else:
        assert result.status == "FAILED" and result.exit_code != 0
        assert "does not match the DEM units" in log
        assert not (tmp_path / "intermediate/units.json").exists()
