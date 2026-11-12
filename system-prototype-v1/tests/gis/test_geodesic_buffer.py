import json
import math
from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest
import yaml
from shapely.geometry import LineString, MultiLineString, Point

from geopilot.checks.input_checks import preflight_step_inputs
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.python_runner import run_python
from geopilot.execution.runner import run_step
from geopilot.gis.measurement.geodesic_buffer import MAX_CAP_ANGLE_DEG, MAX_SEGMENT_STEP_M, geodesic_buffer
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
REFERENCE = json.loads((ROOT / "tests/fixtures/geodesic/reference.json").read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in REFERENCE["cases"]}
EQUATORIAL_RADIUS = 6378137.0
FLATTENING = 1 / 298.257223563


def _assert_probes(polygon, case):
    assert polygon.geom_type in {"Polygon", "MultiPolygon"} and polygon.is_valid and not polygon.is_empty
    for probe in case["probes"]:
        lower, upper = probe["distance_bracket_m"]
        assert upper - lower <= REFERENCE["distance_bracket_tolerance_m"]
        if probe["covered"]:
            assert upper < case["distance_m"]
        else:
            assert lower > case["distance_m"]
        assert polygon.covers(Point(probe["point"])) == probe["covered"], probe


@pytest.mark.parametrize("case", REFERENCE["cases"], ids=list(CASES))
def test_wgs84_capsules_match_independent_distance_probes(case):
    assert (MAX_SEGMENT_STEP_M, MAX_CAP_ANGLE_DEG) == (
        REFERENCE["production_sampling"]["max_segment_step_m"],
        REFERENCE["production_sampling"]["max_cap_angle_deg"],
    )
    polygon = geodesic_buffer(LineString(case["line"]), case["distance_m"])

    _assert_probes(polygon, case)
    if case["id"] == "high-latitude-arc":
        assert polygon.covers(Point(case["centerline_point"]))
        assert case["centerline_point"][1] > case["line"][0][1]
        assert case["inside_probe_raw_chord_distance_m"] > case["distance_m"] + 300


def _meridian_distance(latitude_degrees):
    # Integrate the WGS84 meridional radius of curvature independently of PROJ.
    eccentricity_squared = FLATTENING * (2 - FLATTENING)
    step = abs(math.radians(latitude_degrees)) / 32
    terms = []
    for index in range(33):
        latitude = index * step
        radius = EQUATORIAL_RADIUS * (1 - eccentricity_squared) / (
            1 - eccentricity_squared * math.sin(latitude) ** 2
        ) ** 1.5
        weight = 1 if index in (0, 32) else (4 if index % 2 else 2)
        terms.append(weight * radius)
    return math.fsum(terms) * step / 3


@pytest.mark.parametrize("distance", [500, 2000])
def test_equatorial_sides_and_endpoints_match_analytic_ellipsoid(distance):
    case = CASES["equatorial-straight"]
    polygon = geodesic_buffer(LineString(case["line"]), distance)
    west, south, east, north = polygon.bounds
    lon1, lon2 = case["line"][0][0], case["line"][-1][0]
    tolerance = REFERENCE["analytic_boundary_tolerance_m"]

    assert math.radians(lon1 - west) * EQUATORIAL_RADIUS == pytest.approx(distance, abs=tolerance, rel=0)
    assert math.radians(east - lon2) * EQUATORIAL_RADIUS == pytest.approx(distance, abs=tolerance, rel=0)
    assert _meridian_distance(south) == pytest.approx(distance, abs=tolerance, rel=0)
    assert _meridian_distance(north) == pytest.approx(distance, abs=tolerance, rel=0)


def test_multipart_accepts_consecutive_duplicate_vertices_and_keeps_disjoint_parts():
    line = MultiLineString([
        [(117, 0), (117, 0), (117.01, 0)], [(117.05, 0), (117.06, 0)],
    ])
    polygon = geodesic_buffer(line, 500)

    assert polygon.geom_type == "MultiPolygon" and polygon.is_valid and len(polygon.geoms) == 2
    assert polygon.covers(Point(117.005, 0)) and polygon.covers(Point(117.055, 0))
    assert not polygon.covers(Point(117.03, 0))


