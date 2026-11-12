import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pyogrio
import pyproj
import pytest
import rasterio
import shapely
from fastapi.testclient import TestClient

from geopilot.api.server import create_local_app
from scripts.methods.reference_package import OutputFile, ReferencePackageError, compare_outputs, load_reference_package


FIXTURES = Path(__file__).parent / "fixtures"
PACKAGES = ("attribute-filter", "dem-terrain")


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_files(package):
    return {path.relative_to(package.root).as_posix(): _sha256(path) for path in package.root.rglob("*") if path.is_file()}


def _response(response, code=200):
    assert response.status_code == code, response.text
    return response.json()


def _draft(client, package, directory, case_id=None):
    stage = directory / "staged"
    stage.mkdir()
    inputs = {}
    for role, item in package.case_inputs(case_id).items():
        source = package.resolve_input(role, case_id)
        copy = stage / f"{role}{source.suffix}"
        shutil.copyfile(source, copy)
        assert _sha256(copy) == item.sha256
        asset = _response(client.post("/api/assets", json={"path": str(copy), "layer": item.layer}))
        inputs[role] = {key: asset[key] for key in ("asset_id", "asset_version")}
    task = _response(client.post("/api/tasks", json={"goal": {"summary": package.case_id}}), 201)["task_id"]
    request = {
        "method": package.method.model_dump(mode="json"),
        "inputs": inputs,
        "critical_options": package.case_options(case_id),
    }
    return _response(client.post(f"/api/tasks/{task}/plans", json=request))


@pytest.fixture(params=PACKAGES)
def workbench(request, tmp_path):
    name = request.param
    package = load_reference_package(FIXTURES / name / "manifest.json")
    environment = {
        "python": platform.python_version(),
        "pyogrio": pyogrio.__version__,
        "gdal_vector": pyogrio.__gdal_version_string__,
        "rasterio": rasterio.__version__,
        "gdal_raster": rasterio.__gdal_version__,
        "proj": pyproj.proj_version_str,
        "shapely": shapely.__version__,
    }
    qgis = None
    if name == "attribute-filter":
        command = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
        if command is None:
            pytest.skip("Set GEOPILOT_QGIS_PROCESS to verify the real vector method")
        qgis = Path(command).resolve(strict=True)
        version = subprocess.run([qgis, "--version"], capture_output=True, text=True, check=True, timeout=30)
        environment["qgis"] = version.stdout.splitlines()[0].split()[1].split("-")[0]
    observed = {key: environment[key] for key in package.environment}
    workspace = tmp_path / "workspace"
    app = create_local_app(tmp_path / "project.sqlite", workspace, qgis_process=qgis)
    before = _fixture_files(package)
    yield app, workspace, package, observed
    assert _fixture_files(package) == before


def test_real_v1_outputs_match_independent_reference(workbench, tmp_path):
    app, workspace, package, observed = workbench
    with TestClient(app) as client:
        draft = _draft(client, package, tmp_path)
        assert draft["issues"] == []
        plan = f"/api/plans/{draft['id']}"
        version = {"plan_version": draft["version"]}
        assert _response(client.post(plan + "/confirm", json=version))["status"] == "confirmed"
        run_id = _response(client.post(plan + "/runs", json=version), 202)["run_id"]
        endpoint = f"/api/runs/{run_id}"
        deadline = time.monotonic() + 90
        while True:
            state = _response(client.get(endpoint))
            if state["current_report_id"] or state["execution_status"] in {"FAILED", "CANCELLED"} or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        artifacts = _response(client.get(endpoint + "/artifacts"))["artifacts"]
    assert (state["execution_status"], state["check_status"]) == ("SUCCEEDED", "PASSED"), state
    run = workspace / "tasks" / draft["task_id"] / "runs" / run_id
    outputs = {item["role"]: OutputFile(item["uri"], item["layer"]) for item in artifacts}
    before = {role: _sha256(run / output.path) for role, output in outputs.items()}
    report = compare_outputs(package, outputs, workspace=run)
    print(json.dumps({
        "method": package.method.model_dump(mode="json"), "run_id": run_id, "execution": "SUCCEEDED", "checks": "PASSED",
        "declared_environment": package.environment, "observed_environment": observed, "reference_report": report,
    }, sort_keys=True))
    assert report["status"] == "PASS", report
    assert {role: item["sha256"] for role, item in report["outputs"].items()} == before
    assert {role: _sha256(run / output.path) for role, output in outputs.items()} == before


