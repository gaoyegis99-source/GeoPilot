import json
import shutil
from pathlib import Path

import geopandas as gpd
import pyogrio
import pytest
import yaml
from shapely.geometry import Point, Polygon, box

from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepRequest, StepResult
from geopilot.domain.tool_contract import ToolContract
from geopilot.execution.runner import run_step
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests/fixtures/vector"
STEP = "clip_back_to_analysis_extent"
CRS = "EPSG:32650"


@pytest.fixture
def tool():
    path = ROOT / "config/tools/python-clip-points-to-extent.yaml"
    return ToolContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@pytest.fixture
def workspace(tmp_path):
    for directory in ("inputs", "outputs", "intermediate", f"steps/{STEP}"):
        (tmp_path / directory).mkdir(parents=True)
    return tmp_path


def _run_clip(workspace, tool, *, extent_layer="analysis_extent", original_layer="original_points"):
    registry = ToolRegistry(python_implementations=(tool.implementation,))
    registry.register(tool, required_modules=("pyogrio", "geopandas", "pyproj", "shapely"))
    worker = ROOT / "src/geopilot/gis/tools/clip_points_to_extent.py"
    request = StepRequest(
        run_id="clip-points", step_key=STEP, tool=tool.ref, workdir_ref="clip-points",
        inputs={
            "source": {"path": "inputs/source.gpkg", "layer": "selected_points"},
            "extent": {"path": "inputs/extent.gpkg", "layer": extent_layer},
        },
        parameters={"output_scope": "clipped_to_analysis_extent"},
        output_targets={
            "targets": {"path": "outputs/targets.gpkg", "layer": "clipped_points"},
            "original_targets": {"path": "intermediate/original_targets.gpkg", "layer": original_layer},
        },
        log_path=f"steps/{STEP}/process.log", cancel_path="cancel.flag",
    )
    result = run_step(
        request, registry=registry, workdir=workspace,
        python_implementations={tool.implementation: worker},
    )
    return request, result


def _write_points(workspace, records, *, fields=None):
    frame = gpd.GeoDataFrame(
        {"source_feature_id": [name for name, _ in records], **(fields or {})},
        geometry=[geometry for _, geometry in records], crs=CRS,
    )
    pyogrio.write_dataframe(
        frame, workspace / "inputs/source.gpkg", layer="selected_points", geometry_type="Point",
        layer_options={"FID": "storage_id", "GEOMETRY_NAME": "storage_geometry"},
    )


def _write_extent(workspace, *, geometries=None, crs=CRS):
    if geometries is None:
        geometries = [box(500000, 4400000, 500010, 4400010)]
    frame = gpd.GeoDataFrame(geometry=geometries, crs=crs)
    pyogrio.write_dataframe(frame, workspace / "inputs/extent.gpkg", layer="analysis_extent")


def _assert_outputs(workspace, result, expected):
    assert result.status == "SUCCEEDED" and result.exit_code == 0, result
    assert set(result.outputs) == {"targets", "original_targets"}
    assert result.outputs["targets"].path != result.outputs["original_targets"].path
    for output in result.outputs.values():
        assert output.type == "vector_point"
        path = workspace / output.path
        assert pyogrio.list_layers(path).tolist() == [[output.layer, "Point"]]
        actual = pyogrio.read_dataframe(path, layer=output.layer).sort_values("source_feature_id")
        assert actual.crs.to_string() == CRS
        assert actual.source_feature_id.tolist() == expected.source_feature_id.tolist()
        assert actual.geometry.to_wkb().tolist() == expected.geometry.to_wkb().tolist()


@pytest.mark.parametrize("relation", ["intersects", "within"])
def test_clip_points_from_golden_before_clip_selection(workspace, tool, relation):
    expected = json.loads((GOLDEN / "expected.json").read_text(encoding="utf-8"))["relations"][relation]
    extent = workspace / "inputs/extent.gpkg"
    shutil.copy2(GOLDEN / "road-buffer.gpkg", extent)
    targets = pyogrio.read_dataframe(extent, layer="targets")
    selected = targets[targets.source_feature_id.isin(expected["before_clip_target_ids"])]
    assert set(selected.source_feature_id) == set(expected["before_clip_target_ids"])
    assert "building-outside-extent" in selected.source_feature_id.tolist()
    source = workspace / "inputs/source.gpkg"
    pyogrio.write_dataframe(targets, source, layer="decoy")
    pyogrio.write_dataframe(selected, source, layer="selected_points")
    before = {path: fingerprint_file(path) for path in (source, extent)}

    request, result = _run_clip(workspace, tool)

    reference = targets[targets.source_feature_id.isin(expected["clipped_target_ids"])].sort_values("source_feature_id")
    assert reference.source_feature_id.tolist() == expected["clipped_target_ids"]
    _assert_outputs(workspace, result, reference)
    for path, fingerprint in before.items():
        assert fingerprint_file(path) == fingerprint

    step = workspace / f"steps/{STEP}"
    manifest = json.loads((step / "python-result.json").read_text(encoding="utf-8"))
    assert manifest["outputs"] == request.model_dump(mode="json")["output_targets"]
    assert all(manifest["environment"].get(name) for name in ("geopandas", "pyogrio", "pyproj", "shapely"))
    assert result.environment["python"]
    assert StepResult.model_validate_json((step / "result.json").read_text(encoding="utf-8")) == result
    log = (workspace / result.log_path).read_text(encoding="utf-8")
    assert "clip_points_to_extent.py --request" in log and "exit_code: 0" in log


