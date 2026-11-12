import csv
import hashlib
import json
import platform
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, "/project/scripts/bench")
from run_vector import run_batch, workloads
from geopilot.api.app import create_app
from geopilot.artifacts.service import list_artifacts
from geopilot.data.fingerprint import fingerprint_file
from geopilot.persistence.schema import open_database
from starlette.testclient import TestClient

INPUT = Path("/bench-input")
SCRATCH = Path("/scratch")
INPUT_SHA = "7211b7e75c9560acdab8198c555d62af732f1de3e9fe15317f9ab55706d21775"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


package = INPUT / "road.gpkg"
if digest(package) != INPUT_SHA:
    raise SystemExit("Unexpected original road input")
print("python", sys.version.split()[0], "system", platform.platform(), "csv_limit", csv.field_size_limit(), flush=True)
with TemporaryDirectory(prefix="same-input-", dir=SCRATCH) as directory:
    workspace = Path(directory)
    raw = SCRATCH / "samples.jsonl"
    run_batch(workspace, INPUT, workloads()["road"], "L", "warm", [("probe", 1)],
              Path("/usr/bin/qgis_process"), 600, None, raw)
    row = json.loads(raw.read_text(encoding="utf-8"))
    if row["status"] != "PASS":
        raise SystemExit(row.get("error", "Fixed road replay failed"))
    database = workspace / "metadata.sqlite3"
    with open_database(database, read_only=True) as connection:
        table = next(artifact for artifact in list_artifacts(connection, row["run_id"]) if artifact.role == "zone_statistics")
    workdir = Path(row["workdir"])
    table_path = workdir / table.uri
    with table_path.open(encoding="utf-8", newline="") as stream:
        rows = csv.DictReader(stream, strict=True)
        unassigned, = (entry for entry in rows if entry["assignment_status"] == "unassigned")
        assert len(unassigned["source_feature_ids"]) == 573_440
        assert len(json.loads(unassigned["source_feature_ids"])) == int(unassigned["count"]) == 16_384
    url = f"/api/artifacts/{table.id}"
    with TestClient(create_app(database)) as client:
        preview = client.get(f"{url}/table")
        preview.raise_for_status()
        content = preview.json()
        assert len(content["rows"]) == 100 and content["truncated"] is True
        download = client.get(f"{url}/file")
        download.raise_for_status()
        assert hashlib.sha256(download.content).hexdigest() == digest(table_path)
        assert table.fingerprint == fingerprint_file(table_path).digest
    assert digest(package) == INPUT_SHA
    shutil.copyfile(table_path, SCRATCH / "zone-statistics.csv")
    shutil.copyfile(workdir / "checks/report.json", SCRATCH / "check-report.json")
    proof = {
        "run_id": row["run_id"], "states": row["run_status"],
        "file_sha256": digest(table_path), "artifact_fingerprint": table.fingerprint,
        "input_sha256_before_and_after": INPUT_SHA,
        "csv_rows": table.metadata["row_count"], "unassigned_id_count": 16_384, "unassigned_field_characters": 573_440,
        "preview_http": preview.status_code, "preview_rows": len(content["rows"]),
        "download_http": download.status_code, "download_bytes": len(download.content),
        "check_rules": row["checks"]["rules"], "independent": row["correctness"],
    }
    (SCRATCH / "replay-result.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(proof), flush=True)
