from copy import deepcopy
import csv
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/quality/summarize_user_trials.py"
RESEARCH = ROOT / "docs/round2/user-research"
SYNTHETIC = RESEARCH / "observations.synthetic.json"
DOWNLOAD_COLUMNS = ["zone_id", "assignment_status", "count", "source_feature_ids"]
DOWNLOAD_ROWS = [
    ["zone-east", "assigned", "2", '["building-east", "building-overlap"]'],
    ["zone-west", "assigned", "3", '["building-buffer-boundary", "building-edge-road", "building-west"]'],
    ["", "unassigned", "1", '["building-zone-boundary"]'],
]


def run_cli(path, *args):
    return subprocess.run([sys.executable, str(CLI), str(path), *map(str, args)],
                          capture_output=True, text=True, check=False)


def read_synthetic():
    return json.loads(SYNTHETIC.read_bytes())


def write_observations(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def write_download(path, rows=DOWNLOAD_ROWS, columns=DOWNLOAD_COLUMNS):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(rows)


def test_synthetic_report_uses_distinct_status_and_attempted_time_denominators():
    result = run_cli(SYNTHETIC)

    assert result.returncode == 0 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report["status"] == "SYNTHETIC" and report["record_kind"] == "synthetic"
    assert report["sessions"] == 2 and report["recommended_minimum"] == 5
    assert report["workflow"] == {
        "completed": 1, "completed_without_assistance": 0, "completion_rate": 0.5,
        "median_elapsed_seconds_completed": 101.5,
    }
    registration = report["tasks"]["register_assets"]
    assert registration["median_elapsed_seconds_attempted"] == 30
    assert registration["completed_without_assistance"] == 2
    assert registration["recovery"]["self_recovered"] == 1
    selection = report["tasks"]["select_method"]
    assert selection["completed"] == 1 and selection["attempted"] == 2
    assert selection["participants_requesting_help"] == 2 and selection["help_requests"] == 3
    assert selection["assisted_participants"] == 2 and selection["participants_with_errors"] == 2
    assert selection["median_elapsed_seconds_attempted"] == 25.0
    assert selection["errors"] == 2 and selection["errors_by_code"] == {"WRONG_METHOD_VERSION": 2}
    assert selection["recovery"] == {"self_recovered": 0, "assisted_recovery": 1, "unresolved": 1}
    run = report["tasks"]["run_plan"]
    assert run["attempted"] == 1 and run["completion_rate"] == 0.5
    assert run["median_elapsed_seconds_attempted"] == 44
    assert any("Fewer than 5" in limit for limit in report["limitations"])
    assert any("excluded from user research" in limit for limit in report["limitations"])
    assert all("U001" not in line and "Synthetic OS" not in line for line in result.stdout.splitlines())


def test_blank_real_observations_are_not_run_without_implied_completion():
    result = run_cli(RESEARCH / "observations.blank.json")

    assert result.returncode == 0 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report["status"] == "NOT_RUN" and report["sessions"] == 0
    assert report["workflow"]["completion_rate"] is None
    assert all(row["completion_rate"] is None for row in report["tasks"].values())
    assert all(row["median_elapsed_seconds_attempted"] is None for row in report["tasks"].values())


def test_unrecorded_browser_os_does_not_change_summary(tmp_path):
    data = read_synthetic()
    data["sessions"][0]["environment"]["os"] = None

    result = run_cli(write_observations(tmp_path / "observations.json", data))

    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == json.loads(run_cli(SYNTHETIC).stdout)


def test_invalid_path_exits_without_a_report(tmp_path):
    result = run_cli(tmp_path / "missing.json")
    assert result.returncode == 1 and result.stdout == ""
    assert "Invalid observation file" in result.stderr


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda data: data["sessions"].append(data["sessions"][0]), "duplicate participant"),
        (lambda data: data["sessions"][0]["tasks"].pop("run_plan"), "missing fields"),
        (lambda data: data["sessions"][0].update({"name": "Unexpected"}), "unexpected fields"),
        (lambda data: data.update({"schema_version": True}), "expected integer 1"),
        (lambda data: data["sessions"][0].update(trial_date="2026-02-30"), "invalid calendar date"),
        (lambda data: data["sessions"][0]["environment"].update(commit="1111"), "full Git SHA-1"),
        (lambda data: data["sessions"][0]["environment"].update(os=17), ".environment.os"),
        (lambda data: data["sessions"][0]["environment"].update(browser=None), ".environment.browser"),
        (lambda data: data["sessions"][0]["environment"].update(qgis=None), ".environment.qgis"),
        (lambda data: data["sessions"][0]["tasks"]["select_method"].update(outcome=[]), ".outcome"),
        (lambda data: data["sessions"][0]["tasks"]["select_method"].update(elapsed_seconds=-1), ".elapsed_seconds"),
        (lambda data: data["sessions"][0]["tasks"]["select_method"].update(elapsed_seconds=float("inf")), "numeric constant"),
        (lambda data: data["sessions"][0]["tasks"]["select_method"].update(help_requests=True), ".help_requests"),
        (lambda data: data["sessions"][0]["tasks"]["select_method"].update(assisted=False), "requires assisted=true"),
        (lambda data: data["sessions"][1]["tasks"]["run_plan"].update(help_requests=1), "cannot contain elapsed time or events"),
        (lambda data: data["sessions"][1]["tasks"]["run_plan"].update(outcome="completed", elapsed_seconds=2), "cannot follow"),
        (lambda data: data["sessions"][0]["tasks"].update(register_assets=data["sessions"][1]["tasks"]["run_plan"]), "after the registration task starts"),
    ],
)
def test_invalid_record_exits_nonzero_without_partial_report(tmp_path, change, error):
    data = read_synthetic()
    change(data)
    path = write_observations(tmp_path / "observations.json", data)

    result = run_cli(path)

    assert result.returncode == 1 and result.stdout == ""
    assert error in result.stderr