def test_clip_includes_extent_boundaries_and_keeps_original_fields(workspace, tool):
    shell = box(500000, 4400000, 500010, 4400010)
    hole = box(500004, 4400004, 500006, 4400006)
    extent = Polygon(shell.exterior.coords, [hole.exterior.coords])
    _write_extent(workspace, geometries=[extent, box(500020, 4400020, 500021, 4400021)])
    _write_points(workspace, [
        ("edge", Point(500000, 4400005)),
        ("edge", Point(500000, 4400005)),
        ("corner", Point(500010, 4400010)),
        ("hole-edge", Point(500004, 4400005)),
        ("in-hole", Point(500005, 4400005)),
        ("in-extent", Point(500002, 4400002)),
        ("other-part", Point(500020.5, 4400020.5)),
        ("outside", Point(500015, 4400015)),
    ], fields={"fid": [11, 11, 22, 33, 44, 55, 66, 77], "geom": list("aabcdefg")})

    _, result = _run_clip(workspace, tool)

    expected_ids = ["corner", "edge", "edge", "hole-edge", "in-extent", "other-part"]
    source = pyogrio.read_dataframe(workspace / "inputs/source.gpkg", layer="selected_points")
    reference = source[source.source_feature_id.isin(expected_ids)].sort_values("source_feature_id")
    assert reference.source_feature_id.tolist() == expected_ids
    _assert_outputs(workspace, result, reference)
    for output in result.outputs.values():
        actual = pyogrio.read_dataframe(workspace / output.path, layer=output.layer).sort_values("source_feature_id")
        assert actual["fid"].tolist() == reference["fid"].tolist()
        assert actual["geom"].tolist() == reference["geom"].tolist()


@pytest.mark.parametrize("records", [[], [("outside", Point(500011, 4400005))]])
def test_empty_result_writes_two_valid_point_layers(workspace, tool, records):
    _write_points(workspace, records)
    _write_extent(workspace)

    request, result = _run_clip(workspace, tool, original_layer=None)

    expected = gpd.GeoDataFrame({"source_feature_id": []}, geometry=[], crs=CRS)
    _assert_outputs(workspace, result, expected)
    assert request.output_targets["original_targets"].layer is None
    assert result.outputs["original_targets"].layer == "original_targets"


@pytest.mark.parametrize("extent_crs", ["EPSG:32651", None])
def test_nonmatching_or_unknown_crs_fails_before_output(workspace, tool, extent_crs):
    _write_points(workspace, [("point", Point(500001, 4400001))])
    if extent_crs is None:
        with pytest.warns(UserWarning, match="'crs' was not provided"):
            _write_extent(workspace, crs=extent_crs)
    else:
        _write_extent(workspace, crs=extent_crs)

    _, result = _run_clip(workspace, tool)

    assert result.status == "FAILED" and result.exit_code == 1
    assert result.error.code == "PROCESS_FAILED"
    log = (workspace / result.log_path).read_text(encoding="utf-8")
    assert ("CRS is unknown" if extent_crs is None else "same CRS") in log
    assert not list((workspace / "outputs").iterdir())
    assert not list((workspace / "intermediate").iterdir())


def test_second_output_write_failure_leaves_no_partial_geopackage(workspace, tool):
    _write_points(workspace, [("point", Point(500001, 4400001))])
    _write_extent(workspace)

    _, result = _run_clip(workspace, tool, original_layer="gpkg_contents")

    assert result.status == "FAILED" and result.exit_code == 1
    assert result.error.code == "PROCESS_FAILED"
    assert not list((workspace / "outputs").iterdir())
    assert not list((workspace / "intermediate").iterdir())
