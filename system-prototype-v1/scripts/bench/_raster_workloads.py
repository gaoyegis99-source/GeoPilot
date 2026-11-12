"""Run existing GeoPilot raster operations and verify their benchmark outputs."""

import json
import math
import time
from contextlib import chdir
from functools import partial
from hashlib import sha256
from pathlib import Path, PurePosixPath
from shutil import copyfile

import rasterio
from fastapi.testclient import TestClient
from rasterio.windows import Window

from geopilot.api.server import create_local_app
from geopilot.domain.step_io import ExecutionStatus, StepFileRef, StepRequest
from geopilot.execution.step_support import resolve_step_path
from geopilot.gis.raster.aspect import calculate_aspect
from geopilot.gis.raster.dem_prepare import plan_dem_windows
from geopilot.gis.raster.terrain import write_slope_raster
from geopilot.gis.raster.window_io import read_raster_window
from geopilot.observability.telemetry import SIDECAR_NAME
from geopilot.persistence.run_repository import get_run, list_run_steps
from geopilot.persistence.schema import open_database
from geopilot.remote_sensing.alignment import align_ndvi
from geopilot.remote_sensing.change import write_ndvi_change
from geopilot.remote_sensing.config_models import SensorProductConfig
from geopilot.remote_sensing.config_registry import load_sensor_registry
from geopilot.remote_sensing.indices import write_ndvi
from geopilot.remote_sensing.quality_mask import write_quality_mask
from geopilot.remote_sensing.scaling import scale_product_reflectance

from _raster_reference import aligned_values, compare_pixels, dem_values, optical_values, require, verify_raster


DEM_METHOD = {"id": "dem-terrain-analysis", "version": 1}
REMOTE_METHOD = {"id": "remote-sensing-index-change", "version": 1}
DEM_OPTIONS = {
    "terrain_output": "slope", "slope_unit": "degree", "area_crs": "EPSG:6933",
    "threshold": {"intervals": [{"class_id": 7, "lower": 45, "upper": 45, "include_lower": True, "include_upper": True}]},
}
REMOTE_OPTIONS = {"resampling": "nearest", "change_threshold": 0.1, "area_crs": "EPSG:6933"}
DEM_ARTIFACTS = {"terrain_raster", "threshold_regions", "area_statistics"}
REMOTE_ARTIFACTS = {
    "sensor_configuration", "calibration_report", "first_quality_mask", "second_quality_mask", "first_ndvi",
    "second_ndvi", "alignment_report", "change_raster", "change_classes", "area_statistics",
}
AREA_RELATIVE_TOLERANCE = 2e-7
AREA_ABSOLUTE_TOLERANCE = 0.02


def window_record(window):
    return dict(zip(("col_offset", "row_offset", "width", "height"), map(int, window.flatten()), strict=True))