def test_duplicate_json_field_exits_without_report(tmp_path):
    path = tmp_path / "observations.json"
    path.write_text(
        '{"schema_version": 1, "schema_version": 1, "record_kind": "observed", "sessions": []}',
        encoding="utf-8",
    )
    result = run_cli(path)

    assert result.returncode == 1 and result.stdout == ""
    assert "Duplicate JSON field: schema_version" in result.stderr


def test_download_evidence_verifies_the_independent_zone_counts(tmp_path):
    write_download(tmp_path / "U001.csv", reversed(DOWNLOAD_ROWS))
    result = run_cli(SYNTHETIC, "--downloads-dir", tmp_path)

    assert result.returncode == 0 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report.pop("download_evidence") == {
        "status": "VERIFIED", "claimed_completed": 1, "verified": 1, "missing": 0, "mismatch": 0,
        "needs_review": [],
    }
    assert report == json.loads(run_cli(SYNTHETIC).stdout)


def test_download_evidence_reports_unverified_claims_without_changing_observations(tmp_path):
    data = read_synthetic()
    for participant in ("U004", "U003", "U005"):
        copy = deepcopy(data["sessions"][0])
        copy["participant_id"] = participant
        data["sessions"].append(copy)
    data["sessions"][-1]["tasks"]["download_result"]["outcome"] = "incomplete"
    observations = write_observations(tmp_path / "observations.json", data)
    write_download(tmp_path / "U001.csv")
    for participant in ("U002", "U004", "U005"):
        (tmp_path / f"{participant}.csv").write_text("wrong download", encoding="utf-8")

    result = run_cli(observations, "--downloads-dir", tmp_path)

    assert result.returncode == 0 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report.pop("download_evidence") == {
        "status": "REVIEW_REQUIRED", "claimed_completed": 3, "verified": 1, "missing": 1, "mismatch": 1,
        "needs_review": [{"participant_id": "U003", "status": "missing"},
                         {"participant_id": "U004", "status": "mismatch"}],
    }
    assert report == json.loads(run_cli(observations).stdout)
    assert report["tasks"]["download_result"]["completed"] == 3


@pytest.mark.parametrize("rows,columns", [
    (DOWNLOAD_ROWS, ["zone_id", "count"]),
    ([DOWNLOAD_ROWS[0], DOWNLOAD_ROWS[0], DOWNLOAD_ROWS[2]], DOWNLOAD_COLUMNS),
    (DOWNLOAD_ROWS[:-1], DOWNLOAD_COLUMNS),
    ([*DOWNLOAD_ROWS, ["zone-north", "assigned", "0", "[]"]], DOWNLOAD_COLUMNS),
    ([DOWNLOAD_ROWS[0], ["zone-west", "assigned", "2", "[]"], DOWNLOAD_ROWS[2]], DOWNLOAD_COLUMNS),
    ([*DOWNLOAD_ROWS[:2], ["", "assigned", "1", "[]"]], DOWNLOAD_COLUMNS),
])
def test_download_evidence_rejects_noncanonical_columns_or_zone_rows(tmp_path, rows, columns):
    write_download(tmp_path / "U001.csv", rows, columns)

    result = run_cli(SYNTHETIC, "--downloads-dir", tmp_path)

    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout)["download_evidence"]["needs_review"] == [
        {"participant_id": "U001", "status": "mismatch"},
    ]


def test_invalid_utf8_download_is_a_reviewable_mismatch(tmp_path):
    (tmp_path / "U001.csv").write_bytes(b"\xff")
    result = run_cli(SYNTHETIC, "--downloads-dir", tmp_path)

    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout)["download_evidence"]["mismatch"] == 1


def test_no_observations_require_no_downloads(tmp_path):
    result = run_cli(RESEARCH / "observations.blank.json", "--downloads-dir", tmp_path)

    assert result.returncode == 0 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report["status"] == "NOT_RUN"
    assert report["download_evidence"] == {
        "status": "NOT_APPLICABLE", "claimed_completed": 0, "verified": 0, "missing": 0, "mismatch": 0,
        "needs_review": [],
    }


@pytest.mark.parametrize("exists", [False, True])
def test_invalid_download_directory_exits_without_a_report(tmp_path, exists):
    directory = tmp_path / "downloads"
    if exists:
        directory.touch()

    result = run_cli(SYNTHETIC, "--downloads-dir", directory)

    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr.startswith("Invalid download evidence:")


def test_invalid_observation_with_download_directory_exits_without_a_report(tmp_path):
    result = run_cli(tmp_path / "missing.json", "--downloads-dir", tmp_path)

    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr.startswith("Invalid observation file:")
