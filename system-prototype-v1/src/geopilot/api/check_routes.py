"""Read the current check report and report history for a run."""

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from geopilot.checks.recheck_service import RecheckError, request_recheck
from geopilot.checks.report_service import CheckReport, get_current_report, get_report, list_report_ids
from geopilot.persistence.run_repository import get_run
from geopilot.persistence.schema import open_database
from geopilot.runs.models import CheckStatus


class RunChecks(BaseModel):
    run_id: str
    check_status: CheckStatus
    current_report: CheckReport | None
    report_ids: tuple[str, ...]
    selected_report: CheckReport | None = None


class RecheckAccepted(BaseModel):
    report_id: str


def check_routes(database_path: str | Path, rule_versions: Mapping[str, int]) -> APIRouter:
    router = APIRouter(prefix="/api/runs")

    @router.get("/{run_id}/checks", response_model_exclude_unset=True)
    def read_checks(run_id: str, report_id: str | None = None) -> RunChecks:
        try:
            with open_database(database_path, read_only=True) as connection, connection:
                connection.execute("BEGIN")
                run = get_run(connection, run_id)
                if run is None:
                    raise LookupError(f"Run {run_id!r} does not exist.")
                current = get_current_report(connection, run_id)
                ids = list_report_ids(connection, run_id)
                selection = {}
                if report_id is not None:
                    selected = get_report(connection, run_id, report_id)
                    if selected is None:
                        raise HTTPException(status_code=404, detail={
                            "code": "CHECK_REPORT_NOT_FOUND", "message": "The report does not belong to this run.",
                        })
                    selection["selected_report"] = selected
        except LookupError as error:
            raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": str(error)}) from error
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(
                status_code=500, detail={"code": "STORAGE_FAILED", "message": "Could not read the check reports."},
            ) from error
        return RunChecks(
            run_id=run.id, check_status=run.check_status, current_report=current, report_ids=ids, **selection,
        )

    @router.post("/{run_id}/checks", status_code=202)
    def queue_recheck(run_id: str) -> RecheckAccepted:
        try:
            report_id = request_recheck(database_path, run_id, rule_versions=rule_versions)
        except RecheckError as error:
            raise HTTPException(status_code=error.status_code, detail={
                "code": error.code, "message": str(error), "field": error.field,
            }) from error
        except (sqlite3.Error, ValidationError) as error:
            raise HTTPException(status_code=500, detail={
                "code": "STORAGE_FAILED", "message": "Could not queue the check.",
            }) from error
        return RecheckAccepted(report_id=report_id)

    return router
