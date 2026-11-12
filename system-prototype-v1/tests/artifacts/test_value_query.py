import math
from pathlib import Path

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from pyproj import Transformer
from rasterio.transform import Affine, from_origin
from rasterio.windows import Window

from geopilot.api.app import create_app
from geopilot.artifacts.service import insert_artifacts, prepare_artifacts
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepResult
from geopilot.persistence.run_repository import get_run, insert_run, save_run_step, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.plans.models import AnalysisPlan
from geopilot.runs.models import Run, RunStep


_STAMP = "2026-11-12T00:00:00Z"
_RUN_ID = "original-value-run"
_STEP = "publish"
_GEOGRAPHIC = from_origin(116, 40, 0.01, 0.01)
_ROTATED = Affine(10, 2, 500000, 1, -20, 4400070)
_SAFE_INT = 2**53 - 1


def _write_raster(path, values, *, crs="EPSG:4326", transform=_GEOGRAPHIC, nodata=None, mask=None,
                  units=None, scales=None, offsets=None):
    bands = values[np.newaxis] if values.ndim == 2 else values
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        path, "w", driver="GTiff", width=bands.shape[2], height=bands.shape[1], count=bands.shape[0],
        dtype=bands.dtype, crs=crs, transform=transform, nodata=nodata,
    ) as dataset:
        dataset.write(bands)
        if mask is not None:
            dataset.write_mask(mask)
        if units is not None:
            dataset.units = units
        if scales is not None:
            dataset.scales = scales
        if offsets is not None:
            dataset.offsets = offsets


