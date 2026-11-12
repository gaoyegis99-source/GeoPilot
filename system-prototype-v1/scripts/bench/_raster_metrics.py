"""Resource and environment observations for the isolated raster benchmark."""

import math
import os
import platform
import resource
import shutil
import statistics
import subprocess
import threading
import time
from pathlib import Path

import numpy
import pyogrio
import pyproj
import rasterio
import shapely


INTERVAL_NS = 50_000_000
FILE_BUDGET = 4096
RUNTIME = {
    "GEOPILOT_TELEMETRY": "1", "GDAL_CACHEMAX": "64", "GDAL_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1", "PROJ_NETWORK": "OFF", "PYTHONDONTWRITEBYTECODE": "1",
}


def metric(value, *, status="recorded", reason=None):
    return {"status": status if value is not None else "unavailable", "value": value, "reason": reason}


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _command(*command):
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=15).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def environment(root, output, source_commit):
    cpuinfo = _read("/proc/cpuinfo") or ""
    sockets = [{key.strip(): value.strip() for key, _, value in (line.partition(":") for line in cpu.splitlines())}
               for cpu in cpuinfo.split("\n\n") if cpu]
    physical = {(entry["physical id"], entry["core id"]) for entry in sockets if {"physical id", "core id"} <= entry.keys()}
    memory = dict(line.split(":", 1) for line in (_read("/proc/meminfo") or "").splitlines())
    mount = _command("findmnt", "-T", str(output), "-no", "SOURCE,FSTYPE")
    source = mount.split()[0] if mount else None
    device = _command("lsblk", "-ndo", "ROTA,TYPE,MODEL", source) if source and source.startswith("/dev/") else None
    qgis = os.environ.get("GEOPILOT_QGIS_PROCESS") or shutil.which("qgis_process")
    return {
        "commit": source_commit or _command("git", "-C", str(root), "rev-parse", "HEAD"),
        "os": platform.platform(), "os_release": _read("/etc/os-release"), "kernel": platform.release(),
        "cpu_model": sockets[0].get("model name") if sockets else None,
        "logical_cores": os.cpu_count(), "physical_cores": len(physical) if physical else None,
        "affinity": sorted(os.sched_getaffinity(0)), "load_average_at_start": os.getloadavg(),
        "ram": memory.get("MemTotal", "").strip() or None, "swap": memory.get("SwapTotal", "").strip() or None,
        "cgroups": _cgroups(),
        "storage": {"mount_source_and_filesystem": mount, "device_rotational_type_model": device,
                    "available_bytes": shutil.disk_usage(output).free},
        "versions": {
            "python": platform.python_version(), "qgis": _command(qgis, "--version") if qgis else None,
            "gdal_python": rasterio.__gdal_version__, "gdal_vector_python": pyogrio.__gdal_version_string__,
            "rasterio": rasterio.__version__, "pyogrio": pyogrio.__version__, "numpy": numpy.__version__,
            "proj_python": pyproj.proj_version_str, "proj_database": pyproj.database.get_database_metadata("EPSG.VERSION"),
            "geos_python": shapely.geos_version_string,
        },
        "runtime_environment": {key: os.environ.get(key) for key in RUNTIME},
        "os_page_cache": "uncontrolled",
    }


def _cgroups():
    membership = _read("/proc/self/cgroup") or ""
    limits = {}
    for line in membership.splitlines():
        _, controller, group = line.split(":", 2)
        if not controller:
            files = ("cpu.max", "memory.max", "memory.swap.max", "cpuset.cpus.effective")
            base = Path("/sys/fs/cgroup") / group.lstrip("/")
            limits.update({f"v2:{group}/{name}": _read(base / name) for name in files})
            continue
        for name, files in {
            "cpu": ("cpu.cfs_quota_us", "cpu.cfs_period_us"), "cpuset": ("cpuset.cpus",),
            "memory": ("memory.limit_in_bytes", "memory.memsw.limit_in_bytes"),
        }.items():
            if name not in controller.split(","):
                continue
            path = Path("/sys/fs/cgroup") / name
            parts = Path(group.lstrip("/")).parts
            for depth in range(len(parts), -1, -1):
                base = path.joinpath(*parts[:depth])
                for field in files:
                    value = _read(base / field)
                    if value is not None:
                        limits[str(base / field)] = value
    return {"membership": membership, "limits": limits}


