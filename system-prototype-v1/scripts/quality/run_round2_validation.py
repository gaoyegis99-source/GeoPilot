"""Run release validation from one clean detached GeoPilot checkout."""

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from build_source_inventory import git
from round2_release_manifest import _FIXED_GIS_MAPPING, _file_path, _observed_suite, fixed_gis_cases


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = "artifacts/reports/final/round2-validation"
REPRODUCTION = (
    "README.md", "docs/release/ROUND2_REPRODUCTION.md", "docs/deployment/install-matrix.md",
    "docs/round2/audit/p12-05.md", "docs/round2/audit/p12-07.md", "docs/round2/agent/p15-06.md",
    "docs/round2/deployment/p16-02.md",
)


def _clean_checkout(expected_commit: str, qgis: Path) -> None:
    actual_commit = git(ROOT, "rev-parse", "HEAD").decode("ascii").strip()
    if expected_commit != actual_commit or git(ROOT, "branch", "--show-current").strip():
        raise ValueError("Validation requires the exact source commit in a detached checkout")
    if git(ROOT, "status", "--porcelain=v1", "--untracked-files=normal"):
        raise ValueError("Validation checkout has tracked or untracked changes")
    managed = ROOT / ".environments/qgis-deb/qgis_process"
    if managed.exists() and managed.resolve() != qgis.resolve():
        raise ValueError("Checkout has a managed QGIS executable different from --qgis-process")


def _version(command: list[str], cwd: Path, env: dict) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=20, check=False)
    if result.returncode or not result.stdout.strip():
        raise ValueError(f"Version probe failed for {shlex.join(command)} (exit {result.returncode})")
    return " ".join(result.stdout.split())


def _run(command: list[str], cwd: Path, env: dict, log: Path, commit: str) -> None:
    with log.open("x", encoding="utf-8") as stream:
        print(f"commit {commit}", f"cwd {cwd}", f"command {shlex.join(command)}", sep="\n", file=stream, flush=True)
        result = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, check=False)
        print(f"\nexit {result.returncode}", file=stream)
    if result.returncode:
        raise ValueError(f"{log.stem} exited {result.returncode}. See {log}")


def _suite(name, command, cwd, env, versions, local_dir, repo_dir, commit, mapping, collection=None):
    log = local_dir / f"{name}.log"
    report = local_dir / f"{name}.xml"
    _run(command, cwd, env, log, commit)
    _observed_suite(name, report.read_bytes(), log.read_bytes(), commit, command, mapping, collection)
    return {
        "name": name, "command": command, "environment": versions, "exit_code": 0,
        "junit": f"{repo_dir}/{report.name}", "log": f"{repo_dir}/{log.name}",
    }