@pytest.mark.parametrize("coordinates,reason", [
    ([(179.99, 0), (-179.99, 0)], "antimeridian"),
    ([(179.995, 0), (179.999, 0)], "antimeridian"),
    ([(117, 75.1), (117.01, 75.1)], "latitude"),
    ([(117, 74.999), (117.001, 74.999)], "latitude"),
    ([(117, 0), (118, 0)], "segment length"),
    ([(117, 0), (117.8, 0), (117, 0), (117.8, 0)], "total length"),
    ([(117, 0, 5), (117.01, 0, 5)], "2D"),
])
def test_unsupported_line_extents_or_dimensions_are_rejected(coordinates, reason):
    with pytest.raises(ValueError, match=reason):
        geodesic_buffer(LineString(coordinates), 500)


@pytest.mark.parametrize("distance", [0, 0.5, 2001, math.inf, True])
def test_distance_outside_contract_is_rejected(distance):
    with pytest.raises(ValueError, match="distance"):
        geodesic_buffer(LineString(CASES["equatorial-straight"]["line"]), distance)


def _worker_request(
    workdir, *, dissolve, epsg=4326, case_ids=("equatorial-straight", "equatorial-bend"), tool_changes=None,
):
    config = ROOT / "config/tools/python-wgs84-geodesic-buffer.yaml"
    contract = ToolContract.model_validate(yaml.safe_load(config.read_text(encoding="utf-8")))
    if tool_changes is not None:
        contract = contract.model_copy(update=tool_changes)
    assert contract.measurement_policy.distance == "geodesic"
    worker = ROOT / "src/geopilot/gis/tools/wgs84_geodesic_buffer.py"
    registry = ToolRegistry(python_implementations=[contract.implementation])
    registry.register(contract, required_modules=("pyproj", "shapely", "numpy", "geopandas", "pyogrio"))
    for directory in ("inputs", "outputs", "steps/geodesic_buffer"):
        (workdir / directory).mkdir(parents=True)
    records = [CASES[name] for name in case_ids]
    frame = gpd.GeoDataFrame(
        {"source_feature_id": [case["id"] for case in records]},
        geometry=[LineString(case["line"]) for case in records], crs=4326,
    ).to_crs(epsg)
    source = workdir / "inputs/roads.gpkg"
    pyogrio.write_dataframe(frame, source, layer="roads")
    request = StepRequest(
        run_id="geodesic-reference", step_key="geodesic_buffer", tool=contract.ref, workdir_ref="geodesic-reference",
        inputs={"source": {"path": "inputs/roads.gpkg", "layer": "roads"}},
        parameters={"distance": {"value": records[0]["distance_m"] / 1000, "unit": "kilometer"}, "dissolve": dissolve},
        output_targets={"buffer": {"path": "outputs/roads-buffer.gpkg", "layer": "geodesic"}},
        log_path="steps/geodesic_buffer/process.log", cancel_path="cancel.flag",
    )
    return request, source, registry, {contract.implementation: worker}


@pytest.mark.parametrize("dissolve", [False, True])
def test_real_python_child_preserves_crs_and_matches_fixed_probes(tmp_path, dissolve):
    request, source, registry, implementations = _worker_request(tmp_path, dissolve=dissolve)
    before = fingerprint_file(source)

    result = StepResult.model_validate(run_python(
        request.model_dump(mode="json"), registry=registry, workdir=tmp_path, implementations=implementations,
    ))

    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    output = result.outputs["buffer"]
    assert (output.path, output.layer, output.type) == ("outputs/roads-buffer.gpkg", "geodesic", "vector_polygon")
    frame = pyogrio.read_dataframe(tmp_path / output.path, layer=output.layer)
    assert frame.crs.to_epsg() == 4326
    if dissolve:
        assert len(frame) == 1
        _assert_probes(frame.geometry.item(), CASES["equatorial-bend"])
    else:
        assert frame.source_feature_id.tolist() == ["equatorial-straight", "equatorial-bend"]
        for row in frame.itertuples():
            _assert_probes(row.geometry, CASES[row.source_feature_id])
    assert fingerprint_file(source) == before
    step_dir = tmp_path / "steps/geodesic_buffer"
    saved = StepRequest.model_validate_json((step_dir / "request.json").read_text(encoding="utf-8"))
    assert saved.parameters["distance"] == 500 and saved.tool == request.tool
    manifest = json.loads((step_dir / "python-result.json").read_text(encoding="utf-8"))
    assert manifest["outputs"]["buffer"] == {"path": output.path, "layer": output.layer}
    assert result.environment["pyproj"] and result.environment["shapely"]
    log = (tmp_path / result.log_path).read_text(encoding="utf-8")
    assert "wgs84_geodesic_buffer.py --request" in log and "WGS84" in log and "exit_code: 0" in log


