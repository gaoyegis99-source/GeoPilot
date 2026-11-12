import csv
import json
from pathlib import Path

import geopandas
import pyogrio
import pytest
import yaml
from shapely.geometry import Point

from geopilot.domain.step_io import StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/fixtures/vector"
TOOL_ID = "python-count-points-by-zone"
IMPLEMENTATION = "geopilot.count_points_by_zone"
STEP_DIR = "steps/aggregate_by_zone"
OUTPUT = "outputs/statistics.csv"
CSV_FIELDS = ("zone_id", "assignment_status", "count", "source_feature_ids")


@pytest.fixture
def golden_inputs():
    source = GOLDEN / "road-buffer.gpkg"
    points = pyogrio.read_dataframe(source, layer="targets")
    zones = pyogrio.read_dataframe(source, layer="zones")
    reference = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))["relations"]["intersects"]
    return points, zones, reference


def _points(identifiers, coordinates):
    return geopandas.GeoDataFrame(
        {"source_feature_id": identifiers}, geometry=[Point(*xy) for xy in coordinates], crs="EPSG:32650"
    )


def _stage_inputs(workdir, targets, originals, zones):
    for directory in ("inputs", "outputs", STEP_DIR):
        (workdir / directory).mkdir(parents=True)
    inputs = {}
    for role, frame, geometry_type in (
        ("targets", targets, "Point"),
        ("original_targets", originals, "Point"),
        ("zones", zones, "Polygon"),
    ):
        path = f"inputs/{role}.gpkg"
        pyogrio.write_dataframe(frame, workdir / path, layer=role, geometry_type=geometry_type)
        inputs[role] = {"path": path, "layer": role}
    return inputs


def _run(workdir, inputs):
    path = ROOT / f"config/tools/{TOOL_ID}.yaml"
    contract = ToolContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    registry = ToolRegistry(python_implementations=[IMPLEMENTATION])
    registry.register(contract, required_modules=("geopandas", "pyogrio", "pyproj", "shapely"))
    request = StepRequest(
        run_id="road-count", step_key="aggregate_by_zone", tool={"id": TOOL_ID, "version": 1},
        workdir_ref="road-count", inputs=inputs,
        parameters={"boundary_assignment": "centroid", "boundary_tie": "unassigned", "statistic": "count"},
        output_targets={"statistics": {"path": OUTPUT}}, log_path=f"{STEP_DIR}/process.log",
        cancel_path="cancel.flag",
    )
    result = run_step(
        request, registry=registry, workdir=workdir,
        python_implementations={IMPLEMENTATION: ROOT / "src/geopilot/gis/tools/count_points_by_zone.py"},
    )
    return request, result


def _read_rows(workdir, result):
    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    assert result.outputs["statistics"].model_dump() == {"path": OUTPUT, "layer": None, "type": "table_csv"}
    with (workdir / OUTPUT).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        assert tuple(reader.fieldnames) == CSV_FIELDS
        return [
            (row["zone_id"], row["assignment_status"], int(row["count"]), json.loads(row["source_feature_ids"]))
            for row in reader
        ]


def _assert_child_failure(workdir, result, *messages):
    assert result.status == "FAILED" and result.exit_code == 1, result
    assert result.outputs == {}
    assert result.error.code == "PROCESS_FAILED"
    log = (workdir / result.log_path).read_text(encoding="utf-8")
    assert all(message in log for message in messages), log
    assert not (workdir / OUTPUT).exists()


def test_real_golden_point_count_matches_ids_and_three_two_one(tmp_path, golden_inputs):
    points, zones, reference = golden_inputs
    targets = points[points.source_feature_id.isin(reference["clipped_target_ids"])].iloc[::-1]
    originals = points[points.source_feature_id.isin(reference["before_clip_target_ids"])]
    inputs = _stage_inputs(tmp_path, targets, originals, zones.iloc[::-1])
    before = {ref["path"]: (tmp_path / ref["path"]).read_bytes() for ref in inputs.values()}

    request, result = _run(tmp_path, inputs)

    rows = _read_rows(tmp_path, result)
    assert rows == [
        ("zone-east", "assigned", 2, reference["zone_target_ids"]["zone-east"]),
        ("zone-west", "assigned", 3, reference["zone_target_ids"]["zone-west"]),
        ("", "unassigned", 1, reference["unassigned_target_ids"]),
    ]
    assert sorted(identifier for _, _, _, ids in rows for identifier in ids) == reference["clipped_target_ids"]
    assert sum(count for _, _, count, _ in rows) == reference["counts"]["total"]
    assert all((tmp_path / path).read_bytes() == content for path, content in before.items())

    saved_request = StepRequest.model_validate_json((tmp_path / STEP_DIR / "request.json").read_text(encoding="utf-8"))
    assert saved_request == request
    assert StepResult.model_validate_json((tmp_path / STEP_DIR / "result.json").read_text(encoding="utf-8")) == result
    child = json.loads((tmp_path / STEP_DIR / "python-result.json").read_text(encoding="utf-8"))
    assert child["outputs"] == {"statistics": {"path": OUTPUT}}
    assert result.environment["pyogrio"] == pyogrio.__version__
    log = (tmp_path / request.log_path).read_text(encoding="utf-8")
    assert "count_points_by_zone.py" in log and "exit_code: 0" in log


