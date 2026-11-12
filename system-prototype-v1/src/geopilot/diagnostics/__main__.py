"""Inspect a local GeoPilot installation before launching the workbench."""

import argparse
import json
import os
import platform
from dataclasses import asdict
from pathlib import Path

from geopilot.diagnostics.doctor import ROOT, diagnose


def port_number(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a versioned JSON report")
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--qgis-process")
    parser.add_argument("--port", type=port_number, default=8000)
    parser.add_argument("--frontend-port", type=port_number, default=5173)
    args = parser.parse_args()
    root = args.project_root.resolve()
    data = Path(os.environ.get("GEOPILOT_DATA_DIR") or root / "local-data")
    checks = diagnose(
        root, args.config or root / "config", args.database or data / "project.sqlite",
        args.workspace or data / "workspace", args.qgis_process, args.port, args.frontend_port,
    )
    status = next((status for status in ("FAIL", "WARN") if any(check.status == status for check in checks)), "PASS")
    report = {
        "schema_version": 1, "status": status, "platform": platform.platform(),
        "checks": [asdict(check) for check in checks],
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            print(f"[{check.status}] {check.name} — {check.message}")
    return int(status == "FAIL")


if __name__ == "__main__":
    raise SystemExit(main())