def test_real_worker_rejects_projected_crs_without_publishing(tmp_path):
    request, source, registry, implementations = _worker_request(tmp_path, dissolve=False, epsg=32650)
    before = fingerprint_file(source)

    result = StepResult.model_validate(run_python(
        request.model_dump(mode="json"), registry=registry, workdir=tmp_path, implementations=implementations,
    ))

    assert result.status == "FAILED" and result.exit_code == 1
    assert "WGS84" in (tmp_path / result.log_path).read_text(encoding="utf-8")
    assert not list((tmp_path / "outputs").iterdir())
    assert fingerprint_file(source) == before


def test_unified_run_step_passes_wgs84_contract_through_input_checks_and_real_worker(tmp_path):
    case = CASES["high-latitude-arc"]
    request, source, registry, implementations = _worker_request(tmp_path, dissolve=False, case_ids=(case["id"],))
    before = fingerprint_file(source)

    result = run_step(
        request, registry=registry, workdir=tmp_path, python_implementations=implementations,
        input_preflight=preflight_step_inputs,
    )

    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    output = result.outputs["buffer"]
    actual = pyogrio.read_dataframe(tmp_path / output.path, layer=output.layer)
    assert actual.crs.to_epsg() == 4326 and actual.source_feature_id.tolist() == [case["id"]]
    _assert_probes(actual.geometry.item(), case)
    saved = StepRequest.model_validate_json((tmp_path / "steps/geodesic_buffer/request.json").read_text(encoding="utf-8"))
    assert saved.parameters["distance"] == 1000
    assert fingerprint_file(source) == before
    log = (tmp_path / result.log_path).read_text(encoding="utf-8")
    audits = [json.loads(line.removeprefix("measurement: ")) for line in log.splitlines() if line.startswith("measurement: ")]
    assert audits == [{"role": "source", "method": "geodesic", "crs": "EPSG:4326", "unit": "meter"}]
    input_records = [json.loads(line.removeprefix("input_check: ")) for line in log.splitlines() if line.startswith("input_check: ")]
    assert next(row for row in input_records if row["rule"] == "geodesic_measurement_is_supported")["passed"]
    assert "wgs84_geodesic_buffer.py --request" in log and "exit_code: 0" in log


@pytest.mark.parametrize("input_preflight", [None, preflight_step_inputs], ids=["unified", "input-check"])
def test_other_contract_declaring_geodesic_cannot_dispatch(tmp_path, input_preflight):
    request, _, registry, implementations = _worker_request(
        tmp_path, dissolve=False, tool_changes={"id": "other-geodesic-buffer"},
    )

    result = run_step(
        request, registry=registry, workdir=tmp_path, python_implementations=implementations,
        input_preflight=input_preflight,
    )

    assert result.status == "FAILED" and result.exit_code is None
    assert result.error.code == "MEASUREMENT_POLICY_REJECTED"
    log = (tmp_path / result.log_path).read_text(encoding="utf-8")
    assert "Geodesic distance is unavailable" in log and not any(line.startswith("$ ") for line in log.splitlines())
    assert not list((tmp_path / "outputs").iterdir())


def test_unified_run_step_blocks_projected_geodesic_input_before_child(tmp_path):
    request, _, registry, implementations = _worker_request(tmp_path, dissolve=False, epsg=32650)

    result = run_step(
        request, registry=registry, workdir=tmp_path, python_implementations=implementations,
        input_preflight=preflight_step_inputs,
    )

    assert result.status == "FAILED" and result.exit_code is None
    assert result.error.code == "MEASUREMENT_POLICY_REJECTED" and result.error.field == "inputs.source.crs"
    assert "WGS84" in result.error.message and not list((tmp_path / "outputs").iterdir())
