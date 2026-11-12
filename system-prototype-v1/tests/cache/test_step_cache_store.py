import os
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier, Event

import pytest

from geopilot.artifacts import step_cache
from geopilot.artifacts.step_cache import CachedOutput, StepCache


KEY = sha256(b"request").hexdigest()
OTHER_KEY = sha256(b"other request").hexdigest()
STEP = "calculate_area_with_equal_area_projection"
PAYLOAD = b'{\n  "total_area_m2": 25.0\n}\n'


def _expected(run_id, payload=PAYLOAD):
    return CachedOutput(run_id, STEP, payload, sha256(payload).hexdigest())


def test_round_trip_preserves_original_json_bytes_and_source(tmp_path):
    root = tmp_path / "cache"
    cache = StepCache(root)

    assert cache.load(KEY) is None
    assert not root.exists()
    cache.save(KEY, "source-run", STEP, PAYLOAD)

    assert StepCache(root).load(KEY) == _expected("source-run")
    assert cache.load(OTHER_KEY) is None
    assert len(list(root.iterdir())) == 1


def test_corrupt_content_and_wrong_key_are_misses_and_regular_entry_is_repaired(tmp_path):
    cache = StepCache(tmp_path)
    cache.save(KEY, "source-run", STEP, PAYLOAD)
    entry = tmp_path / f"{KEY}.cache"
    record = entry.read_bytes()

    (tmp_path / f"{OTHER_KEY}.cache").write_bytes(record)
    assert cache.load(OTHER_KEY) is None
    for damaged in (b"incomplete", record[:-1], record.replace(b"source-run", b"forged-run")):
        entry.write_bytes(damaged)
        assert cache.load(KEY) is None

    cache.save(KEY, "recomputed-run", STEP, PAYLOAD)
    assert cache.load(KEY) == _expected("recomputed-run")


@pytest.mark.parametrize("key", ["../outside", "A" * 64, "a" * 63])
def test_invalid_key_cannot_create_or_read_a_record(tmp_path, key):
    root = tmp_path / "cache"
    cache = StepCache(root)

    assert cache.load(key) is None
    with pytest.raises(ValueError, match="SHA-256"):
        cache.save(key, "source-run", STEP, PAYLOAD)
    assert not root.exists()


def test_payload_boundary_and_invalid_source(tmp_path):
    cache = StepCache(tmp_path)
    limit = 64 * 1024
    payload = b'{"text":"' + b"x" * (limit - 11) + b'"}'
    assert len(payload) == limit
    cache.save(KEY, "source-run", STEP, payload)
    assert cache.load(KEY) == _expected("source-run", payload)

    with pytest.raises(ValueError, match="64 KiB"):
        cache.save(KEY, "source-run", STEP, payload + b" ")
    with pytest.raises(ValueError, match="nonempty"):
        cache.save(KEY, "source-run", STEP, b"")
    with pytest.raises(ValueError, match="source_run_id"):
        cache.save(KEY, "other/run", STEP, payload)
    with pytest.raises(ValueError, match="source_step_key"):
        cache.save(KEY, "source-run", "", payload)
    assert cache.load(KEY) == _expected("source-run", payload)


def test_oversized_or_nonregular_entry_is_a_miss(tmp_path):
    cache = StepCache(tmp_path)
    entry = tmp_path / f"{KEY}.cache"
    with entry.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024)
    assert cache.load(KEY) is None

    entry.unlink()
    os.mkfifo(entry)
    assert cache.load(KEY) is None
    with pytest.raises(FileExistsError):
        cache.save(KEY, "source-run", STEP, PAYLOAD)
    assert entry.exists()


def test_root_ancestor_and_entry_symlinks_are_never_followed(tmp_path):
    outside_parent = tmp_path / "outside"
    outside_parent.mkdir()
    outside_root = outside_parent / "cache"
    outside = StepCache(outside_root)
    outside.save(KEY, "outside-run", STEP, PAYLOAD)
    outside_entry = outside_root / f"{KEY}.cache"
    original = outside_entry.read_bytes()

    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(outside_parent, target_is_directory=True)
    root_link = tmp_path / "root-link"
    root_link.symlink_to(outside_root, target_is_directory=True)
    real_root = tmp_path / "real"
    real_root.mkdir()
    entry_link = real_root / f"{KEY}.cache"
    entry_link.symlink_to(outside_entry)

    for root in (parent_link / "cache", root_link, real_root):
        cache = StepCache(root)
        assert cache.load(KEY) is None
        with pytest.raises(OSError):
            cache.save(KEY, "replacement", STEP, PAYLOAD)
    assert entry_link.is_symlink()
    assert outside_entry.read_bytes() == original


def test_concurrent_writers_publish_only_complete_records(tmp_path, monkeypatch):
    cache = StepCache(tmp_path)
    cache.save(KEY, "original", STEP, PAYLOAD)
    ready = Barrier(3)
    release = Event()
    replace = os.replace
    replacements = {
        "writer-one": b'{"total_area_m2":100}\n',
        "writer-two": b'{"total_area_m2":200}\n',
    }

    def wait_to_publish(source, target, **kwargs):
        if target == f"{KEY}.cache":
            ready.wait(timeout=5)
            assert release.wait(timeout=5)
        return replace(source, target, **kwargs)

    monkeypatch.setattr(step_cache.os, "replace", wait_to_publish)
    with ThreadPoolExecutor(max_workers=2) as workers:
        saves = [workers.submit(cache.save, KEY, name, STEP, payload) for name, payload in replacements.items()]
        try:
            ready.wait(timeout=5)
            assert cache.load(KEY) == _expected("original")
        finally:
            release.set()
        for saved in saves:
            saved.result(timeout=5)

    assert cache.load(KEY) in [_expected(name, payload) for name, payload in replacements.items()]
    assert [path.name for path in tmp_path.iterdir()] == [f"{KEY}.cache"]


def test_publication_failure_cleans_only_the_staged_record(tmp_path, monkeypatch):
    cache = StepCache(tmp_path)
    cache.save(KEY, "original", STEP, PAYLOAD)

    def cannot_publish(*args, **kwargs):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(step_cache.os, "replace", cannot_publish)
    with pytest.raises(OSError, match="simulated publication failure"):
        cache.save(KEY, "replacement", STEP, PAYLOAD)

    assert cache.load(KEY) == _expected("original")
    assert [path.name for path in tmp_path.iterdir()] == [f"{KEY}.cache"]
