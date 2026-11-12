"""Read diagnostic files retained by the scheduler."""

from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

from geopilot.artifacts.service import list_artifacts
from geopilot.execution.step_support import _inspect_output, resolve_step_path
from geopilot.persistence.plan_repository import plan_from_row, row_for_plan
from geopilot.persistence.run_repository import get_run, list_run_steps
from geopilot.persistence.schema import open_database
from geopilot.plans.models import AnalysisPlan
from geopilot.runs.models import Run, RunStep


@dataclass(frozen=True)
class DiagnosticFile:
    step_key: str
    role: str
    type: str
    path: str
    layer: str | None
    readable: bool


def load_diagnostic_steps(connection: Connection, run: Run) -> tuple[tuple[RunStep, ...], AnalysisPlan | None]:
    steps = list_run_steps(connection, run.id)
    plan = plan_from_row(row_for_plan(connection, run.plan_id, run.plan_version)) if steps else None
    return steps, plan


def _readable_file(
    workdir: Path, step_key: str, role: str, kind: str, reference: str, layer: str | None = None,
) -> DiagnosticFile | None:
    try:
        path = resolve_step_path(workdir, reference, field=f"steps.{step_key}.{role}")
        if not path.is_file():
            return None
    except (OSError, ValueError):
        return None

    readable = True
    try:
        if kind == "log":
            with path.open("rb") as stream:
                stream.read(1)
        else:
            layer = _inspect_output(path, kind, layer)
    except (OSError, RuntimeError, ValueError):
        readable = False
    return DiagnosticFile(step_key, role, kind, reference, layer, readable)


def list_run_diagnostics(database_path: str | Path, run_id: str) -> tuple[DiagnosticFile, ...]:
    """Report only evidenced files, including existing outputs that cannot be reopened."""
    with open_database(database_path, read_only=True) as connection, connection:
        connection.execute("BEGIN")
        run = get_run(connection, run_id)
        if run is None:
            raise LookupError(f"Run {run_id!r} does not exist.")
        steps, plan = load_diagnostic_steps(connection, run)
        formal_paths = {artifact.uri for artifact in list_artifacts(connection, run_id)}
    return inspect_diagnostic_files(Path(run.workdir), steps, plan, formal_paths)


def inspect_diagnostic_files(
    workdir: Path, steps: tuple[RunStep, ...], plan: AnalysisPlan | None, formal_paths: set[str],
) -> tuple[DiagnosticFile, ...]:
    if plan is None:
        return ()
    declared_outputs = {step.key: step.outputs for step in plan.steps}
    files = []
    for step in steps:
        if step.request is not None:
            for role, target in step.request.output_targets.items():
                if target.path in formal_paths:
                    continue
                declared = declared_outputs[step.step_key][role]
                item = _readable_file(workdir, step.step_key, role, declared.type, target.path, target.layer)
                if item is not None:
                    files.append(item)
        log_path = step.log_path
        if log_path is None and step.request is not None:
            log_path = step.request.log_path
        if log_path is not None:
            log = _readable_file(workdir, step.step_key, "process_log", "log", log_path)
            if log is not None:
                files.append(log)
    return tuple(files)
