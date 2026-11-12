import json
import math
import warnings

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from geopilot.api.app import create_app
from geopilot.artifacts.service import insert_artifacts, prepare_artifacts
from geopilot.data.fingerprint import fingerprint_file
from geopilot.domain.step_io import StepResult
from geopilot.persistence.run_repository import get_run, insert_run, save_run_step, update_run
from geopilot.persistence.schema import initialize_database, open_database
from geopilot.persistence.workspace import create_run_workspace
from geopilot.plans.models import AnalysisPlan
from geopilot.runs.models import Run, RunStep


STAMP = "2026-11-11T00:00:00Z"
RUN_ID = "preview-run"
STEP = "publish"


def _write_raster(path, values, *, crs, transform, nodata, mask=None):
    bands = values[np.newaxis] if values.ndim == 2 else values
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
        path, "w", driver="GTiff", width=bands.shape[2], height=bands.shape[1], count=bands.shape[0],
        dtype=bands.dtype, crs=crs, transform=transform, nodata=nodata,
    ) as raster:
        raster.write(bands)
        if mask is not None:
            raster.write_mask(mask)


@pytest.fixture
def registered(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    workspace = create_run_workspace(tmp_path / "workspace", "preview-task", RUN_ID)
    workspace.create_step(STEP)
    workdir = workspace.path
    terrain = np.array([
        [12, 0, 15, 20], [25, 30, np.nan, 35], [35, 40, 45, 45], [30, 20, 10, -9999],
    ], dtype="float32")
    terrain_mask = np.ones((4, 4), dtype=bool)
    terrain_mask[0, 0] = terrain_mask[3, 3] = False
    _write_raster(workdir / "intermediate/terrain.tif", terrain, crs="EPSG:4326",
                  transform=from_origin(116, 40, 0.001, 0.00075), nodata=-9999, mask=terrain_mask)
    classes = np.tile(np.array([0, 1, 2, 255], dtype="uint8"), (4, 1))
    classes_mask = classes != 255
    classes_mask[1, 2] = False
    mercator = from_origin(500000, 4400000, 10, 10)
    _write_raster(workdir / "intermediate/classes.tif", classes, crs="EPSG:3857",
                  transform=mercator, nodata=255, mask=classes_mask)
    _write_raster(workdir / "intermediate/unknown.tif", terrain, crs=None,
                  transform=from_origin(116, 40, 0.001, 0.001), nodata=-9999)
    _write_raster(workdir / "intermediate/dateline.tif", terrain, crs="EPSG:4326",
                  transform=from_origin(179, 40, 1, 1), nodata=-9999)
    _write_raster(workdir / "intermediate/empty.tif", np.full((4, 4), -9999, dtype="float32"),
                  crs="EPSG:3857", transform=mercator, nodata=-9999)
    _write_raster(workdir / "intermediate/bands.tif", np.stack([np.ones((4, 4)), np.full((4, 4), 5)]).astype("float32"),
                  crs="EPSG:3857", transform=mercator, nodata=-9999)
    (workdir / "intermediate/table.csv").write_text("class,area\n1,40\n", encoding="utf-8")
    output_types = {"terrain": "raster_terrain", "classes": "raster_classification", "unknown": "raster",
                    "dateline": "raster", "empty": "raster", "bands": "raster", "table": "table_csv"}
    outputs = {role: {"type": kind, "target": f"intermediate/{role}.{'csv' if role == 'table' else 'tif'}"}
               for role, kind in output_types.items()}
    plan = AnalysisPlan(
        id="preview-plan", version=1, task_id="preview-task", status="confirmed", goal={"summary": "Preview real rasters"},
        method={"id": "preview-method", "version": 1}, inputs={}, critical_options={},
        steps=[{"key": STEP, "tool": {"id": "preview-tool", "version": 1}, "inputs": {}, "parameters": {}, "outputs": outputs}],
        expected_artifacts=[{"role": role, "step": STEP, "output": role, "type": kind} for role, kind in output_types.items()],
        check_rules=["output_exists"], issues=[],
    )
    result = StepResult(
        run_id=RUN_ID, step_key=STEP, status="SUCCEEDED", exit_code=0,
        outputs={role: {"path": output["target"], "type": output["type"], "layer": None} for role, output in outputs.items()},
        log_path=f"steps/{STEP}/process.log", warnings=[], error=None, started_at=STAMP, finished_at=STAMP, environment={},
    )
    prepared = prepare_artifacts(RUN_ID, plan, workdir, {STEP: result})
    with open_database(database) as connection, connection:
        connection.execute("INSERT INTO task (id, goal_json, created_at) VALUES (?, '{}', ?)", (plan.task_id, STAMP))
        connection.execute(
            "INSERT INTO analysis_plan (id, version, task_id, status, plan_json, created_at) VALUES (?, ?, ?, 'confirmed', ?, ?)",
            (plan.id, plan.version, plan.task_id, plan.model_dump_json(), STAMP),
        )
        insert_run(connection, Run(id=RUN_ID, plan_id=plan.id, plan_version=plan.version, workdir=str(workdir),
                                   created_at=STAMP, execution_status="RUNNING"))
        save_run_step(connection, RunStep(run_id=RUN_ID, step_key=STEP, position=0, status=result.status,
                                         result=result, log_path=result.log_path))
        artifacts = {artifact.role: artifact for artifact in insert_artifacts(connection, prepared)}
        run = get_run(connection, RUN_ID)
        update_run(connection, run.model_copy(update={"execution_status": "SUCCEEDED"}))
    with TestClient(create_app(database)) as client:
        yield client, database, workdir, artifacts


def _endpoint(artifact):
    return f"/api/artifacts/{artifact.id}/raster-preview"


def _preview(client, artifact, **params):
    response = client.get(_endpoint(artifact), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _rgba(client, descriptor):
    response = client.get(descriptor["image_url"])
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    with warnings.catch_warnings(), MemoryFile(response.content) as memory:
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with memory.open() as raster:
            assert raster.driver == "PNG" and raster.count == 4 and raster.dtypes == ("uint8",) * 4
            assert (raster.width, raster.height) == (descriptor["width"], descriptor["height"])
            return raster.read().transpose(1, 2, 0), response.content


def test_true_3857_preview_colors_masks_and_persisted_variants_preserve_original(registered):
    client, database, workdir, artifacts = registered
    artifact = artifacts["terrain"]
    path = workdir / artifact.uri
    original = path.read_bytes()
    original_digest = fingerprint_file(path).digest
    original_database = database.read_bytes()

    terrain = _preview(client, artifact)
    gray = _preview(client, artifact, palette="grayscale")
    rgba, terrain_png = _rgba(client, terrain)
    gray_rgba, gray_png = _rgba(client, gray)
    assert terrain_png != gray_png
    assert terrain["artifact_id"] == artifact.id and terrain["source_fingerprint"] == original_digest == artifact.fingerprint
    assert terrain["image_url"].startswith(_endpoint(artifact) + ".png?") and original_digest in terrain["image_url"]
    assert terrain["palette"] == "terrain" and gray["palette"] == "grayscale" and terrain["band"] == 1
    assert terrain["source_crs"] == "EPSG:4326" and terrain["display_crs"] == "EPSG:3857"
    assert terrain["source_resolution"] == pytest.approx([0.001, 0.00075])
    mercator_y = lambda latitude: 6378137 * math.asinh(math.tan(math.radians(latitude)))
    assert terrain["display_resolution"] == pytest.approx([
        6378137 * math.radians(0.001), (mercator_y(40) - mercator_y(39.997)) / 4,
    ])
    np.testing.assert_allclose(terrain["coordinates"], [[116, 40], [116.004, 40], [116.004, 39.997], [116, 39.997]], atol=1e-9)
    assert terrain["width"] == terrain["height"] == 4 and terrain["resampling"] == "nearest"
    assert terrain["color_mode"] == "continuous" and terrain["classes"] == [] and terrain["nodata_alpha"] == 0
    assert terrain["stretch"] == gray["stretch"] == {"minimum": 0, "maximum": 45}
    assert terrain["ramp"][0] == {"position": 0, "rgba": [47, 76, 116, 255]}
    assert rgba[0, 1].tolist() == [47, 76, 116, 255] and gray_rgba[0, 1].tolist() == [30, 30, 30, 255]
    expected_alpha = np.full((4, 4), 255, dtype="uint8")
    expected_alpha[0, 0] = expected_alpha[1, 2] = expected_alpha[3, 3] = 0
    np.testing.assert_array_equal(rgba[:, :, 3], expected_alpha)
    np.testing.assert_array_equal(gray_rgba[:, :, 3], expected_alpha)

    variants = list((workdir / "previews" / artifact.id).iterdir())
    assert len(variants) == 2
    saved = {}
    for variant in variants:
        record = json.loads((variant / "preview.json").read_text())
        png = (variant / f'{record["image_sha256"]}.png').read_bytes()
        saved[record["descriptor"]["palette"]] = (record["descriptor"], png)
    assert saved == {"terrain": (terrain, terrain_png), "grayscale": (gray, gray_png)}
    assert path.read_bytes() == original and fingerprint_file(path).digest == original_digest
    assert database.read_bytes() == original_database
    assert not list(path.parent.glob("terrain.tif.*"))


def test_class_zero_is_opaque_and_color_change_uses_declared_legend(registered):
    client, _, _, artifacts = registered
    artifact = artifacts["classes"]
    terrain = _preview(client, artifact)
    gray = _preview(client, artifact, palette="grayscale")
    rgba, _ = _rgba(client, terrain)
    gray_rgba, _ = _rgba(client, gray)
    assert terrain["color_mode"] == "categorical" and terrain["ramp"] == [] and terrain["stretch"] is None
    legend = {entry["value"]: entry["rgba"] for entry in terrain["classes"]}
    assert set(legend) == {0, 1, 2} and rgba[0, 0].tolist() == legend[0] and rgba[0, 0, 3] == 255
    assert rgba[0, 1].tolist() == legend[1] and rgba[0, 2].tolist() == legend[2]
    assert rgba[0, 3, 3] == rgba[1, 2, 3] == 0
    assert gray["classes"] != terrain["classes"] and not np.array_equal(gray_rgba, rgba)


def test_bounded_preview_and_explicit_band_are_stable(registered):
    client, _, _, artifacts = registered
    descriptor = _preview(client, artifacts["bands"], band=2, max_size=2)
    assert (descriptor["width"], descriptor["height"], descriptor["band"]) == (2, 2, 2)
    assert descriptor["stretch"] == {"minimum": 5, "maximum": 5}
    rgba, _ = _rgba(client, descriptor)
    np.testing.assert_array_equal(rgba, np.tile([87, 154, 107, 255], (2, 2, 1)))


def test_all_nodata_is_transparent_with_no_stretch(registered):
    client, _, _, artifacts = registered
    descriptor = _preview(client, artifacts["empty"])
    assert descriptor["stretch"] is None
    rgba, _ = _rgba(client, descriptor)
    assert np.count_nonzero(rgba[:, :, 3]) == 0


def test_exact_original_is_checked_again_even_for_published_variant(registered):
    client, _, workdir, artifacts = registered
    artifact = artifacts["terrain"]
    descriptor = _preview(client, artifact)
    mismatch = client.get(_endpoint(artifact) + ".png", params={"source_fingerprint": "0" * 64})
    assert mismatch.status_code == 409 and mismatch.json()["detail"]["code"] == "ARTIFACT_CHANGED"
    with rasterio.open(workdir / artifact.uri, "r+") as raster:
        values = raster.read(1)
        values[0, 1] = 11
        raster.write(values, 1)
    for url in (_endpoint(artifact), descriptor["image_url"]):
        response = client.get(url)
        assert response.status_code == 409 and response.json()["detail"]["code"] == "ARTIFACT_CHANGED"


def test_reuses_completed_preview_and_repairs_an_invalid_cached_address(registered, monkeypatch):
    client, _, workdir, artifacts = registered
    artifact = artifacts["terrain"]
    expected = _preview(client, artifact)
    _, png = _rgba(client, expected)
    record_path, = (workdir / "previews" / artifact.id).glob("*/preview.json")

    def unexpected_render(*_args, **_kwargs):
        pytest.fail("A completed preview variant should be reused")

    with monkeypatch.context() as patch:
        patch.setattr("geopilot.api.artifact_routes.render_raster_preview", unexpected_render)
        assert _preview(client, artifact) == expected
        assert _rgba(client, expected)[1] == png

    record = json.loads(record_path.read_text())
    record["descriptor"]["image_url"] = "https://example.invalid/other.png"
    record_path.write_text(json.dumps(record))
    assert _preview(client, artifact) == expected
    assert json.loads(record_path.read_text())["descriptor"] == expected


@pytest.mark.parametrize(("role", "status", "code"), [
    ("unknown", 422, "RASTER_PREVIEW_UNAVAILABLE"), ("dateline", 422, "RASTER_PREVIEW_UNAVAILABLE"),
    ("table", 422, "ARTIFACT_NOT_RASTER"), ("missing", 404, "ARTIFACT_FILE_MISSING"),
    ("symlink", 422, "ARTIFACT_UNREADABLE"),
])
def test_preview_rejects_invalid_formal_source(registered, role, status, code):
    client, _, workdir, artifacts = registered
    if role in {"missing", "symlink"}:
        artifact = artifacts["terrain"]
        path = workdir / artifact.uri
        path.unlink()
        if role == "symlink":
            path.symlink_to(workdir / artifacts["classes"].uri)
    else:
        artifact = artifacts[role]
    response = client.get(_endpoint(artifact))
    assert response.status_code == status and response.json()["detail"]["code"] == code
    if role == "unknown":
        download = client.get(f"/api/artifacts/{artifact.id}/file")
        assert download.status_code == 200 and download.content == (workdir / artifact.uri).read_bytes()


def test_query_validation_and_openapi_png_type(registered):
    client, _, _, artifacts = registered
    endpoint = _endpoint(artifacts["terrain"])
    for params in ({"max_size": 0}, {"max_size": 1025}, {"band": 0}, {"band": 2}, {"palette": "custom"}):
        assert client.get(endpoint, params=params).status_code == 422
    assert client.get("/api/artifacts/not-registered/raster-preview").status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    png = paths["/api/artifacts/{artifact_id}/raster-preview.png"]["get"]["responses"]["200"]["content"]
    assert png == {"image/png": {"schema": {"type": "string", "format": "binary"}}}
