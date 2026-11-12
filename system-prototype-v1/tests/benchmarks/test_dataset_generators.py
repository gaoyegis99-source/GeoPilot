"""Exercise the public command and evidence contract of the three generators."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
FAMILIES = ("vector", "raster", "remote_sensing")


def run_generator(family, output, tier="S"):
    return subprocess.run(
        [
            sys.executable, str(ROOT / "scripts/bench" / f"generate_{family}.py"),
            "--tier", tier, "--output", str(output),
        ],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("family", FAMILIES)
def test_generator_emits_verifiable_dataset_manifest(family, tmp_path):
    output = tmp_path / family
    result = run_generator(family, output)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert (manifest["version"], manifest["family"], manifest["tier"]) == (1, family, "S")
    assert isinstance(manifest["reference"], dict) and manifest["reference"]
    assert manifest["files"]

    listed = set()
    for record in manifest["files"]:
        relative = Path(record["path"])
        path = (output / relative).resolve()
        assert not relative.is_absolute() and path.is_relative_to(output)
        assert relative != Path("manifest.json") and relative not in listed
        listed.add(relative)
        assert path.is_file() and record["bytes"] == path.stat().st_size > 0
        with path.open("rb") as stream:
            assert record["sha256"] == hashlib.file_digest(stream, "sha256").hexdigest()


@pytest.mark.parametrize("family", FAMILIES)
def test_generator_refuses_existing_output_directory(family, tmp_path):
    output = tmp_path / family
    output.mkdir()
    sentinel = output / "preserve.txt"
    sentinel.write_bytes(b"existing data")
    result = run_generator(family, output)
    assert result.returncode != 0
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"existing data"