def _tree_size(root):
    identities = {}
    transient = set()
    pending = [(root, False)]
    visited = 0
    complete = True
    while pending:
        directory, inside_transient = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > FILE_BUDGET:
                        return sum(identities.values()), sum(identities[key] for key in transient), False
                    if entry.is_symlink():
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name != "inputs":
                                pending.append((entry.path, inside_transient or entry.name.startswith(".")))
                        elif entry.is_file(follow_symlinks=False):
                            stat = entry.stat(follow_symlinks=False)
                            identity = (stat.st_dev, stat.st_ino)
                            identities[identity] = stat.st_size
                            if inside_transient:
                                transient.add(identity)
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
    return sum(identities.values()), sum(identities[key] for key in transient), complete


def file_bytes(paths):
    files = {}
    for path in paths:
        stat = path.stat()
        files[(stat.st_dev, stat.st_ino)] = stat.st_size
    return sum(files.values())


def _rss():
    stat = _read("/proc/self/statm")
    return None if stat is None else int(stat.split()[1]) * os.sysconf("SC_PAGE_SIZE")


def _io():
    contents = _read("/proc/self/io")
    if contents is None:
        return {}
    return {key: int(value) for key, value in (line.split(":", 1) for line in contents.splitlines())}


def _cpu():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return round((usage.ru_utime + usage.ru_stime) * 1e9)


class Sampler:
    def __init__(self, directory, *, rss=True):
        self.directory = directory
        self.with_rss = rss
        self.peak_rss = None
        self.peak_bytes = 0
        self.peak_temp = 0
        self.samples = 0
        self.complete = True
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _sample(self):
        size, temporary, complete = _tree_size(self.directory)
        self.peak_bytes = max(self.peak_bytes, size)
        self.peak_temp = max(self.peak_temp, temporary)
        self.complete &= complete
        if self.with_rss and (observed := _rss()) is not None:
            self.peak_rss = max(self.peak_rss or 0, observed)
        self.samples += 1

    def _loop(self):
        while not self.stop.wait(INTERVAL_NS / 1e9):
            self._sample()

    def __enter__(self):
        self._sample()
        self.thread.start()
        self.start_cpu = _cpu()
        self.start_io = _io()
        return self

    def __exit__(self, *_):
        self.end_cpu = _cpu()
        self.end_io = _io()
        self.stop.set()
        self.thread.join()
        self._sample()

    def document(self):
        status = "sampled" if self.complete and self.samples > 2 else "partial"
        reason = "scan_limit_or_error" if not self.complete else ("endpoint_samples_only" if self.samples <= 2 else None)
        return {
            "peak_workspace_bytes": metric(self.peak_bytes, status=status, reason=reason),
            "peak_temp_bytes": metric(self.peak_temp, status=status, reason=reason),
            "sampling_interval_ns": INTERVAL_NS, "samples": self.samples,
            "scope": "regular workspace files, deduplicated by inode, excluding staged inputs; includes result files",
            "temp_scope": "temporary producer directories with dot-prefixed names inside the sampled workspace",
        }

    def resources(self):
        counters = {
            name: metric(
                self.end_io[counter] - self.start_io[counter]
                if counter in self.start_io and counter in self.end_io else None,
                reason=None if counter in self.start_io and counter in self.end_io else "proc_self_io_unavailable",
            )
            for name, counter in (("disk_read_bytes", "read_bytes"), ("disk_write_bytes", "write_bytes"))
        }
        return {
            "cpu_ns": metric(self.end_cpu - self.start_cpu), **counters,
            "peak_rss_bytes": metric(
                self.peak_rss, status="sampled" if self.samples > 2 else "partial",
                reason="proc_self_statm_unavailable" if self.peak_rss is None else
                       ("endpoint_samples_only" if self.samples <= 2 else None),
            ),
        }


def distribution(values):
    ordered = sorted(values)
    return {"median": statistics.median(ordered), "p95_nearest_rank": ordered[math.ceil(0.95 * len(ordered)) - 1]}