def test_real_v1_bad_inputs_fail_before_a_run(workbench, tmp_path):
    app, _, package, _ = workbench
    failure, = package.failure_cases
    # Draft and confirmation handlers need no scheduler. The task API still exposes persisted run records.
    client = TestClient(app)
    try:
        draft = _draft(client, package, tmp_path, failure.id)
        confirm = client.post(f"/api/plans/{draft['id']}/confirm", json={"plan_version": draft["version"]})
        confirm_issues = _response(confirm, 422)["detail"]["issues"]
        runs = _response(client.get(f"/api/tasks/{draft['task_id']}"))["runs"]
    finally:
        client.close()
    issues = draft["issues"] if failure.expected.phase == "draft" else confirm_issues
    print(json.dumps({
        "method": package.method.model_dump(mode="json"), "case": failure.id,
        "expected": failure.expected.model_dump(), "draft_issues": draft["issues"],
        "confirm_issues": confirm_issues, "confirmation_http": confirm.status_code, "runs": runs,
    }, sort_keys=True))
    assert any(issue["code"] == failure.expected.code for issue in issues), issues
    if failure.expected.phase == "confirm":
        assert all(issue["code"] != failure.expected.code for issue in draft["issues"])
    assert runs == []


def _copy_package(tmp_path, name):
    root = tmp_path / "package"
    shutil.copytree(FIXTURES / name, root)
    path = root / "manifest.json"
    return path, json.loads(path.read_text())


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize("kind", ["escape", "absolute", "symlink", "modified", "duplicate-key", "schema-bool", "unsafe-role"])
def test_loader_rejects_unsafe_or_modified_fixture(tmp_path, kind):
    path, manifest = _copy_package(tmp_path, "attribute-filter")
    item = manifest["inputs"]["features"]
    source = path.parent / item["path"]
    expected = "UNSAFE_PATH"
    if kind == "escape":
        item["path"] = "inputs/../inputs/points.geojson"
    elif kind == "absolute":
        item["path"] = str(source)
    elif kind == "symlink":
        link = source.with_name("link.geojson")
        link.symlink_to(source)
        item["path"] = link.relative_to(path.parent).as_posix()
    elif kind == "modified":
        source.write_bytes(source.read_bytes() + b"\n")
        expected = "FILE_INTEGRITY_MISMATCH"
    elif kind == "duplicate-key":
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        expected = "INVALID_REFERENCE_PACKAGE"
    else:
        if kind == "schema-bool":
            manifest["schema_version"] = True
        else:
            manifest["inputs"]["../features"] = item
        expected = "INVALID_REFERENCE_PACKAGE"
    if kind != "duplicate-key":
        _write_json(path, manifest)
    with pytest.raises(ReferencePackageError) as error:
        load_reference_package(path)
    assert error.value.code == expected


def test_failure_overrides_shallow_merge_and_pin_changed_files(tmp_path):
    path, manifest = _copy_package(tmp_path, "attribute-filter")
    case = manifest["failure_cases"][0]
    case["critical_options"] = {"match_value": "new"}
    _write_json(path, manifest)
    package = load_reference_package(path)
    case_id = case["id"]
    assert package.case_options(case_id) == {"field_name": "class_name", "match_value": "new"}
    assert package.case_options()["match_value"] == "keep"
    assert package.resolve_input("features", case_id).name == "without-class.geojson"
    source = package.resolve_input("features", case_id)
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(ReferencePackageError, match="SHA-256"):
        package.resolve_input("features", case_id)


@pytest.mark.parametrize("kind", ["absolute", "escape", "symlink", "hardlink", "inside-package"])
def test_comparison_rejects_paths_aliasing_fixtures(tmp_path, kind):
    manifest, _ = _copy_package(tmp_path, "attribute-filter")
    package = load_reference_package(manifest)
    original = package.resolve_input("features")
    workspace = tmp_path / "run"
    workspace.mkdir()
    alias = workspace / "alias.geojson"
    relative = alias.name
    if kind == "absolute":
        relative = str(original)
    elif kind == "escape":
        relative = "../package/inputs/points.geojson"
    elif kind == "symlink":
        alias.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, alias)
    else:
        workspace = tmp_path
        relative = "package/inputs/points.geojson"
    report = compare_outputs(package, {"filtered_features": OutputFile(relative)}, workspace=workspace)
    assert report["status"] == "FAIL"
    assert [(issue["code"], issue["field"]) for issue in report["issues"]] == [("UNSAFE_PATH", "outputs.filtered_features")]


def _single_output_package(tmp_path, name, role, reference):
    manifest, document = _copy_package(tmp_path, name)
    item = document["outputs"][role]
    document["outputs"] = {role: item}
    path = manifest.parent / item["reference"]["path"]
    _write_json(path, reference)
    item["reference"]["sha256"] = _sha256(path)
    _write_json(manifest, document)
    workspace = tmp_path / "run"
    workspace.mkdir()
    return load_reference_package(manifest), workspace


