from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier

import numpy as np
import pytest
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from geopilot.execution.step_support import AdapterError
from geopilot.preview.store import load_preview, publish_preview


ARTIFACT_ID = "registered-raster"
FINGERPRINT = sha256(b"original artifact").hexdigest()


def _render(palette="terrain", *, max_size=16, band=1, fingerprint=FINGERPRINT):
    value = 123 if palette == "terrain" else 86
    with MemoryFile() as memory:
        with memory.open(
            driver="PNG", width=1, height=1, count=4, dtype="uint8",
            transform=from_origin(0, 1, 1, 1),
        ) as dataset:
            dataset.write(np.array([value, value, value, 255], dtype="uint8").reshape(4, 1, 1))
        image = memory.read()
    return {
        "artifact_id": ARTIFACT_ID, "source_fingerprint": fingerprint,
        "palette": palette, "band": band, "width": 1, "height": 1,
        "image_url": f"/api/artifacts/{ARTIFACT_ID}/raster-preview.png?max_size={max_size}",
    }, image


def _get(workdir, render, *, palette="terrain", max_size=16, band=1, fingerprint=FINGERPRINT, renderer_version=1):
    options = dict(
        source_fingerprint=fingerprint, palette=palette, max_size=max_size, band=band, renderer_version=renderer_version,
    )
    existing = load_preview(workdir, ARTIFACT_ID, **options)
    if existing is not None:
        return existing
    descriptor, png = render()
    publish_preview(workdir, ARTIFACT_ID, **options, descriptor=descriptor, png=png)
    return descriptor, png


def _unexpected_render():
    pytest.fail("A complete preview should be reused without rendering again")


def test_persists_independent_png_and_metadata_for_each_preview_variant(tmp_path):
    artifact = tmp_path / "analysis.tif"
    original = b"original artifact"
    artifact.write_bytes(original)
    rendered = _render()
    assert _get(tmp_path, lambda: rendered) == rendered
    assert _get(tmp_path, _unexpected_render) == rendered

    gray = _render("grayscale")
    assert _get(tmp_path, lambda: gray, palette="grayscale") == gray
    small = _render(max_size=8)
    assert _get(tmp_path, lambda: small, max_size=8) == small
    second_band = _render(band=2)
    assert _get(tmp_path, lambda: second_band, band=2) == second_band
    next_fingerprint = sha256(b"next version").hexdigest()
    next_source = _render(fingerprint=next_fingerprint)
    assert _get(tmp_path, lambda: next_source, fingerprint=next_fingerprint) == next_source
    assert _get(tmp_path, lambda: rendered, renderer_version=2) == rendered
    records = list((tmp_path / "previews" / ARTIFACT_ID).glob("*/preview.json"))
    assert len(records) == 6
    assert len(list((tmp_path / "previews" / ARTIFACT_ID).glob("*/*.png"))) == 6
    assert artifact.read_bytes() == original


def test_repairs_an_incomplete_or_corrupt_preview_record(tmp_path):
    rendered = _render()
    assert _get(tmp_path, lambda: rendered) == rendered
    record, = (tmp_path / "previews" / ARTIFACT_ID).glob("*/preview.json")
    image, = record.parent.glob("*.png")
    image.write_bytes(b"incomplete")
    assert _get(tmp_path, lambda: rendered) == rendered
    assert _get(tmp_path, _unexpected_render) == rendered
    record.write_text("{", encoding="utf-8")
    assert _get(tmp_path, lambda: rendered) == rendered
    assert _get(tmp_path, _unexpected_render) == rendered
    assert image.read_bytes() == rendered[1]


def test_parallel_publication_leaves_one_complete_preview(tmp_path):
    rendered = _render()
    ready = Barrier(2)

    def concurrent_render():
        ready.wait(timeout=5)
        return rendered

    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(_get, tmp_path, concurrent_render) for _ in range(2)]
        assert [request.result(timeout=10) for request in requests] == [rendered, rendered]
    assert _get(tmp_path, _unexpected_render) == rendered
    record, = (tmp_path / "previews" / ARTIFACT_ID).glob("*/preview.json")
    assert sorted(path.suffix for path in record.parent.iterdir()) == [".json", ".png"]


def test_preview_does_not_follow_a_symlink_outside_the_run(tmp_path):
    run = tmp_path / "run"
    other = tmp_path / "elsewhere"
    run.mkdir()
    other.mkdir()
    (run / "previews").symlink_to(other, target_is_directory=True)
    with pytest.raises(AdapterError):
        _get(run, _unexpected_render)
    assert list(other.iterdir()) == []