def test_retained_ids_use_original_locations_and_exact_duplicates_count_once(tmp_path, golden_inputs):
    _, zones, _ = golden_inputs
    ids = ["moved", "moved", "outside", "one-zone-boundary"]
    targets = _points(ids, [(501500, 4400500)] * 2 + [(499999, 4400500), (500000, 4400500)])
    originals = _points(ids, [(500500, 4400500)] * 2 + [(499999, 4400500), (500000, 4400500)])
    inputs = _stage_inputs(tmp_path, targets, originals, zones)

    _, result = _run(tmp_path, inputs)

    assert _read_rows(tmp_path, result) == [
        ("zone-east", "assigned", 0, []),
        ("zone-west", "assigned", 2, ["moved", "one-zone-boundary"]),
        ("", "unassigned", 1, ["outside"]),
    ]


def test_empty_clipped_layer_reports_all_zones_with_zero_counts(tmp_path, golden_inputs):
    points, zones, _ = golden_inputs
    inputs = _stage_inputs(tmp_path, points.iloc[:0], points, zones)

    _, result = _run(tmp_path, inputs)

    assert _read_rows(tmp_path, result) == [
        ("zone-east", "assigned", 0, []), ("zone-west", "assigned", 0, []), ("", "unassigned", 0, []),
    ]


@pytest.mark.parametrize("role", ["targets", "original_targets"])
def test_same_id_with_conflicting_locations_fails(tmp_path, golden_inputs, role):
    _, zones, _ = golden_inputs
    point = _points(["repeat"], [(500500, 4400500)])
    conflict = _points(["repeat", "repeat"], [(500500, 4400500), (501500, 4400500)])
    inputs = _stage_inputs(
        tmp_path, conflict if role == "targets" else point, conflict if role == "original_targets" else point, zones
    )

    _, result = _run(tmp_path, inputs)

    _assert_child_failure(tmp_path, result, role, "repeat", "conflicting locations")


def test_clipped_id_missing_from_original_targets_fails(tmp_path, golden_inputs):
    _, zones, _ = golden_inputs
    targets = _points(["missing"], [(500500, 4400500)])
    originals = _points(["other"], [(500500, 4400500)])
    inputs = _stage_inputs(tmp_path, targets, originals, zones)

    _, result = _run(tmp_path, inputs)

    _assert_child_failure(tmp_path, result, "original_targets", "missing")


def test_multilayer_geopackage_requires_a_zone_layer_name(tmp_path, golden_inputs):
    points, zones, _ = golden_inputs
    inputs = _stage_inputs(tmp_path, points, points, zones)
    pyogrio.write_dataframe(zones, tmp_path / inputs["zones"]["path"], layer="other_zones")
    del inputs["zones"]["layer"]

    _, result = _run(tmp_path, inputs)

    _assert_child_failure(tmp_path, result, "zones", "Select one GeoPackage layer", "other_zones")


@pytest.mark.parametrize("crs", [None, "EPSG:4326"], ids=["undefined", "mismatched"])
@pytest.mark.filterwarnings("ignore:'crs' was not provided:UserWarning")
def test_requires_known_matching_crs(tmp_path, golden_inputs, crs):
    points, zones, _ = golden_inputs
    zones = zones.set_crs(None, allow_override=True) if crs is None else zones.to_crs(crs)
    inputs = _stage_inputs(tmp_path, points, points, zones)

    _, result = _run(tmp_path, inputs)

    _assert_child_failure(tmp_path, result, "zones", "CRS", "known" if crs is None else "match")
