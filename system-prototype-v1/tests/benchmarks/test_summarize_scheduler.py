import json
import shutil
import tarfile
from hashlib import sha256
from io import BytesIO

import pytest

from scripts.bench import summarize_scheduler as scheduler


@pytest.fixture
def evidence(tmp_path):
    for name in (scheduler.NORMAL, scheduler.NATIVE, scheduler.JUNIT, scheduler.CONSOLE, scheduler.LIFECYCLE):
        shutil.copyfile(scheduler.EVIDENCE / name, tmp_path / name)
    return tmp_path


def replace_archive(path, change, *, update_seal=True):
    members = scheduler.archive_members(path.read_bytes())

    def checksums():
        return "".join(f"{sha256(raw).hexdigest()}  ./{name}\n" for name, raw in sorted(members.items())).encode()

    manifest = checksums()
    change(members)
    members["SHA256SUMS"] = checksums() if update_seal else manifest
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            entry = tarfile.TarInfo(f"./{name}")
            entry.size = len(content)
            archive.addfile(entry, BytesIO(content))


def assert_error(evidence, capsys, text):
    assert scheduler.main(["--evidence-dir", str(evidence)]) == 1
    stdout, stderr = capsys.readouterr()
    assert not stdout and stderr.startswith("summarize scheduler: ")
    assert text in stderr and "Traceback" not in stderr


def test_default_summarizes_sealed_evidence_without_claiming_parallel_capacity(capsys):
    assert scheduler.main([]) == 0
    stdout, stderr = capsys.readouterr()
    assert not stderr
    summary = json.loads(stdout)
    assert summary["evidence_sha256"][scheduler.LIFECYCLE] == sha256(
        (scheduler.EVIDENCE / scheduler.LIFECYCLE).read_bytes(),
    ).hexdigest()
    assert summary["status_http_totals"] == {"normal": {"200": 724}, "native": {"200": 206}}
    assert summary["native_http"]["sda2/concurrent"]["requests"]["status"]["latency"]["p95_ms"] == 49.93
    assert summary["sqlite_locks"]["windows"]["all_commits"]["read_blocking_us"] == {
        "n": 20, "min_ms": 0.407, "median_ms": 0.6195, "max_ms": 3.824,
    }
    assert summary["pressure"]["junit"] == {"passed": 101, "skipped": 1}
    assert summary["pressure"]["native_cancel_to_worker_exit_seconds"] == 2.172
    assert sum(case.get("container_running_after_local_exit", False) for case in summary["lifecycle"]["cases"].values()) == 3
    assert summary["worker_capacity"]["observed_active_scheduler_slots"] == 1
    assert summary["worker_capacity"]["validated_parallel_worker_count"] is None


def test_missing_native_evidence_is_reported(evidence, capsys):
    (evidence / scheduler.NATIVE).unlink()
    assert_error(evidence, capsys, scheduler.NATIVE)


def test_changed_archive_member_fails_checksum(evidence, capsys):
    replace_archive(evidence / scheduler.NORMAL, lambda members: members.update({"source-revision": b"changed"}),
                    update_seal=False)
    assert_error(evidence, capsys, "checksum differs")


def test_resealed_http_summary_still_has_to_match_raw_status_records(evidence, capsys):
    def change(members):
        member = "agent/standard/summary.json"
        summary = json.loads(members[member])
        summary["requests"]["status"]["codes"]["200"] -= 1
        members[member] = json.dumps(summary).encode()

    replace_archive(evidence / scheduler.NORMAL, change)
    assert_error(evidence, capsys, "raw HTTP observations differ")


def test_resealed_lock_timestamps_still_have_to_match_strace(evidence, capsys):
    def change(members):
        document = json.loads(members["lock-windows.json"])
        row = document["scheduler_write_windows"][0]
        for key in scheduler._LOCK_EVENTS.values():
            row[key] += 1
        members["lock-windows.json"] = json.dumps(document).encode()

    replace_archive(evidence / scheduler.NORMAL, change)
    assert_error(evidence, capsys, "lock events differ from the raw trace")


def test_pressure_console_must_match_junit(evidence, capsys):
    path = evidence / scheduler.CONSOLE
    path.write_text(path.read_text().replace("101 passed", "100 passed"))
    assert_error(evidence, capsys, "Pressure console contradicts JUnit")


def test_contradictory_live_container_evidence_is_rejected(evidence, capsys):
    path = evidence / scheduler.LIFECYCLE
    rows = list(map(json.loads, path.read_text().splitlines()))
    row = next(row for row in rows if row["event"] == "client_stop_observation")
    row["container_running"] = False
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert_error(evidence, capsys, "contradictory external container lifecycle")


@pytest.mark.parametrize("malformed", [b'{"event":', b'[]\n', b'{"event":"one","event":"two"}\n'])
def test_malformed_jsonl_is_reported_without_traceback(evidence, capsys, malformed):
    (evidence / scheduler.LIFECYCLE).write_bytes(malformed)
    assert_error(evidence, capsys, "")
