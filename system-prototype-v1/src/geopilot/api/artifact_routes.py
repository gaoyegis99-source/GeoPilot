"""Expose the formal artifact register and separate diagnostic files."""

import sqlite3
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from geopilot.api.spatial_preview import vector_preview
from geopilot.artifacts.models import Artifact
from geopilot.artifacts.raster_source import ArtifactRasterError, open_artifact_raster
from geopilot.artifacts.service import artifact_by_id, list_artifacts
from geopilot.artifacts.value_query import RasterValue, RasterValueError, query_raster_value
from geopilot.data import csv_output
from geopilot.execution.step_support import AdapterError, resolve_step_path
from geopilot.persistence.run_repository import get_run
from geopilot.persistence.schema import open_database
from geopilot.preview.raster_preview import (
    MAX_PREVIEW_SIZE,
    RENDERER_VERSION,
    Palette,
    RasterPreview,
    RasterPreviewDescriptor,
    RasterPreviewError,
    render_raster_preview,
)
from geopilot.preview.store import load_preview, publish_preview
from geopilot.runs.models import CheckStatus
from geopilot.scheduler.failure_handler import DiagnosticFile, inspect_diagnostic_files, load_diagnostic_steps
from geopilot.scheduler.output_resolver import resolve_named_output


class ArtifactStatus(Artifact):
    readable: bool


class RunArtifacts(BaseModel):
    run_id: str
    check_status: CheckStatus
    artifacts: list[ArtifactStatus]
    diagnostics: tuple[DiagnosticFile, ...]


class ArtifactTable(BaseModel):
    columns: list[str]
    rows: list[list[str]]
    truncated: bool


_PreviewSize = Annotated[int, Query(ge=1, le=MAX_PREVIEW_SIZE)]
_PreviewBand = Annotated[int, Query(gt=0)]
_PreviewFingerprint = Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")]
_Longitude = Annotated[float, Query(ge=-180, le=180, allow_inf_nan=False)]
_Latitude = Annotated[float, Query(ge=-90, le=90, allow_inf_nan=False)]
_MAX_TABLE_PREVIEW_CELL_BYTES = 8 * 1024 * 1024


