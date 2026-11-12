from pathlib import Path

import pytest

from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.tool_contract import ToolContract
from geopilot.gis.measurement.coverage import MeasurementPolicyError
from geopilot.gis.measurement.projection import validate_step_area


DEM = Path(__file__).resolve().parents[2] / "fixtures/raster/dem-plane.tif"


@pytest.fixture
def contract():
    return ToolContract.model_validate({
        "id": "python-dem-area-stats", "version": 1, "runner": "python",
        "implementation": "geopilot.summarize_dem_area",
        "inputs": {"threshold_regions": "raster_classification"}, "outputs": {"area_statistics": "json"},
        "parameters": {"area_crs": {"type": "string"}},
        "measurement_policy": {"area": "equal_area", "distance": "not_applicable"},
        "preconditions": ["crs_is_known"], "check_rules": ["output_exists"],
    })


def test_exact_raster_area_tool_validates_utm_input_in_the_requested_equal_area_crs(contract):
    source = read_raster_metadata(DEM)
    decision = validate_step_area(source, contract, field="inputs.threshold_regions.crs", target_crs="EPSG:6933")
    assert source.crs == "EPSG:32650"
    assert (decision.method, decision.crs, decision.unit) == ("equal_area", "EPSG:6933", "square_meter")


def test_rejects_non_equal_area_target_and_other_contract_versions(contract):
    source = read_raster_metadata(DEM)
    with pytest.raises(MeasurementPolicyError) as bad_target:
        validate_step_area(source, contract, field="inputs.threshold_regions.crs", target_crs="EPSG:3857")
    assert bad_target.value.field == "parameters.area_crs"
    with pytest.raises(MeasurementPolicyError) as other_tool:
        validate_step_area(source, contract.model_copy(update={"version": 2}),
                           field="inputs.threshold_regions.crs", target_crs="EPSG:6933")
    assert other_tool.value.field == "inputs.threshold_regions.crs"


def test_area_target_is_declarative_for_a_tool_that_projects_original_cells(contract):
    tool = ToolContract.model_validate({
        **contract.model_dump(mode="json"),
        "id": "python-remote-sensing-calculate-change",
        "implementation": "geopilot.remote_sensing_workflow",
        "inputs": {"aligned_ndvi": "raster_ndvi_pair"},
        "preconditions": ["crs_is_known", "area_projected_from_raster_cells"],
    })
    source = read_raster_metadata(DEM)
    field = "inputs.aligned_ndvi.crs"
    decision = validate_step_area(source, tool, field=field, target_crs="EPSG:6933")
    assert (decision.method, decision.crs, decision.unit) == ("equal_area", "EPSG:6933", "square_meter")
    with pytest.raises(MeasurementPolicyError) as invalid:
        validate_step_area(source, tool, field=field, target_crs="EPSG:3857")
    assert invalid.value.field == "parameters.area_crs"