def test_vector_comparison_keys_by_id_and_compares_topology(tmp_path):
    role = "filtered_features"
    first = [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
    second = [[2, 0], [2, 2], [4, 2], [4, 0], [2, 0]]
    reference = {
        "crs": "EPSG:4326", "id_field": "sample_id", "attribute_fields": ["rank"],
        "features": {
            "a": {"wkt": "POLYGON ((2 2, 2 0, 0 0, 0 2, 2 2))", "properties": {"rank": 1}},
            "b": {"wkt": "POLYGON ((4 0, 4 2, 2 2, 2 0, 4 0))", "properties": {"rank": 2}},
        },
    }
    package, workspace = _single_output_package(tmp_path, "attribute-filter", role, reference)
    features = [
        {"type": "Feature", "properties": {"sample_id": identifier, "rank": rank},
         "geometry": {"type": "Polygon", "coordinates": [coordinates]}}
        for identifier, rank, coordinates in [("b", 2, second), ("a", 1, first)]
    ]
    data = {"type": "FeatureCollection", "features": features}
    path = workspace / "features.geojson"
    outputs = {role: OutputFile(path.name)}
    _write_json(path, data)
    assert compare_outputs(package, outputs, workspace=workspace)["status"] == "PASS"
    data["features"][1]["properties"]["sample_id"] = "b"
    _write_json(path, data)
    report = compare_outputs(package, outputs, workspace=workspace)
    assert "duplicated" in report["issues"][0]["message"]
    data["features"][1]["properties"]["sample_id"] = "a"
    data["features"][1]["geometry"]["coordinates"] = [second]
    _write_json(path, data)
    assert "topology" in compare_outputs(package, outputs, workspace=workspace)["issues"][0]["message"]
    data["features"][1]["geometry"]["coordinates"] = [first]
    data["features"][1]["properties"]["rank"] = 2
    _write_json(path, data)
    assert "attributes" in compare_outputs(package, outputs, workspace=workspace)["issues"][0]["message"]
    data["features"][1]["properties"]["rank"] = 1
    data["crs"] = {"type": "name", "properties": {"name": "EPSG:3857"}}
    _write_json(path, data)
    assert "CRS" in compare_outputs(package, outputs, workspace=workspace)["issues"][0]["message"]


@pytest.mark.parametrize("change", ["within-tolerance", "mask", "value", "transform", "nodata"])
def test_raster_comparison_detects_mask_values_and_grid_changes(tmp_path, change):
    role = "terrain_raster"
    reference = {
        "crs": "EPSG:6933", "transform": [10, 0, 100, 0, -10, 100], "resolution": [10, 10],
        "width": 2, "height": 1, "dtypes": ["float32"], "nodata": [-9999],
        "pixels": [[[45, None]]], "absolute_tolerance": 0.0001, "tolerance_basis": "Half the declared reporting precision.",
    }
    package, workspace = _single_output_package(tmp_path, "dem-terrain", role, reference)
    path = workspace / "slope.tif"
    outputs = {role: OutputFile(path.name)}
    nodata = -9998 if change == "nodata" else -9999
    origin = 101 if change == "transform" else 100
    values = [45.01 if change == "value" else 45.00001, 45 if change == "mask" else nodata]
    with rasterio.open(
        path, "w", driver="GTiff", width=2, height=1, count=1, dtype="float32", crs="EPSG:6933",
        nodata=nodata, transform=rasterio.transform.from_origin(origin, 100, 10, 10),
    ) as raster:
        raster.write(np.array([values], dtype="float32"), 1)
    report = compare_outputs(package, outputs, workspace=workspace)
    if change == "within-tolerance":
        assert report["status"] == "PASS"
        return
    assert report["status"] == "FAIL"
    expected = {"mask": "mask differs", "value": "values differ", "transform": "transform", "nodata": "NoData"}
    assert expected[change] in report["issues"][0]["message"]


def test_json_comparison_requires_exact_types_and_only_declared_tolerance(tmp_path):
    role = "area_statistics"
    reference = {
        "value": {"classes": [{"area/m2": 100.0, "count": 1}]},
        "numeric_tolerances": {"/classes/0/area~1m2": {"absolute": 0.125, "basis": "Analytic precision boundary."}},
    }
    package, workspace = _single_output_package(tmp_path, "dem-terrain", role, reference)
    path = workspace / "area.json"
    outputs = {role: OutputFile(path.name)}
    data = {"classes": [{"area/m2": 100.125, "count": 1}]}
    _write_json(path, data)
    assert compare_outputs(package, outputs, workspace=workspace)["status"] == "PASS"
    data["classes"][0]["count"] = 1.0
    _write_json(path, data)
    assert compare_outputs(package, outputs, workspace=workspace)["issues"][0]["message"].endswith("/count")
    data["classes"][0].update({"count": 1, "area/m2": 100.126})
    _write_json(path, data)
    assert compare_outputs(package, outputs, workspace=workspace)["issues"][0]["message"].endswith("/area~1m2")
    assert compare_outputs(package, {}, workspace=workspace)["issues"][0]["code"] == "OUTPUT_ROLE_SET_MISMATCH"
