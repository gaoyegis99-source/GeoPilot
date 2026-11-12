"""Inspect and verify a GeoPilot method registration candidate for human review."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import yaml
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from geopilot.application.land_use_checks import LAND_USE_CHECKS
from geopilot.application.remote_sensing_checks import REMOTE_SENSING_CHECKS
from geopilot.application.run_checks import RUN_CHECKS
from geopilot.checks.raster_checks import DEM_ARTIFACT_RULES
from geopilot.methods.loader import _UniqueKeySafeLoader
from geopilot.scripts.registration import RegistrationCandidate, RegistrationError, validate_manual_registration

from lint import LintError, lint_methods
from template import TemplateError, generate_template
from scripts.methods.trial import TrialError, run_trial


SCHEMA_VERSION = 1
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config"
REGISTERED_RULES = frozenset((*RUN_CHECKS, *LAND_USE_CHECKS, *REMOTE_SENSING_CHECKS, *DEM_ARTIFACT_RULES))


def issue(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


def emit(command: str | None, *, data=None, errors=()) -> None:
    document = json.dumps({
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": "error" if errors else "ok",
        "data": data,
        "errors": list(errors),
    }, allow_nan=False, indent=2)
    sys.stdout.buffer.write((document + "\n").encode("ascii"))


class MethodParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        emit(None, errors=[issue("CLI_USAGE", "$", message)])
        self.exit(2)


class InputPathError(OSError):
    def __init__(self, field: str, message: str, *, code: str = "INPUT_READ_ERROR") -> None:
        super().__init__(message)
        self.code = code
        self.field = field


def read_candidate(path: Path) -> RegistrationCandidate:
    try:
        if not path.is_file():
            raise OSError(f"Expected a regular candidate file: {path}")
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise InputPathError("candidate", str(error)) from error
    return RegistrationCandidate.model_validate(yaml.load(text, Loader=_UniqueKeySafeLoader))


def read_only_database(path: Path) -> Path:
    try:
        if not path.is_file():
            raise OSError(f"Expected an existing project database: {path}")
        database = path.resolve(strict=True)
        with database.open("rb") as stream:
            header = stream.read(20)
        sidecars = (database.with_name(database.name + suffix) for suffix in ("-wal", "-shm", "-journal"))
        has_sidecars = any(sidecar.exists() or sidecar.is_symlink() for sidecar in sidecars)
    except OSError as error:
        raise InputPathError("database", str(error)) from error
    if not header.startswith(b"SQLite format 3\0") or len(header) < 20:
        raise InputPathError("database", "Expected a SQLite database header", code="DATABASE_READ_ERROR")
    # Even mode=ro can create WAL/SHM files. Check the file format before connecting.
    if header[18:20] != b"\x01\x01" or has_sidecars:
        raise InputPathError(
            "database", "Use a quiet rollback-journal SQLite review copy with no sidecars",
            code="DATABASE_REVIEW_COPY_REQUIRED",
        )
    return database


def build_parser() -> argparse.ArgumentParser:
    parser = MethodParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"GeoPilot method CLI {SCHEMA_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, summary in (
        ("inspect", "Display the normalized candidate structure"),
        ("validate", "Verify the candidate against an existing trial"),
        ("report", "Verify the trial and emit a report for human review"),
    ):
        command = commands.add_parser(name, help=summary, description=summary, allow_abbrev=False)
        command.add_argument("--candidate", type=Path, required=True, help="UTF-8 YAML registration candidate")
        if name != "inspect":
            command.add_argument("--database", type=Path, required=True, help="existing rollback-mode SQLite database")
            command.add_argument("--workspace", type=Path, required=True, help="existing trial workspace root")
    template = commands.add_parser(
        "template", help="Generate a method draft for an exact local tool", allow_abbrev=False,
    )
    template.add_argument("--tool", required=True, help="registered tool ID@VERSION")
    template.add_argument("--method", required=True, help="proposed method ID@VERSION")
    template.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                          help="configuration root containing tools/")
    lint = commands.add_parser(
        "lint", help="Check a method draft or the local method catalog", allow_abbrev=False,
    )
    lint.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="configuration root")
    lint.add_argument("--method", type=Path, help="single method definition to check strictly")
    trial = commands.add_parser(
        "trial", help="Run a reference case in a new isolated project", allow_abbrev=False,
    )
    trial.add_argument("--package", type=Path, required=True, help="reference package manifest.json")
    trial.add_argument("--work-dir", type=Path, required=True, help="new trial directory in an existing parent")
    trial.add_argument("--case", help="positive or expected-failure case ID")
    trial.add_argument("--method", type=Path, help="external candidate YAML matching the package ID and version")
    trial.add_argument("--qgis-process", type=Path, help="trusted local QGIS Processing executable")
    trial.add_argument("--timeout", type=float, default=120, help="trial deadline in seconds (default: 120)")
    return parser


def execute(args: argparse.Namespace) -> dict:
    if args.command == "template":
        return generate_template(args.config, args.tool, args.method)
    if args.command == "lint":
        return lint_methods(args.config, args.method)
    if args.command == "trial":
        return run_trial(args.package, args.work_dir, case=args.case, candidate=args.method,
                         qgis_process=args.qgis_process, timeout=args.timeout)
    candidate = read_candidate(args.candidate)
    normalized = json.loads(candidate.model_dump_json())
    if args.command == "inspect":
        return {"scope": "candidate_structure", "candidate": normalized}
    database = read_only_database(args.database)
    try:
        if not args.workspace.is_dir():
            raise OSError(f"Expected an existing trial workspace: {args.workspace}")
    except OSError as error:
        raise InputPathError("workspace", str(error)) from error
    evidence = validate_manual_registration(
        database, args.workspace, candidate, registered_rules=REGISTERED_RULES,
    )
    result = {"scope": "manual_registration", "evidence": evidence.model_dump(mode="json")}
    if args.command == "report":
        result["candidate"] = normalized
        result["disposition"] = "manual_review_required"
    return result


def main() -> int:
    args = build_parser().parse_args()
    try:
        data = execute(args)
    except LintError as error:
        emit(args.command, errors=error.errors)
        return error.exit_code
    except (TemplateError, TrialError) as error:
        emit(args.command, errors=[issue(error.code, error.field, str(error))])
        return error.exit_code
    except InputPathError as error:
        emit(args.command, errors=[issue(error.code, error.field, str(error))])
        return 2
    except sqlite3.Error as error:
        emit(args.command, errors=[issue("DATABASE_READ_ERROR", "database", str(error))])
        return 2
    except (UnicodeError, RecursionError, yaml.YAMLError) as error:
        emit(args.command, errors=[issue("CANDIDATE_PARSE_ERROR", "candidate", str(error))])
        return 1
    except ValidationError as error:
        emit(args.command, errors=[
            issue("INVALID_CANDIDATE", ".".join(map(str, detail["loc"])) or "$", detail["msg"])
            for detail in error.errors(include_url=False, include_context=False, include_input=False)
        ])
        return 1
    except PydanticSerializationError:
        emit(args.command, errors=[issue("INVALID_CANDIDATE", "$", "Candidate values must be valid UTF-8 JSON")])
        return 1
    except RegistrationError as error:
        emit(args.command, errors=[issue(error.code, error.field, error.message)])
        return 1
    emit(args.command, data=data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
