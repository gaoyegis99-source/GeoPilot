"""Observe status-read errors from a controlled SQLite lock on a copy."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import monotonic

from fastapi import HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.testclient import TestClient

from geopilot.api.app import create_app

source = Path(sys.argv[1])
original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
endpoint = "/api/runs/8c2d171c53894655b4470d61db9b29c8"
with TemporaryDirectory(dir="/dev/shm", prefix="geopilot-p13-status.") as tmp:
    database = Path(tmp) / "project.sqlite"
    shutil.copyfile(source, database)
    app = create_app(database)
    observed = []

    @app.exception_handler(HTTPException)
    async def observe_exception(request, error):
        cause = error.__cause__
        observed.append({"exception": type(cause).__name__, "sqlite_code": getattr(cause, "sqlite_errorcode", None),
                         "sqlite_name": getattr(cause, "sqlite_errorname", None)})
        return await http_exception_handler(request, error)

    with TestClient(app) as client, sqlite3.connect(database) as writer:
        assert client.get(endpoint).json()["execution_status"] == "FAILED"
        writer.execute("BEGIN IMMEDIATE")
        assert client.get(endpoint).status_code == 200
        writer.rollback()
        writer.execute("BEGIN EXCLUSIVE")
        start = monotonic()
        result = client.get(endpoint)
        elapsed = monotonic() - start
        writer.rollback()
        assert result.status_code == 500 and result.json()["detail"]["code"] == "STORAGE_FAILED"
        assert observed == [{"exception": "OperationalError", "sqlite_code": sqlite3.SQLITE_BUSY, "sqlite_name": "SQLITE_BUSY"}]
        assert 4.9 < elapsed
        assert client.get(endpoint).status_code == 200
        print(json.dumps({"source_sha256": original_hash, "begin_immediate_status": 200,
                          "begin_exclusive_status": result.status_code, "detail": result.json()["detail"],
                          "cause": observed[0], "wait_seconds": round(elapsed, 3), "after_release_status": 200}, indent=2))
assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