def window_probes(side, block, period):
    return {
        "block_boundary": Window(block - 16, block - 16, 32, 32),
        "source_edge": Window(side - 32, side - 32, 32, 32),
        "nodata_neighborhood": Window(period // 2 - 4, period // 2 - 4, 9, 9),
    }


def _optical_geometry(fixture, profile):
    return {
        "landsat_side": fixture.grid("first_product").shape[1], "offset": profile["offset_m"],
        "first_pixel": profile["landsat_pixel_m"], "second_pixel": profile["sentinel_pixel_m"],
        "scl_pixel": profile["scl_pixel_m"],
    }


def _terrain_check(path, fixture, profile, *, value, cropped=False, classes=False):
    grid = fixture.grid("dem")
    height, side = grid.shape
    if cropped:
        grid = grid.window(Window(1, 1, side - 2, height - 2))
    metrics = verify_raster(
        path, grid, dtypes=["uint8" if classes else "float32"], nodata=[255 if classes else -9999],
        expected=partial(
            dem_values, side=side, period=profile["nodata_period_pixels"], offset=int(cropped), terrain=value,
        ), tolerance=0 if classes else 1e-5,
    )
    require(metrics["valid_pixels_by_band"] == [fixture.reference["valid_terrain_pixels"]], "Terrain pixel count differs")
    return metrics


def _optical_check(path, fixture, geometry, role, stage):
    return verify_raster(
        path, fixture.grid(role), dtypes=["uint8" if stage == "quality" else "float64"],
        nodata=[None if stage == "quality" else "nan"],
        expected=partial(optical_values, role=role, stage=stage, **geometry),
        tolerance=0 if stage == "quality" else 1e-6, count_ones=stage == "quality",
    )


def _common_check(path, fixture, geometry, stage):
    bands = 2 if stage == "aligned" else 1
    metrics = verify_raster(
        path, fixture.grid("first_product"), dtypes=["uint8" if stage == "classes" else "float64"] * bands,
        nodata=[255 if stage == "classes" else "nan"] * bands,
        expected=partial(aligned_values, stage=stage, **geometry), tolerance=0 if stage == "classes" else 1e-6,
    )
    require(metrics["valid_pixels_by_band"] == [fixture.reference["common_valid_pixels"]] * bands,
            "Common valid pixel count differs")
    return metrics


def read_ahead(paths):
    total = 0
    for path in dict.fromkeys(paths):
        with path.open("rb") as stream:
            while data := stream.read(1024 * 1024):
                total += len(data)
    return total


def dem_tools(fixture, profile, directory, measure):
    dem = fixture.files["dem"]
    grid = fixture.grid("dem")
    height, side = grid.shape
    period = profile["nodata_period_pixels"]
    for name, window in window_probes(side, profile["block_pixels"], period).items():
        cells = int(window.width * window.height)
        observed, trial = measure(
            f"window_read_{name}", read_raster_window, (dem, window), {}, [dem], cells, 4 * cells,
        )
        values, valid = dem_values(window, period=period, side=side)
        expected_grid = grid.window(window)
        require((observed.crs.to_string(), observed.transform, observed.nodata, observed.data.dtype.name)
                == (grid.crs, expected_grid.affine, (-9999,), "float32"), "Window grid, NoData or type differs")
        trial["correctness"] = {
            "window": window_record(window), "valid_pixels": int(valid.sum()), "masked_pixels": cells - int(valid.sum()),
            "maximum_absolute_error": compare_pixels(observed.data, values, valid, 0, name),
        }

    extent = directory / "extent.gpkg"
    copyfile(fixture.files["analysis_extent"], extent)
    with chdir(directory):
        windows, trial = measure(
            "neighborhood_extent", plan_dem_windows,
            (dem, StepFileRef(path="extent.gpkg", layer="analysis_extent"), 1),
            {}, [dem, extent], None, None,
        )
    analysis = Window(1, 1, side - 2, height - 2)
    expanded = Window(0, 0, side, height)
    require(windows["radius_pixels"] == 1 and windows["analysis_window"] == window_record(analysis)
            and windows["expanded_window"] == window_record(expanded)
            and windows["analysis_bounds"] == fixture.reference["analysis_extent"]["bounds"],
            "Neighborhood windows or bounds differ")
    trial["correctness"] = {key: windows[key] for key in ("analysis_window", "expanded_window", "analysis_bounds")}
    for name, operation, options, value in (
        ("slope_degree", write_slope_raster, {"slope_unit": "degree"}, 45),
        ("slope_percent", write_slope_raster, {"slope_unit": "percent"}, 100),
        ("aspect", calculate_aspect, {}, 270),
    ):
        output = directory / f"{name}.tif"
        _, trial = measure(name, operation, (dem, output), {"band": 1, "z_factor": 1, **options},
                           [dem], height * side, 4 * height * side, [output])
        trial["correctness"] = _terrain_check(output, fixture, profile, value=value)


def remote_tools(fixture, profile, directory, measure, root):
    geometry = _optical_geometry(fixture, profile)
    ndvi_paths = []
    registry = load_sensor_registry(root / "config/sensors")
    for period, role in (("first", "first_product"), ("second", "second_product")):
        product = fixture.files[role]
        reflectance = directory / f"{period}_reflectance.tif"
        quality = directory / f"{period}_quality.tif"
        ndvi = directory / f"{period}_ndvi.tif"
        setup = time.monotonic_ns()
        scale_product_reflectance(product, reflectance, registry=registry)
        write_quality_mask(product, quality, registry=registry)
        setup_ns = time.monotonic_ns() - setup
        quality_report = _optical_check(quality, fixture, geometry, role, "quality")
        cells = math.prod(fixture.grid(role).shape)
        _, trial = measure(f"{period}_ndvi", write_ndvi, (reflectance, quality, ndvi), {},
                           [reflectance, quality], cells, cells * 17, [ndvi])
        trial["setup_ns"] = setup_ns
        trial["correctness"] = {
            "ndvi": _optical_check(ndvi, fixture, geometry, role, "ndvi"), "quality": quality_report,
        }
        ndvi_paths.append(ndvi)
    aligned = directory / "aligned.tif"
    extent = fixture.files["analysis_extent"]
    cells = geometry["landsat_side"] ** 2
    _, trial = measure(
        "align_nearest", align_ndvi, (*ndvi_paths, aligned),
        {"resampling": "nearest", "analysis_extent": extent, "analysis_layer": "analysis_extent"},
        [*ndvi_paths, extent], cells, sum(math.prod(fixture.grid(role).shape) for role in
                                         ("first_product", "second_product")) * 8, [aligned],
    )
    trial["correctness"] = _common_check(aligned, fixture, geometry, "aligned")
    change = directory / "change.tif"
    _, trial = measure("change_delta", write_ndvi_change, (aligned, change), {}, [aligned], cells, cells * 16, [change])
    trial["correctness"] = _common_check(change, fixture, geometry, "change")


def _response(response, code=200):
    require(response.status_code == code, f"HTTP {response.status_code}: {response.text}")
    return response.json()


def _terminal_state(client, endpoint, trial, deadline):
    consecutive_storage_errors = 0
    request_index = 0
    while time.monotonic() < deadline:
        request_index += 1
        request_started = time.monotonic_ns()
        response = client.get(endpoint)
        if response.status_code == 200:
            state = response.json()
            trial["run_state"] = state
            consecutive_storage_errors = 0
            if (state["execution_status"] in {"FAILED", "CANCELLED"}
                or state["check_status"] in {"PASSED", "WARNING", "FAILED"}):
                return state
        else:
            elapsed_ns = time.monotonic_ns() - request_started
            try:
                body = response.json()
            except ValueError:
                body = response.text
            trial.setdefault("status_poll_errors", []).append({
                "http_status": response.status_code, "body": body, "elapsed_ns": elapsed_ns,
                "request_index": request_index,
            })
            detail = body.get("detail", {}) if isinstance(body, dict) else {}
            if response.status_code != 500 or not isinstance(detail, dict) or detail.get("code") != "STORAGE_FAILED":
                raise RuntimeError(f"Run status GET returned HTTP {response.status_code}: {response.text}")
            consecutive_storage_errors += 1
            if consecutive_storage_errors >= 3:
                raise RuntimeError("Three consecutive run status GET requests failed with STORAGE_FAILED")
        time.sleep(0.2)
    raise TimeoutError("Run did not reach an observable persisted terminal state by the deadline")


def _executed_step_requests(database, workdir, run_id, state):
    """Read adapter-normalized requests from the actual run after its terminal observation."""
    with open_database(database, read_only=True) as connection:
        connection.execute("BEGIN")
        run = get_run(connection, run_id)
        require(
            run is not None and run.id == state["run_id"] and Path(run.workdir) == workdir
            and (run.execution_status, run.check_status) == (state["execution_status"], state["check_status"]),
            "Persisted method run differs from its observed terminal state",
        )
        records = list_run_steps(connection, run_id)
    observed = []
    for step in records:
        result = step.result
        if result is None or result.exit_code is None or (result.error and result.error.phase != "execute"):
            continue
        require(step.request is not None, f"Executed step {step.step_key} has no persisted request")
        relative = PurePosixPath(step.request.log_path).with_name("request.json").as_posix()
        path = resolve_step_path(workdir, relative, field=f"steps.{step.step_key}.request")
        request = StepRequest.model_validate_json(path.read_bytes())
        # The scheduler stores its compiled request before the adapter normalizes parameters.
        require(
            request.model_dump(exclude={"parameters"}) == step.request.model_dump(exclude={"parameters"}),
            f"Normalized request identity differs for executed step {step.step_key}",
        )
        observed.append({
            "step_key": step.step_key, "tool": request.tool.model_dump(mode="json"),
            "parameters": request.parameters, "status": result.status.value, "exit_code": result.exit_code,
        })
    return observed


class MethodWorkload:
    """The warm group retains one production API and scheduler with distinct tasks and runs."""

    def __init__(self, fixture, directory, root, stack):
        started = time.monotonic_ns()
        self.fixture = fixture
        self.root = root
        self.workspace = directory / "workspace"
        self.database = directory / "project.sqlite"
        app = create_local_app(self.database, self.workspace, config=root / "config")
        self.client = stack.enter_context(TestClient(app))
        self.initialization_ns = time.monotonic_ns() - started
        started = time.monotonic_ns()
        self.inputs = {}
        for role, path in fixture.files.items():
            if role == "native_quality_reference":
                continue
            asset = _response(self.client.post("/api/assets", json={
                "path": str(path), "layer": "analysis_extent" if role == "analysis_extent" else None,
            }))
            self.inputs[role] = {key: asset[key] for key in ("asset_id", "asset_version")}
        self.registration_ns = time.monotonic_ns() - started

    def run(self, trial, profile, expected_area, timeout, sampler_factory):
        client, fixture = self.client, self.fixture
        is_dem = fixture.family == "raster"
        method, options = (DEM_METHOD, DEM_OPTIONS) if is_dem else (REMOTE_METHOD, REMOTE_OPTIONS)
        trial["method"] = method
        trial["critical_options"] = options
        setup = time.monotonic_ns()
        task = _response(client.post("/api/tasks", json={"goal": {"summary": "P13-04 raster benchmark"}}), 201)["task_id"]
        draft = _response(client.post(f"/api/tasks/{task}/plans", json={
            "method": method, "inputs": self.inputs, "critical_options": options,
        }))
        require(not draft["issues"], f"Draft issues: {draft['issues']}")
        endpoint = f"/api/plans/{draft['id']}"
        request = {"plan_version": draft["version"]}
        confirmed = _response(client.post(endpoint + "/confirm", json=request))
        require(confirmed["status"] == "confirmed" and confirmed["method"] == method, "Unexpected confirmed plan")
        trial["setup_ns"] = time.monotonic_ns() - setup
        trial["confirmed_parameters"] = confirmed["critical_options"]
        trial["parameters_sha256"] = sha256(
            json.dumps(confirmed["critical_options"], sort_keys=True, separators=(",", ":")).encode(),
        ).hexdigest()
        trial["task_id"] = task
        trial["plan_id"] = draft["id"]
        trial["plan_version"] = draft["version"]
        if not is_dem:
            products = fixture.reference["products"]
            registry = load_sensor_registry(self.root / "config/sensors")
            for period, name in (("first", "landsat"), ("second", "sentinel")):
                snapshot = SensorProductConfig.model_validate(confirmed["critical_options"][f"{period}_configuration"])
                expected = registry.get(snapshot.ref)
                require(snapshot == expected and snapshot.ref.model_dump(mode="json") == products[name]["configuration"],
                        f"Unexpected confirmed {period} sensor configuration")
        with sampler_factory(self.workspace / "tasks" / task / "runs", rss=False) as sampler:
            started = time.monotonic_ns()
            run_id = _response(client.post(endpoint + "/runs", json=request), 202)["run_id"]
            trial["run_id"] = run_id
            run_endpoint = f"/api/runs/{run_id}"
            directory = self.workspace / "tasks" / task / "runs" / run_id
            trial["workspace"] = str(directory)
            try:
                state = _terminal_state(client, run_endpoint, trial, time.monotonic() + timeout)
            except Exception:
                trial["stop_group"] = True
                trial["wall_ns"] = time.monotonic_ns() - started
                try:
                    cancellation = client.post(run_endpoint + "/cancel")
                    trial["cancellation"] = {"http_status": cancellation.status_code, "body": cancellation.text}
                except Exception as cancellation_error:
                    trial["cancellation"] = {"error": str(cancellation_error)}
                raise
            finally:
                trial.setdefault("wall_ns", time.monotonic_ns() - started)
        trial["workspace_size"] = sampler.document()
        trial["executed_steps"] = _executed_step_requests(self.database, directory, run_id, state)
        sidecar = directory / SIDECAR_NAME
        for _ in range(40):
            if sidecar.is_file():
                break
            time.sleep(0.05)
        require(sidecar.is_file(), f"Missing P13-02 telemetry: {sidecar}")
        telemetry = json.loads(sidecar.read_text())
        trial["telemetry"] = telemetry
        trial["exit_code"] = telemetry["exit_code"]
        require((telemetry["schema_version"], telemetry["run_id"], telemetry["execution_status"],
                 telemetry["check_status"]) == (1, run_id, "SUCCEEDED", "PASSED"),
                f"Run {run_id} did not pass execution/checks: {state}")
        versions = [(item["key"], item["tool"]) for item in confirmed["steps"]]
        require(len(versions) == (7 if is_dem else 8) and
                [(step["step_key"], {"id": step["tool"]["id"], "version": step["tool"]["version"]})
                 for step in telemetry["steps"]] == versions and
                all(step["status"] == "SUCCEEDED" and step["exit_code"] == 0 for step in telemetry["steps"])
                and [(step["step_key"], step["tool"]) for step in trial["executed_steps"]] == versions
                and all(step["status"] == ExecutionStatus.SUCCEEDED and step["exit_code"] == 0
                        for step in trial["executed_steps"]),
                "Registered or measured steps differ from the confirmed V1 method")
        check = _response(client.get(run_endpoint + "/checks"))["current_report"]
        require(check["status"] == "PASSED" and check["report"]["completed_rules"] == confirmed["check_rules"],
                "Persisted V1 check rules differ")
        trial["v1_completed_checks"] = check["report"]["completed_rules"]
        artifacts = _response(client.get(run_endpoint + "/artifacts"))["artifacts"]
        trial["correctness"], trial["output_bytes"] = self._verify_artifacts(
            artifacts, directory, profile, expected_area, confirmed, run_id,
        )
        trial["pixels"] = math.prod(fixture.grid("dem" if is_dem else "first_product").shape)

    def _verify_artifacts(self, artifacts, directory, profile, expected_area, confirmed, run_id):
        fixture = self.fixture
        is_dem = fixture.family == "raster"
        roles = {item["role"]: item for item in artifacts}
        require(roles.keys() == (DEM_ARTIFACTS if is_dem else REMOTE_ARTIFACTS) and len(roles) == len(artifacts),
                "Official artifact roles differ")
        outputs = {}
        inodes = {}
        for role, item in roles.items():
            path = _run_file(directory, item["uri"])
            origin = item["source"]
            require(item["readable"] and item["run_id"] == run_id and origin["plan_id"] == confirmed["id"]
                    and origin["plan_version"] == confirmed["version"]
                    and origin["inputs"] and all(confirmed["inputs"][key] == value for key, value in origin["inputs"].items()),
                    f"Artifact provenance differs for {role}")
            outputs[role] = path
            stat = path.stat()
            inodes[(stat.st_dev, stat.st_ino)] = stat.st_size
        if is_dem:
            checks = {
                "terrain_raster": _terrain_check(outputs["terrain_raster"], fixture, profile, value=45, cropped=True),
                "threshold_regions": _terrain_check(
                    outputs["threshold_regions"], fixture, profile, value=7, cropped=True, classes=True,
                ),
            }
            counts = {7: fixture.reference["valid_terrain_pixels"]}
            nodata = fixture.reference["terrain_nodata_pixels"]
        else:
            geometry = _optical_geometry(fixture, profile)
            checks = {}
            for period, role in (("first", "first_product"), ("second", "second_product")):
                for suffix, stage in (("quality_mask", "quality"), ("ndvi", "ndvi")):
                    key = f"{period}_{suffix}"
                    checks[key] = _optical_check(outputs[key], fixture, geometry, role, stage)
            aligned = _run_file(directory, "intermediate/align_two_dates/aligned_ndvi.tif")
            checks["aligned_ndvi"] = _common_check(aligned, fixture, geometry, "aligned")
            checks["change_raster"] = _common_check(outputs["change_raster"], fixture, geometry, "change")
            checks["change_classes"] = _common_check(outputs["change_classes"], fixture, geometry, "classes")
            counts = {1: fixture.reference["phase_counts"]["decrease"], 2: fixture.reference["phase_counts"]["stable"],
                      3: fixture.reference["phase_counts"]["increase"]}
            nodata = fixture.reference["nodata_pixels"]
            alignment = json.loads(outputs["alignment_report"].read_text())
            require(alignment["common_valid_pixels"] == sum(counts.values()) and alignment["resampling"] == "nearest",
                    "Persisted alignment counts or resampling differ")
            products = json.loads(outputs["sensor_configuration"].read_text())
            calibration = json.loads(outputs["calibration_report"].read_text())
            registry = load_sensor_registry(self.root / "config/sensors")
            for role, name in (("first_product", "landsat"), ("second_product", "sentinel")):
                ref = fixture.reference["products"][name]
                configuration = SensorProductConfig.model_validate(products[role]["configuration"])
                require(configuration == registry.get(configuration.ref)
                        and configuration.ref.model_dump(mode="json") == ref["configuration"]
                        and products[role]["product_id"] == calibration[role]["product_id"] == ref["id"]
                        and products[role]["product_key"] == calibration[role]["product_key"] == ref["product_key"],
                        f"Official configuration/calibration provenance differs for {role}")
                for band in ("red", "nir"):
                    observed = calibration[role]["bands"][band]
                    require((observed["source_code"], observed["source_index"]) ==
                            (ref["bands"][band]["code"], ref["bands"][band]["index"]),
                            f"Calibration band differs for {role}.{band}")
                    keys, values = (("scale", "offset"), (0.0000275, -0.2)) if name == "landsat" else (
                        ("quantification", "dn_offset"), (10000.0, -1000.0),
                    )
                    require(tuple(observed[key] for key in keys) == values, f"Calibration differs for {role}.{band}")
            checks["calibration_report"] = calibration
        stats = json.loads(outputs["area_statistics"].read_text())
        checks["area_statistics"] = verify_area(stats, expected_area, counts, nodata, remote=not is_dem)
        return checks, sum(inodes.values())


def _run_file(directory, name):
    relative = Path(name)
    require(not relative.is_absolute() and ".." not in relative.parts, f"Invalid result path {name}")
    path = directory / relative
    require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(directory),
            f"Missing or unsafe official result: {path}")
    return path