def run(args) -> Path:
    commit = args.commit
    _clean_checkout(commit, args.qgis_process)
    checkout = Path(git(ROOT, "rev-parse", "--show-toplevel").decode().strip())
    output = args.output.resolve()
    if output.is_relative_to(checkout):
        raise ValueError("Evidence output must be outside the validation checkout")
    prefix = git(ROOT, "rev-parse", "--show-prefix").decode().strip()
    repo_dir = prefix + REPORT_DIR
    reproduction = sorted({_file_path(path) for path in (args.reproduction or [prefix + path for path in REPRODUCTION])})
    evidence = {_file_path(path) for path in args.evidence}
    mapping = (ROOT / _FIXED_GIS_MAPPING).read_bytes()
    fixed_nodes = sorted({node for nodes in fixed_gis_cases(mapping).values() for node in nodes})
    output.mkdir()
    local_dir = output / "round2-validation"
    local_dir.mkdir()
    frontend = ROOT / "frontend"
    python = str(args.python.absolute())
    qgis = str(args.qgis_process.absolute())
    pytest = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    python_paths = [str(ROOT / "src"), *(str(path.resolve(strict=True)) for path in args.python_path)]
    backend_env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(python_paths),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "QT_QPA_PLATFORM": "offscreen",
        "GEOPILOT_QGIS_PROCESS": qgis,
        "GEOPILOT_BENCH_QGIS": qgis,
    }
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "GEOPILOT_AGENT_EVAL_REPORT"):
        backend_env.pop(key, None)
    frontend_env = os.environ.copy()
    operating_system = platform.platform()
    python_versions = {
        "os": operating_system,
        "python": _version([python, "--version"], ROOT, backend_env),
        "pytest": _version([python, "-m", "pytest", "--version"], ROOT, backend_env),
        "qgis": _version([qgis, "--version"], ROOT, backend_env),
        "qgis_executable": qgis,
        "pythonpath": backend_env["PYTHONPATH"],
    }
    frontend_versions = {
        "os": operating_system,
        "node": _version(["node", "--version"], frontend, frontend_env),
        "npm": _version(["npm", "--version"], frontend, frontend_env),
    }
    install = ["npm", "ci", "--include=dev", "--no-audit", "--no-fund"]
    if args.offline_npm_cache:
        install.extend(("--offline", "--cache", str(args.offline_npm_cache.resolve(strict=True))))
    _run([python, "-m", "pip", "list", "--format=json"], ROOT, backend_env, local_dir / "backend-dependencies.log", commit)
    evidence.add(f"{repo_dir}/backend-dependencies.log")
    frontend_steps = {"frontend-install": install, "frontend-build": ["npm", "run", "build"]}
    for name, command in frontend_steps.items():
        _run(command, frontend, frontend_env, local_dir / f"{name}.log", commit)
        evidence.add(f"{repo_dir}/{name}.log")
    frontend_test = [
        "npm", "test", "--", "--run", "--reporter=default", "--reporter=junit",
        f"--outputFile.junit={local_dir / 'frontend.xml'}",
    ]
    suites = [_suite("frontend", frontend_test, frontend, frontend_env, frontend_versions,
                     local_dir, repo_dir, commit, mapping)]
    collection_command = [*pytest, "--collect-only", "tests"]
    collection_log = local_dir / "backend-collection.log"
    _run(collection_command, ROOT, backend_env, collection_log, commit)
    collection = (collection_log.read_bytes(), collection_command)
    with TemporaryDirectory(prefix="geopilot-round2-validation-") as temporary:
        backend_test = [*pytest, "tests", f"--basetemp={Path(temporary) / 'backend'}",
                        f"--junitxml={local_dir / 'backend.xml'}"]
        backend = _suite("backend", backend_test, ROOT, backend_env, python_versions,
                         local_dir, repo_dir, commit, mapping, collection)
        backend["collection"] = {"command": collection_command, "log": f"{repo_dir}/{collection_log.name}"}
        suites.append(backend)
        fixed_test = [*pytest, *fixed_nodes, f"--basetemp={Path(temporary) / 'fixed_gis'}",
                      f"--junitxml={local_dir / 'fixed_gis.xml'}"]
        suites.append(_suite("fixed_gis", fixed_test, ROOT, backend_env, python_versions,
                             local_dir, repo_dir, commit, mapping))
    _clean_checkout(commit, args.qgis_process)
    receipt = {
        "schema_version": 1, "source_commit": commit, "suites": suites,
        "evidence": sorted(evidence), "reproduction": reproduction,
    }
    path = output / "round2-validation.json"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="full source commit already checked out detached")
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory outside the checkout")
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help="locked test environment's Python")
    parser.add_argument("--qgis-process", type=Path, required=True)
    parser.add_argument("--python-path", type=Path, action="append", default=[], help="additional dependency directory")
    parser.add_argument("--offline-npm-cache", type=Path)
    parser.add_argument("--evidence", action="append", default=[], help="repository-relative evidence path")
    parser.add_argument("--reproduction", action="append", default=[], help="repository-relative Markdown path")
    try:
        print(run(parser.parse_args()))
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
