"""Reopen a named upstream output before handing it to a consumer."""

from pathlib import Path

from geopilot.domain.step_io import StepFileRef, StepOutput
from geopilot.execution.step_support import AdapterError, _inspect_output, resolve_step_path


def resolve_named_output(workdir: Path, output: StepOutput, *, field: str) -> StepFileRef:
    """Validate the reported file and exact layer as the declared output type."""
    try:
        path = resolve_step_path(workdir, output.path, field=field)
        if not path.is_file():
            raise FileNotFoundError(f"Previous step output {output.path!r} is missing")
        layer = _inspect_output(path, output.type, output.layer)
    except FileNotFoundError as error:
        raise AdapterError(str(error), code="OUTPUT_MISSING", phase="preflight", field=field) from error
    except (OSError, RuntimeError, ValueError) as error:
        raise AdapterError(
            f"Cannot reopen previous step output {output.path!r}: {error}",
            code="OUTPUT_INVALID", phase="preflight", field=field,
        ) from error
    return StepFileRef(path=output.path, layer=layer)