def artifact_routes(database_path: str | Path) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/runs/{run_id}/artifacts")
    def read_artifacts(run_id: str) -> RunArtifacts:
        try:
            with open_database(database_path, read_only=True) as connection, connection:
                connection.execute("BEGIN")
                run = get_run(connection, run_id)
                if run is None:
                    raise LookupError(f"Run {run_id!r} does not exist.")
                published = list_artifacts(connection, run_id)
                steps, plan = load_diagnostic_steps(connection, run)
        except LookupError as error:
            raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": str(error)}) from error
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(
                status_code=500, detail={"code": "STORAGE_FAILED", "message": "Could not read the artifact register."},
            ) from error

        workdir = Path(run.workdir)
        diagnostics = inspect_diagnostic_files(workdir, steps, plan, {artifact.uri for artifact in published})
        items = []
        for artifact in published:
            readable = True
            try:
                resolve_named_output(workdir, artifact.output, field=f"artifacts.{artifact.role}")
            except AdapterError:
                readable = False
            items.append(ArtifactStatus(**artifact.model_dump(mode="python"), readable=readable))
        return RunArtifacts(run_id=run.id, check_status=run.check_status, artifacts=items, diagnostics=diagnostics)

    def locate_artifact(artifact_id: str) -> tuple[Artifact, Path, Path]:
        try:
            with open_database(database_path, read_only=True) as connection, connection:
                connection.execute("BEGIN")
                artifact = artifact_by_id(connection, artifact_id)
                if artifact is None:
                    raise HTTPException(status_code=404, detail={
                        "code": "ARTIFACT_NOT_FOUND", "message": "The artifact is not registered.",
                    })
                run = get_run(connection, artifact.run_id)
            if run is None:
                raise ValueError("The artifact has no stored run.")
        except (sqlite3.Error, ValueError) as error:
            raise HTTPException(status_code=500, detail={
                "code": "STORAGE_FAILED", "message": "Could not read the artifact register.",
            }) from error
        try:
            workdir = Path(run.workdir)
            path = resolve_step_path(workdir, artifact.uri, field="artifact.uri")
            if not path.is_file():
                raise FileNotFoundError
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail={
                "code": "ARTIFACT_FILE_MISSING", "message": "The registered artifact file is missing.",
            }) from error
        except (OSError, AdapterError) as error:
            raise HTTPException(status_code=422, detail={
                "code": "ARTIFACT_UNREADABLE", "message": "Could not open the registered artifact file.",
            }) from error
        return artifact, workdir, path

    def raster_display(
        artifact_id: str, *, palette: Palette, max_size: int, band: int, source_fingerprint: str | None = None,
    ) -> RasterPreview:
        artifact, workdir, path = locate_artifact(artifact_id)
        query = urlencode({
            "palette": palette, "max_size": max_size, "band": band, "source_fingerprint": artifact.fingerprint,
        })
        image_url = f"/api/artifacts/{quote(artifact.id, safe='')}/raster-preview.png?{query}"
        variant = {
            "source_fingerprint": artifact.fingerprint, "palette": palette, "max_size": max_size,
            "band": band, "renderer_version": RENDERER_VERSION,
        }
        try:
            with open_artifact_raster(artifact, path, source_fingerprint=source_fingerprint) as dataset:
                cached = load_preview(workdir, artifact.id, **variant)
                if cached is not None:
                    document, png = cached
                    try:
                        descriptor = RasterPreviewDescriptor.model_validate(document)
                    except ValidationError:
                        pass
                    else:
                        if (
                            descriptor.image_url == image_url and max(descriptor.width, descriptor.height) <= max_size
                            and png[12:16] == b"IHDR" and int.from_bytes(png[16:20], "big") == descriptor.width
                            and int.from_bytes(png[20:24], "big") == descriptor.height
                        ):
                            return RasterPreview(descriptor, png)
                preview = render_raster_preview(
                    artifact, dataset, palette=palette, max_size=max_size, band=band, image_url=image_url,
                )
            publish_preview(
                workdir, artifact.id, **variant, descriptor=preview.descriptor.model_dump(mode="json"), png=preview.png,
            )
        except ArtifactRasterError as error:
            raise HTTPException(status_code=error.status_code, detail={
                "code": error.code, "message": str(error), "field": error.field,
            }) from None
        except RasterPreviewError as error:
            raise HTTPException(status_code=422, detail={
                "code": "RASTER_PREVIEW_UNAVAILABLE", "message": str(error), "field": "preview",
            }) from None
        except (AdapterError, OSError, ValueError, RuntimeError):
            raise HTTPException(status_code=500, detail={
                "code": "STORAGE_FAILED", "message": "Could not read or save the raster preview.", "field": "preview",
            }) from None
        return preview

    @router.get("/artifacts/{artifact_id}/file")
    def read_file(artifact_id: str) -> FileResponse:
        artifact, _, path = locate_artifact(artifact_id)
        return FileResponse(path, filename=f"{path.stem}-{artifact.run_id}{path.suffix}")

    @router.get("/artifacts/{artifact_id}/preview")
    def read_preview(artifact_id: str) -> Response:
        artifact, _, path = locate_artifact(artifact_id)
        if artifact.kind != "vector":
            raise HTTPException(status_code=422, detail={
                "code": "PREVIEW_UNAVAILABLE", "message": "This artifact does not have a vector preview.",
            })
        return vector_preview(path, artifact.layer)

    @router.get("/artifacts/{artifact_id}/raster-preview")
    def read_raster_preview(
        artifact_id: str, response: Response, palette: Palette = "terrain",
        max_size: _PreviewSize = MAX_PREVIEW_SIZE, band: _PreviewBand = 1,
    ) -> RasterPreviewDescriptor:
        response.headers["Cache-Control"] = "no-store"
        return raster_display(artifact_id, palette=palette, max_size=max_size, band=band).descriptor

    @router.get(
        "/artifacts/{artifact_id}/raster-preview.png", response_class=Response,
        responses={200: {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}},
    )
    def read_raster_preview_image(
        artifact_id: str, palette: Palette = "terrain", max_size: _PreviewSize = MAX_PREVIEW_SIZE,
        band: _PreviewBand = 1, source_fingerprint: _PreviewFingerprint = None,
    ) -> Response:
        preview = raster_display(
            artifact_id, palette=palette, max_size=max_size, band=band, source_fingerprint=source_fingerprint,
        )
        return Response(content=preview.png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.get("/artifacts/{artifact_id}/value")
    def read_raster_value(
        artifact_id: str, response: Response, longitude: _Longitude, latitude: _Latitude,
        band: _PreviewBand = 1, source_fingerprint: _PreviewFingerprint = None,
    ) -> RasterValue:
        artifact, _, path = locate_artifact(artifact_id)
        try:
            with open_artifact_raster(artifact, path, source_fingerprint=source_fingerprint) as dataset:
                result = query_raster_value(artifact, dataset, longitude=longitude, latitude=latitude, band=band)
        except ArtifactRasterError as error:
            raise HTTPException(status_code=error.status_code, detail={
                "code": error.code, "message": str(error), "field": error.field,
            }) from None
        except RasterValueError as error:
            raise HTTPException(status_code=422, detail={
                "code": "RASTER_VALUE_UNAVAILABLE", "message": str(error), "field": error.field,
            }) from None
        response.headers["Cache-Control"] = "no-store"
        return result

    @router.get("/artifacts/{artifact_id}/table")
    def read_table(artifact_id: str, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> ArtifactTable:
        artifact, _, path = locate_artifact(artifact_id)
        if artifact.kind != "table" or artifact.format.upper() != "CSV":
            raise HTTPException(status_code=422, detail={
                "code": "TABLE_PREVIEW_UNAVAILABLE", "message": "This artifact does not have a CSV table preview.",
            })
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv_output.reader(stream, strict=True)
                columns = next(reader, [])
                if not columns:
                    raise ValueError("The CSV values do not match their header.")
                remaining_bytes = _MAX_TABLE_PREVIEW_CELL_BYTES - sum(len(cell.encode("utf-8")) for cell in columns)
                if remaining_bytes < 0:
                    raise ValueError("The CSV header is too large to preview.")
                rows = []
                for row in reader:
                    if not row:
                        continue
                    if len(row) != len(columns):
                        raise ValueError("The CSV values do not match their header.")
                    remaining_bytes -= sum(len(cell.encode("utf-8")) for cell in row)
                    if len(rows) == limit or remaining_bytes < 0:
                        return ArtifactTable(columns=columns, rows=rows, truncated=True)
                    rows.append(row)
        except (OSError, csv_output.Error, ValueError) as error:
            raise HTTPException(status_code=422, detail={
                "code": "TABLE_PREVIEW_FAILED", "message": str(error),
            }) from error
        return ArtifactTable(columns=columns, rows=rows, truncated=False)

    return router
