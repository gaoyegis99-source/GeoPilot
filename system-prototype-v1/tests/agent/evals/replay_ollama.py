"""Opt-in replay of the locked synthetic tasks using an isolated local Ollama."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory

from geopilot.domain.task import TaskGoal
from geopilot.execution.process_manager import capture_process
from geopilot.llm.context_builder import OutboundContextError
from geopilot.llm.providers.ollama import OllamaModelProvider

from eval_support import (
    LOCK, ROOT, EvaluationProject, _request, evaluate, load_cases, prompt_lock, score_report, sha256, write_new_json,
)


MODEL = "qwen3:4b-instruct"
DIGEST = "0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0"
VERSION = "0.24.0"
MANIFEST = "manifests/registry.ollama.ai/library/qwen3/4b-instruct"
PORT = 11434
ATTEMPT_TIMEOUT = 25
REPLAY_TIMEOUT = 210
PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


class SetupError(RuntimeError):
    pass


def not_run(code):
    return {
        "schema_version": 1, "dataset_id": "geopilot-agent-behavior-v1", "status": "NOT_RUN", "code": code,
        "provider": {"kind": "ollama", "model": MODEL, "expected_digest": DIGEST, "expected_version": VERSION},
        "cases": [],
    }


def _verify_lock(project, cases, lock):
    current = prompt_lock([])
    for field in ("schema_version", "prompt_version", "dataset_sha256", "suggestion_instructions_sha256",
                  "ollama_format_sha256"):
        if current[field] != lock.get(field):
            raise SetupError("PROMPT_LOCK_MISMATCH")
    for case in cases:
        try:
            request, _, _ = _request(case, project, TaskGoal(summary=case["goal"]))
            actual = sha256(request.model_dump_json().encode("utf-8"))
        except OutboundContextError:
            actual = None
        if case["id"] not in lock["cases"] or actual != lock["cases"][case["id"]]:
            raise SetupError("PROMPT_LOCK_MISMATCH")


def _command(*args):
    try:
        subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise SetupError("NAMESPACE_SETUP_FAILED") from error


def _read_metadata(path):
    connection = HTTPConnection("127.0.0.1", PORT, timeout=1)
    try:
        connection.request("GET", path)
        with connection.getresponse() as response:
            content = response.read(262145)
            if response.status != 200 or len(content) > 262144:
                raise SetupError("LOCAL_METADATA_UNAVAILABLE")
        return json.loads(content)
    finally:
        connection.close()


def _verify_manifest(store: Path):
    try:
        manifest = store / MANIFEST
        if not manifest.is_file() or manifest.stat().st_size > 262144:
            raise SetupError("LOCAL_MODEL_MISSING")
        if sha256(manifest.read_bytes()) != DIGEST:
            raise SetupError("MODEL_DIGEST_MISMATCH")
    except OSError as error:
        raise SetupError("LOCAL_MODEL_MISSING") from error


def _server_pin():
    version = _read_metadata("/api/version").get("version")
    tags = _read_metadata("/api/tags").get("models", [])
    model = [row for row in tags if row.get("name") == MODEL]
    if version != VERSION:
        raise SetupError("OLLAMA_VERSION_MISMATCH")
    if len(model) != 1 or str(model[0].get("digest")).removeprefix("sha256:") != DIGEST:
        raise SetupError("MODEL_DIGEST_MISMATCH")


def _wait_server(server):
    deadline = time.monotonic() + 15
    while server.poll() is None and time.monotonic() < deadline:
        try:
            _server_pin()
        except (OSError, HTTPException):
            time.sleep(0.1)
        else:
            return
    raise SetupError("OLLAMA_START_FAILED")


def _isolated(args):
    scratch = args.scratch
    if os.readlink("/proc/self/ns/net") == args.parent_network:
        raise SetupError("NETWORK_NAMESPACE_REQUIRED")
    _command("ip", "link", "set", "lo", "up")
    if [name for _, name in socket.if_nameindex()] != ["lo"] or len(
        Path("/proc/net/route").read_text(encoding="ascii").splitlines()
    ) != 1:
        raise SetupError("LOOPBACK_ONLY_REQUIRED")
    models = scratch / "models"
    models.mkdir()
    _command("mount", "--bind", str(args.models), str(models))
    _command("mount", "-o", "remount,bind,ro", str(models))
    if not os.statvfs(models).f_flag & os.ST_RDONLY:
        raise SetupError("READ_ONLY_MODEL_STORE_REQUIRED")
    _verify_manifest(models)
    project = EvaluationProject(scratch)
    cases = [case for case in load_cases() if case.get("live")]
    lock = json.loads(LOCK.read_bytes())
    _verify_lock(project, cases, lock)
    server_home = scratch / "server-home"
    server_home.mkdir()
    env = {
        "PATH": PATH, "HOME": str(server_home), "LC_ALL": "C.UTF-8", "OLLAMA_HOST": f"127.0.0.1:{PORT}",
        "OLLAMA_MODELS": str(models), "OLLAMA_NO_CLOUD": "1", "OLLAMA_NOPRUNE": "1",
        "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_CONTEXT_LENGTH": "4096",
        "OLLAMA_KEEP_ALIVE": "2m", "CUDA_VISIBLE_DEVICES": "-1", "HIP_VISIBLE_DEVICES": "-1",
    }
    with (scratch / "ollama.log").open("xb") as log, subprocess.Popen(
        [str(args.ollama_binary), "serve"], cwd=scratch, env=env, stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT,
    ) as server:
        try:
            _wait_server(server)
            provider = OllamaModelProvider(MODEL, port=PORT)
            rows = [evaluate(case, project, provider=provider, timeout=ATTEMPT_TIMEOUT) for case in cases]
            result = score_report(rows, {
                "kind": "ollama", "model": MODEL, "digest": DIGEST, "version": VERSION,
                "attempt_timeout_seconds": ATTEMPT_TIMEOUT, "context_length": 4096,
            }, lock)
            result.update({"status": "RECORDED", "isolation": {
                "interfaces": ["lo"], "ipv4_routes": 0, "read_only_model_store": True,
                "cloud_disabled": True, "pull_requested": False,
            }})
            try:
                _server_pin()
                _verify_manifest(models)
            except (OSError, HTTPException, SetupError, ValueError, TypeError, AttributeError):
                result.update({"status": "INCOMPLETE", "code": "MODEL_PIN_COULD_NOT_BE_RECONFIRMED"})
            else:
                result["provider"]["pin_verified_after"] = True
            return result
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


def _launch(args):
    if args.report.exists() or args.report.is_symlink() or not args.report.parent.is_dir():
        raise SystemExit("Choose a new report file in an existing directory")
    if not args.ollama_binary.is_file() or not args.models.is_dir():
        write_new_json(args.report, not_run("LOCAL_MODEL_RUNTIME_MISSING"))
        return 2
    with TemporaryDirectory(prefix="geopilot-p15-08-local-", dir="/dev/shm") as directory:
        scratch = Path(directory)
        args_vector = [
            "/usr/bin/unshare", "--user", "--map-root-user", "--net", "--mount", "--pid",
            "--fork", "--mount-proc", "--kill-child=KILL", "--", sys.executable, str(Path(__file__).resolve()),
            "--enable-local-model", "--ollama-binary", str(args.ollama_binary.resolve()),
            "--models", str(args.models.resolve()), "--report", str(args.report.resolve()),
            "--scratch", str(scratch), "--parent-network", os.readlink("/proc/self/ns/net"),
        ]
        try:
            outcome = capture_process(args_vector, cwd=scratch, timeout=REPLAY_TIMEOUT, env={
                "PATH": PATH, "HOME": str(scratch), "LC_ALL": "C.UTF-8", "PYTHONPATH": str(ROOT / "src"),
                "PYTHONDONTWRITEBYTECODE": "1",
            })
        except OSError:
            report = not_run("NAMESPACE_UNAVAILABLE")
        else:
            if outcome.timed_out:
                report = not_run("REPLAY_DEADLINE_EXPIRED")
            elif outcome.exit_code:
                report = not_run("NAMESPACE_PROCESS_FAILED")
            else:
                try:
                    report = json.loads(outcome.stdout)
                except ValueError:
                    report = not_run("REPLAY_REPORT_INVALID")
    write_new_json(args.report, report)
    return 0 if report["status"] == "RECORDED" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--enable-local-model", action="store_true", required=True)
    parser.add_argument("--ollama-binary", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--parent-network", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.scratch is None:
        return _launch(args)
    try:
        if not args.parent_network:
            raise SetupError("NETWORK_NAMESPACE_REQUIRED")
        report = _isolated(args)
    except SetupError as error:
        report = not_run(str(error))
    except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError):
        report = not_run("LOCAL_REPLAY_SETUP_FAILED")
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
