"""Browse published local script runs through their own file manifest."""

import json
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
from shutil import copyfileobj
from stat import S_ISREG
from tempfile import TemporaryDirectory
from typing import Annotated, BinaryIO, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Path as RoutePath, Query, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from geopilot.api.spatial_preview import vector_preview
from geopilot.data.geopackage_reader import list_geopackage_layers
from geopilot.domain.errors import ErrorDetail
from geopilot.persistence.workspace import _open_path
from geopilot.scripts.models import ScriptSnapshot
from geopilot.scripts.runner import SCRIPT_LOG_PATH, ScriptOutput, ScriptRunResult, load_script_result
from geopilot.scripts.snapshot import _validate_run_id, load_script_snapshot


_VECTOR_SUFFIXES = {".gpkg", ".geojson"}
_LOG_TAIL_BYTES = 128 * 1024
_RunStatus = Literal["NOT_STARTED", "RESULT_PENDING", "SUCCEEDED", "FAILED"]


class ScriptRunSummary(BaseModel):
    script_run_id: str
    script_id: str
    version: int
    created_at: datetime
    status: _RunStatus
    log_available: bool


class ScriptOutputView(ScriptOutput):
    index: int
    readable: bool
    preview_available: bool


class ScriptRunView(ScriptRunSummary):
    exit_code: int | None
    timed_out: bool
    started_at: datetime | None
    finished_at: datetime | None
    error: ErrorDetail | None
    outputs: list[ScriptOutputView]


class ScriptRuns(BaseModel):
    runs: list[ScriptRunSummary]


