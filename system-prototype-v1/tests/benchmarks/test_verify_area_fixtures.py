import importlib
import json
import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "benchmarks/raster/area-fixtures"


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/bench"))
    return importlib.import_module("verify_area_fixtures")


def test_dem_m_matches_independent_pixels_and_area(verifier):
    manifest = json.loads((BUNDLE / "manifest.json").read_text())
    entry = next(item for item in manifest["fixtures"] if item["id"] == "dem-m")
    observed = verifier.verify_fixture(BUNDLE, entry)
    assert observed["valid_class_counts"] == {"7": 4183812}
    assert observed["masked_pixels"] == 2304 and observed["maximum_absolute_pixel_error"] == 0
    assert observed["area_m2"]["7"] == pytest.approx(418714664.9057954, rel=2e-7)


def test_portable_fixture_rejects_changed_bytes(verifier, tmp_path):
    manifest = json.loads((BUNDLE / "manifest.json").read_text())
    entry = next(item for item in manifest["fixtures"] if item["id"] == "dem-m")
    copied = tmp_path / entry["path"]
    shutil.copyfile(BUNDLE / entry["path"], copied)
    with copied.open("r+b") as stream:
        first = stream.read(1)
        stream.seek(0)
        stream.write(bytes([first[0] ^ 1]))
    with pytest.raises(ValueError, match="size or SHA-256 differs"):
        verifier.verify_fixture(tmp_path, entry)