def verify_area(stats, expected_area, counts, nodata, *, remote):
    classes = {row["class_id"]: row for row in stats["classes"]}
    selected = sum(counts.values())
    require(classes.keys() == counts.keys() and len(classes) == len(stats["classes"]) and
            stats["area_crs"] == "EPSG:6933" and stats["resampling"] == "none" and
            stats["cell_area_method"] == "projected_cell_boundary" and stats["area_unit"] == "square_meter" and
            stats["nodata_pixel_count"] == nodata and stats["valid_pixel_count"] == stats["selected_pixel_count"] == selected
            and stats["unselected_pixel_count"] == 0, "Area grid, classes or counts differ")
    if remote:
        require(stats["threshold"] == 0.1 and stats["direction"] == "second_minus_first",
                "Change threshold or direction differs")
        require([classes[code]["class_name"] for code in (1, 2, 3)] == ["decrease", "stable", "increase"],
                "Change class names differ")
    for code, row in classes.items():
        require(row["pixel_count"] == counts[code] and math.isclose(
            row["area_m2"], expected_area[code], rel_tol=AREA_RELATIVE_TOLERANCE, abs_tol=AREA_ABSOLUTE_TOLERANCE,
        ), f"Projected area differs for class {code}: {row['area_m2']} vs {expected_area[code]}")
    require(math.isclose(stats["total_area_m2"], sum(expected_area.values()),
                         rel_tol=AREA_RELATIVE_TOLERANCE, abs_tol=AREA_ABSOLUTE_TOLERANCE),
            "Total projected area differs")
    return {"observed": stats, "expected_area_by_class_m2": expected_area, "relative_tolerance": AREA_RELATIVE_TOLERANCE,
            "absolute_tolerance_m2": AREA_ABSOLUTE_TOLERANCE}