def _problem(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _snapshot(root: Path, run_id: str) -> ScriptSnapshot:
    try:
        _validate_run_id(run_id)
    except ValueError as error:
        raise _problem(404, "SCRIPT_RUN_NOT_FOUND", "The script run does not exist.") from error
    try:
        return load_script_snapshot(root, run_id)
    except FileNotFoundError as error:
        raise _problem(404, "SCRIPT_RUN_NOT_FOUND", "The script snapshot is unavailable.") from error
    except (OSError, ValueError) as error:
        raise _problem(422, "SCRIPT_RUN_UNREADABLE", "Could not read the script snapshot.") from error


def _open_file(root: Path, run_id: str, relative: str) -> tuple[BinaryIO, int]:
    # Callers use fixed names or validated relative paths from ScriptRunResult.
    path = PurePosixPath(relative)
    with _open_path(root, ("scripts", run_id, *path.parts[:-1])) as directory:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not S_ISREG(info.st_mode):
            raise ValueError("Expected a regular local file")
        return os.fdopen(fd, "rb"), info.st_size
    except BaseException:
        os.close(fd)
        raise


def _open_output(root: Path, run_id: str, output: ScriptOutput) -> BinaryIO:
    stream, size = _open_file(root, run_id, output.path)
    if size != output.size_bytes:
        stream.close()
        raise ValueError("The output size differs from the script manifest")
    return stream


def _load_run(root: Path, run_id: str) -> tuple[ScriptRunSummary, ScriptRunResult | None]:
    snapshot = _snapshot(root, run_id)
    try:
        result = load_script_result(root, run_id)
    except FileNotFoundError:
        result = None
    except (OSError, ValueError) as error:
        raise _problem(422, "SCRIPT_RUN_UNREADABLE", "Could not read the script result.") from error
    if result is not None and (result.script_id, result.version) != (snapshot.script_id, snapshot.version):
        raise _problem(422, "SCRIPT_RUN_UNREADABLE", "The result does not match the published script snapshot.")
    try:
        log, _ = _open_file(root, run_id, SCRIPT_LOG_PATH)
        log.close()
        log_available = True
    except (OSError, ValueError):
        log_available = False
    return ScriptRunSummary(
        script_run_id=snapshot.script_run_id, script_id=snapshot.script_id, version=snapshot.version,
        created_at=snapshot.created_at, log_available=log_available,
        status=result.status.value if result is not None else "RESULT_PENDING" if log_available else "NOT_STARTED",
    ), result


def _output_view(root: Path, run_id: str, index: int, output: ScriptOutput) -> ScriptOutputView:
    try:
        _open_output(root, run_id, output).close()
        readable = True
    except (OSError, ValueError):
        readable = False
    return ScriptOutputView(
        **output.model_dump(), index=index, readable=readable,
        preview_available=readable and PurePosixPath(output.path).suffix.lower() in _VECTOR_SUFFIXES,
    )


def _locate_output(root: Path, run_id: str, index: int) -> tuple[ScriptOutput, BinaryIO]:
    _, result = _load_run(root, run_id)
    if result is None or index >= len(result.outputs):
        raise _problem(404, "SCRIPT_OUTPUT_NOT_FOUND", "The output is not in the script result manifest.")
    output = result.outputs[index]
    try:
        return output, _open_output(root, run_id, output)
    except FileNotFoundError as error:
        raise _problem(404, "SCRIPT_OUTPUT_MISSING", "The recorded script output is missing.") from error
    except (OSError, ValueError) as error:
        raise _problem(422, "SCRIPT_OUTPUT_UNREADABLE", "Could not read the recorded script output.") from error


def _stream_file(stream: BinaryIO, *, media_type: str, headers: dict[str, str] | None = None) -> StreamingResponse:
    return StreamingResponse(
        iter(lambda: stream.read(64 * 1024), b""), media_type=media_type,
        headers={"X-Content-Type-Options": "nosniff", **(headers or {})}, background=BackgroundTask(stream.close),
    )


def _preview(root: Path, run_id: str, output: ScriptOutput, stream: BinaryIO) -> Response:
    suffix = PurePosixPath(output.path).suffix.lower()
    if suffix not in _VECTOR_SUFFIXES:
        raise _problem(422, "PREVIEW_UNAVAILABLE", "This output does not have a vector preview.")
    try:
        if suffix == ".gpkg":
            if stream.read(16) != b"SQLite format 3\0":
                raise ValueError("Expected a GeoPackage file")
            for extension in ("-wal", "-journal"):
                try:
                    sidecar, size = _open_file(root, run_id, output.path + extension)
                except FileNotFoundError:
                    continue
                sidecar.close()
                if size:
                    raise ValueError("Vector preview requires a closed GeoPackage without a pending journal")
        else:
            document = json.load(stream)
            if not isinstance(document, dict) or document.get("type") not in ("FeatureCollection", "Feature"):
                raise ValueError("Expected a GeoJSON feature document")
        stream.seek(0)
        # GDAL reopens the checked bytes in a private path with a known extension.
        with TemporaryDirectory(prefix="geopilot-script-preview-") as directory:
            path = Path(directory) / f"preview{suffix}"
            with path.open("xb") as target:
                copyfileobj(stream, target)
            layer = None
            if suffix == ".gpkg":
                layers = [name for name, geometry in list_geopackage_layers(path) if geometry is not None]
                if len(layers) != 1:
                    raise ValueError("GeoPackage preview requires one spatial layer")
                layer = layers[0]
            return vector_preview(path, layer)
    except (OSError, ValueError) as error:
        raise _problem(422, "PREVIEW_FAILED", str(error)) from error


def script_routes(workspace_root: str | Path) -> APIRouter:
    root = Path(workspace_root).resolve()
    router = APIRouter(prefix="/api/script-runs")

    @router.get("")
    def list_runs() -> ScriptRuns:
        try:
            with _open_path(root, ("scripts",)) as directory:
                names = os.listdir(directory)
        except FileNotFoundError:
            return ScriptRuns(runs=[])
        except OSError as error:
            raise _problem(422, "SCRIPT_WORKSPACE_UNREADABLE", "Could not read the script workspace.") from error
        runs = []
        for name in names:
            if name == "drafts":
                continue
            try:
                summary, _ = _load_run(root, name)
            except HTTPException:
                continue
            runs.append(summary)
        return ScriptRuns(runs=sorted(runs, key=lambda item: (item.created_at, item.script_run_id), reverse=True))

    @router.get("/{script_run_id}")
    def read_run(script_run_id: str) -> ScriptRunView:
        summary, result = _load_run(root, script_run_id)
        return ScriptRunView(
            **summary.model_dump(), exit_code=result.exit_code if result is not None else None,
            timed_out=result.timed_out if result is not None else False,
            started_at=result.started_at if result is not None else None,
            finished_at=result.finished_at if result is not None else None,
            error=result.error if result is not None else None,
            outputs=[_output_view(root, script_run_id, index, output)
                     for index, output in enumerate(result.outputs)] if result is not None else [],
        )

    @router.get("/{script_run_id}/log", response_class=PlainTextResponse)
    def read_log(script_run_id: str, tail: Annotated[int | None, Query(ge=1, le=1000)] = None) -> Response:
        _snapshot(root, script_run_id)
        try:
            stream, size = _open_file(root, script_run_id, SCRIPT_LOG_PATH)
            if tail is None:
                return _stream_file(stream, media_type="text/plain; charset=utf-8")
            with stream:
                stream.seek(max(0, size - _LOG_TAIL_BYTES))
                text = stream.read(_LOG_TAIL_BYTES).decode("utf-8", errors="replace")
            return PlainTextResponse("".join(text.splitlines(keepends=True)[-tail:]))
        except FileNotFoundError as error:
            raise _problem(404, "SCRIPT_LOG_NOT_FOUND", "The script process log is unavailable.") from error
        except (OSError, ValueError) as error:
            raise _problem(422, "SCRIPT_LOG_UNREADABLE", "Could not read the script process log.") from error

    @router.get("/{script_run_id}/outputs/{index}/file")
    def download(script_run_id: str, index: Annotated[int, RoutePath(ge=0)]) -> Response:
        output, stream = _locate_output(root, script_run_id, index)
        filename = quote(PurePosixPath(output.path).name, safe="")
        return _stream_file(stream, media_type="application/octet-stream", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}", "Content-Length": str(output.size_bytes),
        })

    @router.get("/{script_run_id}/outputs/{index}/preview")
    def preview(script_run_id: str, index: Annotated[int, RoutePath(ge=0)]) -> Response:
        output, stream = _locate_output(root, script_run_id, index)
        with stream:
            return _preview(root, script_run_id, output, stream)

    return router
