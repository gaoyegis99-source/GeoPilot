"""Write or verify the GeoPilot system prototype v1 delivery manifest."""

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = Path("artifacts/reports/final/delivery-manifest.json")

SOURCE_DIRECTORIES = ("src", "config", "frontend/src", "scripts", "tests")
SOURCE_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-test.txt",
    "frontend/index.html",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.json",
    "frontend/vite.config.ts",
)
EVIDENCE = {
    "startup": (
        "README.md",
        "docs/user-guide/workbench.md",
        "docs/acceptance/P11-07.md",
        "artifacts/smoke/p11-07/live-road/summary.json",
        "artifacts/smoke/p11-07/clean-smoke-junit.xml",
        "artifacts/smoke/p11-07/npm-build-final.log",
    ),
    "acceptance": (
        "docs/release/FINAL_ACCEPTANCE.md",
        "docs/acceptance/P11.md",
        "plans/05_ACCEPTANCE/05_FINAL_ACCEPTANCE_CHECKLIST.md",
        "artifacts/reports/final/delivery_manifest.py",
        "docs/traceability/requirements-matrix.md",
        "docs/acceptance/FIXED_CASES.md",
        "docs/acceptance/P11-02.md",
        "artifacts/reports/methods/regression.md",
        "docs/acceptance/interface-audit.md",
        "docs/acceptance/P11-05.md",
        "docs/acceptance/lifecycle.md",
    ),
    "regression": (
        "artifacts/test-results/p11-complete-backend-junit.xml",
        "artifacts/test-results/p11-05-junit.xml",
        "artifacts/test-results/p11-04-frontend-junit.xml",
        "artifacts/test-results/p11-02-fixed-regression-junit.xml",
    ),
    "reproduction": (
        "artifacts/reproduction/p11-05/README.md",
        "artifacts/reproduction/p11-05/verify-workbench.mjs",
        "artifacts/reproduction/p11-05/api.SmO0UpPt/pytest/test_real_workbench_replays_ro0/comparison.json",
        "artifacts/reproduction/p11-05/workbench.KavlOq7U/browser.json",
        "artifacts/reproduction/p11-05/workbench.KavlOq7U/zone-statistics.csv",
        "artifacts/reproduction/p11-05/workbench.KavlOq7U/filtered-features.gpkg",
        "artifacts/smoke/p8-native-workbench-verified/browser.json",
        "artifacts/smoke/p8-10/browser.7TgLX6/topmost-raster.json",
        "artifacts/smoke/p11-04/run.or1ZGlPX/verification.json",
        "artifacts/smoke/p11-06/run.1HiQwqlr/verification.json",
    ),
}


def file_record(path: Path) -> dict:
    with (ROOT / path).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": path.as_posix(), "sha256": digest}


def build_manifest() -> dict:
    paths = {Path(name) for name in SOURCE_FILES}
    for name in SOURCE_DIRECTORIES:
        directory = ROOT / name
        if not directory.is_dir():
            raise FileNotFoundError(directory)
        for item in directory.rglob("*"):
            path = item.relative_to(ROOT)
            if item.is_file() and "__pycache__" not in path.parts and item.suffix not in {".pyc", ".pyo"}:
                paths.add(path)
    source = [file_record(path) for path in sorted(paths)]
    source_index = "".join(f"{item['sha256']}  {item['path']}\n" for item in source)
    return {
        "release": "GeoPilot system prototype v1",
        "acceptance_date": "2026-11-12",
        "source_sha256": hashlib.sha256(source_index.encode("utf-8")).hexdigest(),
        "source_files": source,
        "evidence": {
            group: [file_record(Path(path)) for path in files] for group, files in EVIDENCE.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the current manifest")
    args = parser.parse_args()
    try:
        manifest = build_manifest()
        path = ROOT / MANIFEST
        if args.write:
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif json.loads(path.read_text(encoding="utf-8")) != manifest:
            parser.exit(1, "Delivery verification failed: source or evidence has changed.\n")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Delivery verification failed: {error}\n")
    count = sum(map(len, manifest["evidence"].values()))
    action = "written" if args.write else "verified"
    print(f"Delivery manifest {action}: {len(manifest['source_files'])} source files, {count} evidence files")
    print(f"Source SHA-256: {manifest['source_sha256']}")


if __name__ == "__main__":
    main()
