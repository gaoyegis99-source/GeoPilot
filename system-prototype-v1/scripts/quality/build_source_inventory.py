"""Inventory committed GeoPilot files and record worktree changes separately."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DIRECTORIES = ("src", "config", "frontend", "tests", "docs", "artifacts")


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False
    )
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def build_inventory(root: Path, ref: str) -> dict:
    commit = git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    commit = commit.decode("ascii").strip()
    tree = git(root, "ls-tree", "-rz", commit, "--", *DIRECTORIES)
    files = []
    hashes = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, filename = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        path = filename.decode("utf-8", errors="surrogateescape")
        if kind != "blob":
            raise ValueError(f"{path}: expected a Git blob, found {kind}")
        if oid not in hashes:
            content = git(root, "cat-file", "blob", oid)
            hashes[oid] = hashlib.sha256(content).hexdigest()
        files.append({"path": path, "git_mode": mode, "sha256": hashes[oid]})

    counts = dict.fromkeys(DIRECTORIES, 0)
    for file in files:
        counts[file["path"].split("/", 1)[0]] += 1
    if missing := [name for name, count in counts.items() if not count]:
        raise ValueError(f"Required inventory directories are empty or absent: {', '.join(missing)}")

    status = git(root, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=normal")
    changes = [
        {"status": entry[:2], "path": entry[3:]}
        for entry in status.decode("utf-8", errors="surrogateescape").split("\0")
        if entry
    ]
    return {
        "commit": commit,
        "files": sorted(files, key=lambda file: file["path"]),
        "counts": counts,
        "worktree": {
            "head": git(root, "rev-parse", "HEAD").decode("ascii").strip(),
            "branch": git(root, "rev-parse", "--abbrev-ref", "HEAD").decode(
                "utf-8", errors="surrogateescape"
            ).strip(),
            "changes": changes,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="GeoPilot prototype directory")
    parser.add_argument("--ref", default="HEAD", help="Git commit or ref to inventory")
    args = parser.parse_args()
    try:
        print(json.dumps(build_inventory(args.root, args.ref), indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Source inventory failed: {error}\n")


if __name__ == "__main__":
    main()
