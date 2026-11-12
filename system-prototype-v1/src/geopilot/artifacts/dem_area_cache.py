"""Reuse one verified DEM area report within a local service session."""

import json
import math
import os
import site
import sys
from hashlib import sha256
from pathlib import Path

import pyproj
from pyproj import CRS

from geopilot.artifacts.step_cache import MAX_PAYLOAD_BYTES, StepCache
from geopilot.checks.cache_reuse import read_reuse_output, verify_reuse_source
from geopilot.data.fingerprint import _sidecars, fingerprint_file
from geopilot.data.raster_reader import read_raster_metadata
from geopilot.domain.step_io import StepRequest, StepReuse
from geopilot.execution.output_staging import OUTPUT_STAGING_ENV, owned_output_staging
from geopilot.execution.step_support import AdapterError, PreparedStep, _verify_outputs, resolve_step_path
from geopilot.execution.worker_lease import worker_lease
from geopilot.methods.models import StepOutputBinding
from geopilot.plans.models import AnalysisPlan


_STEP = "calculate_area_with_equal_area_projection"
_INPUT = "threshold_regions"
_OUTPUT = "area_statistics"
_AREA_TOOL = ("python-dem-area-stats", 1)
_SOURCE = Path(__file__).resolve().parents[1]
_WORKER = _SOURCE / "gis/tools/summarize_dem_area.py"


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _runtime_identity(workdir: Path) -> str | None:
    """Bind the active Python installation and effective local GIS settings."""
    environment = dict(os.environ)
    # External runtime paths may select mutable data, plugins or import hooks.
    external = ("GDAL_CONFIG_FILE", "GDAL_DRIVER_PATH", "GDAL_DATA", "PROJ_DATA", "PROJ_LIB",
                "PROJ_AUX_DB", "PROJ_USER_WRITABLE_DIRECTORY", "PYTHONHOME", "PYTHONUSERBASE", "LD_PRELOAD")
    if any(environment.get(name) for name in external) or environment.get("PROJ_NETWORK", "OFF").upper() != "OFF":
        return None
    # The Python adapter prepends this source root and runs from the step workdir.
    # A module there can shadow a dependency already imported by the API process.
    source_root = _SOURCE.parent
    if any(path.name not in {_SOURCE.name, "__pycache__"} for path in source_root.iterdir()):
        return None
    inherited_imports = environment.get("PYTHONPATH")
    if inherited_imports:
        for entry in inherited_imports.split(os.pathsep):
            path = (workdir / entry).resolve()
            if path != source_root and path.exists():
                return None
    venv_configuration = Path(sys.prefix) / "pyvenv.cfg"
    if environment.get("PYTHONNOUSERSITE") != "1":
        if not venv_configuration.is_file() or site.ENABLE_USER_SITE is not False:
            return None
        options = dict(
            (name.strip().lower(), value.strip().lower())
            for line in venv_configuration.read_text().splitlines()
            for name, separator, value in [line.partition("=")] if separator
        )
        if options.get("include-system-site-packages") != "false":
            return None
    sites = {Path(path) for path in site.getsitepackages()}
    sites.update(Path(path) for path in sys.path if Path(path).name in {"site-packages", "dist-packages"})
    startup = {*sites, _WORKER.parent}
    if any(any(path.glob(name + "*")) for path in startup for name in ("sitecustomize", "usercustomize")):
        return None
    for directory in sites:
        for extension in directory.glob("*.pth"):
            if extension.stat().st_size > MAX_PAYLOAD_BYTES or any(
                line.strip() and not line.lstrip().startswith("#") for line in extension.read_text().splitlines()
            ):
                return None
    home = Path.home()
    if any(path.is_file() for path in (home / ".gdal/gdalrc", Path("/etc/gdal/gdalrc"),
                                       Path(sys.prefix) / "etc/gdal/gdalrc")):
        return None
    proj_user = Path(pyproj.datadir.get_user_data_dir())
    if proj_user.is_dir() and any(proj_user.iterdir()):
        return None
    # A leading/relative LD_LIBRARY_PATH entry resolves in the worker directory.
    if any(workdir.glob("*.so*")):
        return None

    digest = sha256(_canonical({"environment": environment, "python": sys.version}))
    standard_library = Path(os.__file__).parent
    standard_zip = standard_library.with_name(f"python{sys.version_info.major}{sys.version_info.minor}.zip")
    roots = [_SOURCE, Path(sys.executable).resolve(), standard_library, *sites]
    roots.extend(path for path in (venv_configuration, standard_zip) if path.is_file())
    for folder in environment.get("LD_LIBRARY_PATH", "").split(os.pathsep):
        if folder:
            path = Path(folder)
            if not path.is_absolute():
                return None
            if path.is_dir():
                roots.append(path.resolve())

    def record(path: Path) -> None:
        info = path.stat()
        digest.update(_canonical((str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)))

    def unreadable(error: OSError) -> None:
        raise error

    for root in sorted(set(roots)):
        if root.is_file():
            record(root)
        elif root.is_dir():
            for directory, subdirectories, files in os.walk(root, onerror=unreadable):
                folder = Path(directory)
                excluded = {"__pycache__"}
                if folder == standard_library:
                    excluded.update({"site-packages", "dist-packages"})
                subdirectories[:] = sorted(set(subdirectories) - excluded)
                if any((folder / name).is_symlink() for name in subdirectories):
                    return None
                for name in sorted(files):
                    record(folder / name)
    return digest.hexdigest()