@pytest.fixture
def registered(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    workspace = create_run_workspace(tmp_path / "workspace", "value-task", _RUN_ID)
    workspace.create_step(_STEP)
    workdir = workspace.path
    products = workdir / "intermediate"
    first = np.array([[0, 11, 12], [21, np.nan, 23], [31, 32, -9999]], dtype="float32")
    second = np.arange(100, 109, dtype="float32").reshape((3, 3))
    valid = np.ones(first.shape, dtype=bool)
    valid[0, 2] = valid[2, 2] = False
    _write_raster(products / "terrain.tif", np.stack([first, second]), nodata=-9999, mask=valid,
                  units=("degree", "m"), scales=(1, 2.5), offsets=(0, -10))
    _write_raster(products / "rotated.tif", np.arange(9, dtype="int16").reshape((3, 3)),
                  crs="EPSG:32650", transform=_ROTATED, nodata=0)
    _write_raster(products / "unsigned.tif", np.array([[_SAFE_INT, _SAFE_INT + 1, 2**64 - 1]], dtype="uint64"))
    _write_raster(products / "signed.tif", np.array([[-_SAFE_INT, -_SAFE_INT - 1, -2**63]], dtype="int64"))
    _write_raster(products / "unknown.tif", first, crs=None)
    _write_raster(products / "complex.tif", np.full((3, 3), 1 + 2j, dtype="complex64"))
    (products / "table.csv").write_text("label,count\nvalid,4\n", encoding="utf-8")

    kinds = {role: "raster" for role in ("terrain", "rotated", "unsigned", "signed", "unknown", "complex")}
    kinds["table"] = "table_csv"
    outputs = {role: {"type": kind, "target": f"intermediate/{role}.{'csv' if role == 'table' else 'tif'}"}
               for role, kind in kinds.items()}
    plan = AnalysisPlan(
        id="value-plan", version=1, task_id="value-task", status="confirmed", goal={"summary": "Read original values"},
        method={"id": "value-method", "version": 1}, inputs={}, critical_options={},
        steps=[{"key": _STEP, "tool": {"id": "value-tool", "version": 1}, "inputs": {}, "parameters": {}, "outputs": outputs}],
        expected_artifacts=[{"role": role, "step": _STEP, "output": role, "type": kind} for role, kind in kinds.items()],
        check_rules=["output_exists"], issues=[],
    )
    result = StepResult(
        run_id=_RUN_ID, step_key=_STEP, status="SUCCEEDED", exit_code=0,
        outputs={role: {"path": target["target"], "type": target["type"], "layer": None} for role, target in outputs.items()},
        log_path=f"steps/{_STEP}/process.log", warnings=[], error=None, started_at=_STAMP, finished_at=_STAMP, environment={},
    )
    prepared = prepare_artifacts(_RUN_ID, plan, workdir, {_STEP: result})
    with open_database(database) as connection, connection:
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES (?, '{}', ?)", (plan.task_id, _STAMP))
        connection.execute(
            "INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at) VALUES (?, ?, ?, 'confirmed', ?, ?)",
            (plan.id, plan.version, plan.task_id, plan.model_dump_json(), _STAMP),
        )
        insert_run(connection, Run(id=_RUN_ID, plan_id=plan.id, plan_version=1, workdir=str(workdir),
                                   created_at=_STAMP, execution_status="RUNNING"))
        save_run_step(connection, RunStep(run_id=_RUN_ID, step_key=_STEP, position=0, status=result.status,
                                         result=result, log_path=result.log_path))
        artifacts = {artifact.role: artifact for artifact in insert_artifacts(connection, prepared)}
        run = get_run(connection, _RUN_ID)
        update_run(connection, run.model_copy(update={"execution_status": "SUCCEEDED"}))
    with TestClient(create_app(database)) as client:
        yield client, database, workdir, artifacts


def _url(artifact):
    return f"/api/artifacts/{artifact.id}/value"


def _query(client, artifact, longitude, latitude, **params):
    response = client.get(_url(artifact), params={"longitude": longitude, "latitude": latitude, **params})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_reads_valid_zero_and_original_raw_band_values_without_requiring_a_preview(registered):
    client, database, workdir, artifacts = registered
    artifact = artifacts["terrain"]
    source = workdir / artifact.uri
    before, database_before = fingerprint_file(source), database.read_bytes()
    assert not any((workdir / "previews").iterdir())
    longitude, latitude = 116.005, 39.995
    value = _query(client, artifact, longitude, latitude, source_fingerprint=artifact.fingerprint)
    assert value == {
        "artifact_id": artifact.id, "source_fingerprint": artifact.fingerprint, "band": 1,
        "longitude": longitude, "latitude": latitude, "source_crs": "EPSG:4326",
        "source_x": longitude, "source_y": latitude, "row": 0, "column": 0, "status": "value",
        "nodata": False, "value": 0, "dtype": "float32", "unit": "degree", "scale": 1, "offset": 0,
    }
    second = _query(client, artifact, longitude, latitude, band=2)
    assert (second["band"], second["value"], second["unit"], second["scale"], second["offset"]) == (2, 100, "m", 2.5, -10)
    adjacent = _query(client, artifact, 116.025, 39.985)
    assert (adjacent["row"], adjacent["column"], adjacent["value"]) == (1, 2, 23)
    assert fingerprint_file(source) == before and database.read_bytes() == database_before
    assert not any((workdir / "previews").iterdir())


@pytest.mark.parametrize(("longitude", "latitude", "row", "column"), [
    (116.025, 39.995, 0, 2), (116.015, 39.985, 1, 1), (116.025, 39.975, 2, 2),
])
def test_internal_mask_nonfinite_and_nodata_all_return_null_with_actual_cell(registered, longitude, latitude, row, column):
    client, _, _, artifacts = registered
    value = _query(client, artifacts["terrain"], longitude, latitude)
    assert (value["status"], value["row"], value["column"], value["value"], value["nodata"]) == (
        "nodata", row, column, None, True,
    )


def test_geographic_half_open_boundaries_keep_adjacent_representable_points_distinct(registered):
    client, _, _, artifacts = registered
    artifact = artifacts["terrain"]
    left, top = _GEOGRAPHIC * (0, 0)
    right, bottom = _GEOGRAPHIC * (3, 3)
    inner_x, inner_y = _GEOGRAPHIC * (1, 1)
    exact = _query(client, artifact, left, top)
    assert (exact["row"], exact["column"], exact["value"]) == (0, 0, 0)
    internal = _query(client, artifact, inner_x, inner_y)
    assert (internal["row"], internal["column"], internal["status"]) == (1, 1, "nodata")
    before_internal = _query(client, artifact, math.nextafter(inner_x, -math.inf), math.nextafter(inner_y, math.inf))
    assert (before_internal["row"], before_internal["column"], before_internal["value"]) == (0, 0, 0)
    for x, y in ((right, 39.985), (116.015, bottom), (math.nextafter(left, -math.inf), top)):
        outside = _query(client, artifact, x, y)
        assert (outside["status"], outside["row"], outside["column"], outside["value"], outside["nodata"]) == (
            "outside", None, None, None, False,
        )
        assert (outside["source_x"], outside["source_y"]) == (x, y)
    inside = _query(client, artifact, math.nextafter(right, -math.inf), 39.985)
    assert (inside["row"], inside["column"], inside["value"]) == (1, 2, 23)


def test_projected_rotated_grid_uses_source_affine_and_half_open_outer_edges(registered):
    client, _, _, artifacts = registered
    artifact = artifacts["rotated"]
    to_lonlat = Transformer.from_crs("EPSG:32650", "EPSG:4326", always_xy=True)

    def at(column, row):
        x, y = _ROTATED * (column, row)
        longitude, latitude = to_lonlat.transform(x, y)
        value = _query(client, artifact, longitude, latitude)
        assert value["source_crs"] == "EPSG:32650"
        assert (value["source_x"], value["source_y"]) == pytest.approx((x, y), rel=0, abs=2e-8)
        return value

    centered = at(1.5, 2.5)
    assert (centered["status"], centered["row"], centered["column"], centered["value"]) == ("value", 2, 1, 7)
    on_internal_edge = at(1, 1)
    assert (on_internal_edge["row"], on_internal_edge["column"], on_internal_edge["value"]) == (1, 1, 4)
    corner = at(0, 0)
    assert (corner["status"], corner["row"], corner["column"], corner["nodata"]) == ("nodata", 0, 0, True)
    for column, row in ((3, 1.5), (1.5, 3), (-0.1, 2)):
        assert at(column, row)["status"] == "outside"


@pytest.mark.parametrize(("role", "expected", "dtype"), [
    ("unsigned", [_SAFE_INT, str(_SAFE_INT + 1), str(2**64 - 1)], "uint64"),
    ("signed", [-_SAFE_INT, str(-_SAFE_INT - 1), str(-2**63)], "int64"),
])
def test_preserves_native_integer_precision_for_browser_json(registered, role, expected, dtype):
    client, _, _, artifacts = registered
    result = [_query(client, artifacts[role], longitude, 39.995) for longitude in (116.005, 116.015, 116.025)]
    assert [item["value"] for item in result] == expected
    assert all(item["dtype"] == dtype and item["status"] == "value" and item["unit"] is None for item in result)


def test_only_the_hit_pixel_and_mask_are_read(registered, monkeypatch):
    client, _, workdir, artifacts = registered
    artifact = artifacts["terrain"]
    actual_open = rasterio.open
    reads = []

    def track_reads(path, *args, **kwargs):
        raster = actual_open(path, *args, **kwargs)
        if Path(path) == workdir / artifact.uri:
            actual_read = raster.read

            def read(band, *args, **kwargs):
                reads.append((band, kwargs.get("window"), kwargs.get("masked")))
                return actual_read(band, *args, **kwargs)

            raster.read = read
        return raster

    monkeypatch.setattr(rasterio, "open", track_reads)
    assert _query(client, artifact, 116.025, 39.985, band=2)["value"] == 105
    assert _query(client, artifact, 116.05, 39.985)["status"] == "outside"
    assert reads == [(2, Window(2, 1, 1, 1), True)]


def test_mismatched_fingerprint_and_modified_source_are_refused(registered):
    client, _, workdir, artifacts = registered
    terrain, rotated = artifacts["terrain"], artifacts["rotated"]
    params = {"longitude": 116.005, "latitude": 39.995}
    response = client.get(_url(terrain), params={**params, "source_fingerprint": rotated.fingerprint})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "ARTIFACT_CHANGED"
    with rasterio.open(workdir / terrain.uri, "r+") as dataset:
        values = dataset.read(1)
        values[0, 0] = 999
        dataset.write(values, 1)
    response = client.get(_url(terrain), params=params)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "ARTIFACT_CHANGED"


@pytest.mark.parametrize(("role", "status", "code"), [
    ("unknown", 422, "RASTER_VALUE_UNAVAILABLE"), ("complex", 422, "RASTER_VALUE_UNAVAILABLE"),
    ("table", 422, "ARTIFACT_NOT_RASTER"), ("missing", 404, "ARTIFACT_FILE_MISSING"),
    ("unregistered", 404, "ARTIFACT_NOT_FOUND"),
])
def test_distinguishes_unavailable_originals(registered, role, status, code):
    client, _, workdir, artifacts = registered
    if role == "missing":
        (workdir / artifacts["terrain"].uri).unlink()
    url = _url(artifacts["terrain"] if role == "missing" else artifacts[role]) if role != "unregistered" else "/api/artifacts/missing/value"
    response = client.get(url, params={"longitude": 116.005, "latitude": 39.995})
    assert response.status_code == status and response.json()["detail"]["code"] == code


@pytest.mark.parametrize("overrides", [
    {"longitude": "NaN"}, {"latitude": "inf"}, {"longitude": 181}, {"latitude": -91}, {"band": 0}, {"band": 3},
    {"source_fingerprint": "invalid"},
])
def test_query_parameters_are_finite_and_validated(registered, overrides):
    client, _, _, artifacts = registered
    response = client.get(_url(artifacts["terrain"]), params={"longitude": 116.005, "latitude": 39.995, **overrides})
    assert response.status_code == 422
