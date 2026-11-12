"""Install and start one GeoPilot matrix row from an isolated Git snapshot."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


REPO = Path(__file__).resolve().parents[3]
PROTOTYPE = "system-prototype-v1"
SOURCE = ("src", "config", "frontend", "requirements.txt")
LOOPBACK = "127.0.0.1"


def git(*args):
    return subprocess.check_output(["git", "-C", str(REPO), *args], text=True).strip()


def extract_source(commit, scratch):
    archive = scratch / "source.tar"
    subprocess.run([
        "git", "-C", str(REPO), "archive", "--format=tar", f"--output={archive}", commit, "--",
        *(f"{PROTOTYPE}/{name}" for name in SOURCE),
    ], check=True)
    with tarfile.open(archive) as snapshot:
        snapshot.extractall(scratch, filter="data")
    return scratch / PROTOTYPE


def install_step(report, output, name, argv, cwd, env, *, timeout=900, split_stderr=False):
    log = output / f"{name}.log"
    step = {"name": name, "argv": list(map(str, argv)), "cwd": str(cwd), "log": log.name, "exit_code": None}
    report["steps"].append(step)
    with log.open("xb") as stdout:
        if split_stderr:
            stderr_path = output / f"{name}.stderr.log"
            step["stderr_log"] = stderr_path.name
            with stderr_path.open("xb") as stderr:
                result = subprocess.run(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr, timeout=timeout)
        else:
            result = subprocess.run(argv, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT, timeout=timeout)
    step["exit_code"] = result.returncode
    result.check_returncode()
    return log


def free_ports():
    with socket.socket() as backend, socket.socket() as frontend:
        backend.bind((LOOPBACK, 0))
        frontend.bind((LOOPBACK, 0))
        return backend.getsockname()[1], frontend.getsockname()[1]


def stop_server(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


@contextmanager
def server(report, output, name, argv, cwd, env):
    log = output / f"{name}.log"
    step = {"name": name, "argv": list(map(str, argv)), "cwd": str(cwd), "log": log.name, "exit_code": None}
    report["steps"].append(step)
    with log.open("xb") as stream:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            yield process
        finally:
            stop_server(process)
            step["exit_code"] = process.returncode


def wait_http(process, url, *, timeout=45):
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Server exited with code {process.returncode} before responding on {url}")
        try:
            with opener.open(url, timeout=2) as response:
                if response.status == 200:
                    return response.read(8 * 1024**2)
        except (URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Server did not respond on {url} within {timeout} seconds")


def verify_install(report, output, scratch, args):
    root = extract_source(report["commit"], scratch)
    frontend = root / "frontend"
    data = scratch / "data"
    data.mkdir()
    venv = scratch / "venv"
    python = venv / "bin" / "python"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    env.pop("PYTHONPATH", None)
    install_step(report, output, "venv", [sys.executable, "-m", "venv", venv], root, env)
    install_step(report, output, "pip-install", [
        python, "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
        "--only-binary=:all:", "-r", "requirements.txt",
    ], root, env)
    install_step(report, output, "pip-check", [python, "-m", "pip", "check"], root, env)
    install_step(report, output, "npm-ci", [
        "npm", "ci", "--include=dev", "--no-audit", "--no-fund", "--cache", scratch / "npm-cache",
    ], frontend, env)
    install_step(report, output, "npm-build", ["npm", "run", "build"], frontend, env)

    api_port, frontend_port = free_ports()
    api_url = f"http://{LOOPBACK}:{api_port}"
    frontend_url = f"http://{LOOPBACK}:{frontend_port}"
    database, workspace, config = data / "project.sqlite", data / "workspace", root / "config"
    env.update({
        "PYTHONPATH": str(root / "src"), "GEOPILOT_API_URL": api_url,
        "QT_QPA_PLATFORM": "offscreen", "QGIS_CUSTOM_CONFIG_PATH": str(scratch / "qgis-config"),
    })
    doctor_log = install_step(report, output, "doctor", [
        python, "-m", "geopilot.diagnostics", "--json", "--project-root", root,
        "--database", database, "--workspace", workspace, "--qgis-process", args.qgis_process,
        "--port", str(api_port), "--frontend-port", str(frontend_port),
    ], root, env, split_stderr=True)
    doctor = json.loads(doctor_log.read_bytes())
    report["diagnostics"] = doctor
    if doctor["status"] not in {"PASS", "WARN"}:
        raise RuntimeError("Environment diagnostics rejected the installation")
    with server(report, output, "backend", [
        python, "-m", "geopilot.api.server", "--database", database, "--workspace", workspace,
        "--config", config, "--qgis-process", args.qgis_process, "--port", str(api_port),
    ], root, env) as api:
        api_schema = json.loads(wait_http(api, f"{api_url}/openapi.json"))
        if "/api/methods" not in api_schema.get("paths", {}):
            raise RuntimeError("Backend OpenAPI is missing the method catalog")
        with server(report, output, "frontend", [
            "npm", "run", "dev", "--", "--port", str(frontend_port), "--strictPort",
        ], frontend, env) as web:
            index = wait_http(web, f"{frontend_url}/")
            if b'id="root"' not in index:
                raise RuntimeError("Frontend did not serve the GeoPilot workbench")
            direct = json.loads(wait_http(api, f"{api_url}/api/methods"))
            proxied = json.loads(wait_http(web, f"{frontend_url}/api/methods"))
            if direct != proxied or not any(entry.get("available") for entry in direct.get("methods", [])):
                raise RuntimeError("Frontend proxy did not return the same catalog with an available method")
    report["startup"] = {"openapi": "PASS", "workbench_html": "PASS", "method_catalog_proxy": "PASS"}
    (output / "methods.json").write_text(json.dumps(direct, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="matrix row label")
    parser.add_argument("--ref", default="HEAD", help="committed GeoPilot source revision")
    parser.add_argument("--qgis-process", type=Path, required=True, help="provisioned QGIS Processing executable")
    parser.add_argument("--output", type=Path, required=True, help="new directory for raw evidence")
    args = parser.parse_args()
    args.qgis_process = args.qgis_process.absolute()
    output = args.output.absolute()
    try:
        output.mkdir()
    except OSError as error:
        parser.error(str(error))
    report = {
        "schema_version": 1, "label": args.label, "os": platform.freedesktop_os_release(),
        "platform": platform.platform(), "python_launcher": platform.python_version(), "steps": [], "status": "FAIL",
    }
    try:
        report["commit"] = git("rev-parse", "--verify", "--end-of-options", f"{args.ref}^{{commit}}")
        if not args.qgis_process.is_file() or not os.access(args.qgis_process, os.X_OK):
            raise ValueError("QGIS Processing must be an existing executable file")
        with tempfile.TemporaryDirectory(prefix="geopilot-install-check-") as directory:
            verify_install(report, output, Path(directory), args)
        report["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as error:
        report["error"] = str(error)
    report["exit_code"] = int(report["status"] != "PASS")
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{report['status']} {output / 'report.json'}")
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