def _eligible(plan: AnalysisPlan) -> bool:
    if (plan.method.id, plan.method.version) != ("dem-terrain-analysis", 1):
        return False
    steps = {step.key: step for step in plan.steps}
    area = steps.get(_STEP)
    if area is None or area.tool is None or (area.tool.id, area.tool.version) != _AREA_TOOL:
        return False
    source = area.inputs.get(_INPUT)
    if not isinstance(source, StepOutputBinding) or source.output != _INPUT:
        return False
    threshold = steps.get(source.step)
    return threshold is not None and threshold.tool is not None and (
        threshold.tool.id, threshold.tool.version
    ) == ("python-dem-threshold", 1)


def _read_report(payload: bytes, source: Path, area_crs: str) -> None:
    report = json.loads(payload)
    if not isinstance(report, dict):
        raise ValueError("Expected an area report")
    metadata = read_raster_metadata(source)
    target = CRS.from_user_input(area_crs).to_2d()
    same_grid = CRS.from_user_input(metadata.crs).to_2d().equals(target, ignore_axis_order=True)
    fields = {
        "source_crs": metadata.crs, "area_crs": target.to_string(), "area_method": "equal_area",
        "area_unit": "square_meter", "resampling": "none",
        "cell_area_method": "affine_determinant" if same_grid else "projected_cell_boundary",
    }
    if any(report.get(key) != value for key, value in fields.items()):
        raise ValueError("Cached report differs from the current measurement")
    count_fields = ("valid_pixel_count", "selected_pixel_count", "unselected_pixel_count", "nodata_pixel_count")
    counts = [report.get(key) for key in count_fields]
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("Invalid cached pixel counts")
    valid, selected, background, nodata = counts
    rows = report.get("classes")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Expected cached class records")
    identifiers, pixels, areas = ([row.get(key) for row in rows] for key in ("class_id", "pixel_count", "area_m2"))
    if (
        any(type(label) is not int or not 0 < label < 255 for label in identifiers)
        or identifiers != sorted(set(identifiers))
        or any(type(count) is not int or count <= 0 for count in pixels)
        or any(type(area) not in (int, float) or not math.isfinite(area) or area <= 0 for area in areas)
        or sum(pixels) != selected or selected + background != valid
        or valid + nodata != metadata.width * metadata.height
        or type(report.get("total_area_m2")) not in (int, float)
        or report["total_area_m2"] != math.fsum(areas)
    ):
        raise ValueError("Cached class totals are invalid")


class DemAreaReuse:
    """Keep a pending cache write until its source run has passed its checks."""

    def __init__(self, store: StepCache, database: Path, plan: AnalysisPlan, worker: Path) -> None:
        self.store = store
        self.database = database
        self.plan = plan
        self.worker = worker
        self._before: str | None = None
        self._pending: tuple[str, str, str, bytes] | None = None

    @classmethod
    def for_plan(cls, store, database, plan, implementations):
        worker = implementations.get("geopilot.summarize_dem_area")
        if worker is None or not _eligible(plan) or Path(worker).resolve() != _WORKER:
            return None
        return cls(store, Path(database), plan, _WORKER)

    def _identity(self, step: PreparedStep) -> tuple[str, Path] | None:
        if (step.request["step_key"] != _STEP or (step.contract.id, step.contract.version) != _AREA_TOOL
                or step.contract.implementation != "geopilot.summarize_dem_area"
                or step.contract.inputs != {_INPUT: "raster_classification"}
                or step.contract.outputs != {_OUTPUT: "json"}):
            return None
        ref = step.request["inputs"][_INPUT]
        target = step.request["output_targets"][_OUTPUT]
        if ref.get("layer") is not None or target.get("layer") is not None:
            return None
        source = resolve_step_path(step.workdir, ref["path"], field=f"inputs.{_INPUT}")
        if source.suffix.lower() not in {".tif", ".tiff"} or _sidecars(source):
            return None
        runtime = _runtime_identity(step.workdir)
        if runtime is None:
            return None
        digest = fingerprint_file(source).digest
        if _sidecars(source):
            return None
        document = {
            "version": 1,
            "plan": self.plan.model_dump(mode="json", exclude={"id", "task_id", "version", "status", "goal", "issues"}),
            "step": step.request["step_key"], "contract": step.contract.model_dump(mode="json"),
            "worker": str(self.worker), "runtime": runtime, "parameters": step.request["parameters"],
            "input": {"role": _INPUT, "layer": None, "digest": digest},
        }
        return sha256(_canonical(document)).hexdigest(), source

    def restore(self, step: PreparedStep) -> tuple[StepReuse, dict] | None:
        if step.request["step_key"] != _STEP:
            return None
        created = None
        try:
            identity = self._identity(step)
            if identity is None:
                return None
            key, source = identity
            self._before = key
            entry = self.store.load(key)
            if entry is None:
                return None
            reuse = StepReuse(cache_key=key, source_run_id=entry.source_run_id,
                              source_step_key=entry.source_step_key, output_sha256=entry.output_sha256)
            request = StepRequest.model_validate(step.request)
            if not verify_reuse_source(self.database, reuse, request):
                return None
            _read_report(entry.payload, source, request.parameters["area_crs"])
            destination = resolve_step_path(step.workdir, request.output_targets[_OUTPUT].path, field=f"outputs.{_OUTPUT}")
            with worker_lease(step.workdir):
                if self._identity(step) != identity:
                    return None
                if step.cancel_requested is not None and step.cancel_requested():
                    raise AdapterError("The step was cancelled before restoring its cached output.", code="PROCESS_CANCELLED")
                with owned_output_staging(step.workdir, step.request, {}) as staging:
                    directory = Path(json.loads(staging[OUTPUT_STAGING_ENV])[str(destination)])
                    temporary = directory / "area.json"
                    with temporary.open("xb") as stream:
                        stream.write(entry.payload)
                    os.link(temporary, destination)
                    created = destination
                outputs = _verify_outputs(step)
                if step.cancel_requested is not None and step.cancel_requested():
                    raise AdapterError("The step was cancelled while restoring its cached output.", code="PROCESS_CANCELLED")
            return reuse, outputs
        except (OSError, ValueError, RuntimeError, OverflowError) as error:
            if created is not None:
                created.unlink(missing_ok=True)
            if isinstance(error, AdapterError) and error.code == "PROCESS_CANCELLED":
                raise
            return None

    def capture(self, step: PreparedStep, outputs: dict) -> None:
        if step.request["step_key"] != _STEP or self._before is None:
            return
        try:
            identity = self._identity(step)
            if identity is None or identity[0] != self._before:
                return
            key, source = identity
            payload = read_reuse_output(step.workdir, outputs[_OUTPUT]["path"])
            _read_report(payload, source, step.request["parameters"]["area_crs"])
            self._pending = (key, step.request["run_id"], step.request["step_key"], payload)
        except (OSError, ValueError, RuntimeError, OverflowError):
            return

    def publish(self) -> None:
        if self._pending is not None:
            try:
                self.store.save(*self._pending)
            except (OSError, ValueError):
                pass
