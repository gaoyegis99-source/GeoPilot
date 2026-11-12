"""Reopen the workbench downloads, GIS logs and original data independently."""

import argparse
import csv
import hashlib
import json
from math import radians
from pathlib import Path

import pyogrio
import shapely


WGS84_SEMI_MAJOR_M = 6_378_137.0
EQUATORIAL_ORIGIN = 3.0


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def features(path, layer=None):
    return pyogrio.read_dataframe(path, layer=layer).set_index("source_feature_id").sort_index()


def checked_case(directory, expectation, browser):
    source = directory / expectation["file"]
    assert digest(source) == browser["original_sha256"]
    run = browser["origin"]
    workspace = directory / "workspace/tasks" / run["task_id"] / "runs" / run["run_id"]
    downloads = browser["downloads"]
    for download in downloads.values():
        assert digest(directory / download["file"]) == download["sha256"]
    selected = features(directory / downloads["selected_targets"]["file"])
    assert list(selected.index) == sorted(expectation["selected_ids"])
    originals = features(source, "targets")
    assert selected.crs == originals.crs
    assert all(selected.geometry[key].equals_exact(originals.geometry[key], 1e-11) for key in selected.index)
    extent = features(source, "analysis_extent")
    assert bool(shapely.covers(extent.geometry.union_all(), selected.geometry.to_numpy()).all())

    lookup = load(workspace / "steps/select_roads_from_expanded_extent/result.json")["outputs"]["roads"]
    candidates = features(workspace / lookup["path"], lookup["layer"])
    assert list(candidates.index) == expectation["candidate_ids"]
    original_roads = features(source, "roads")
    assert candidates.crs == original_roads.crs
    assert all(candidates.geometry[key].equals(original_roads.geometry[key]) for key in candidates.index)

    request = load(workspace / "steps/create_buffer/request.json")
    result = load(workspace / "steps/create_buffer/result.json")
    log = (workspace / result["log_path"]).read_text(encoding="utf-8")
    assert result["exit_code"] == 0
    assert request["parameters"]["distance"] == 500
    assert request["run_id"] == run["run_id"]
    buffer = pyogrio.read_dataframe(directory / downloads["buffer_extent"]["file"])
    assert len(buffer) and buffer.geometry.is_valid.all() and not buffer.geometry.is_empty.any()
    evidence = {"run_id": run["run_id"], "crs": buffer.crs.to_string(), "selected_ids": list(selected.index),
                "candidate_ids": list(candidates.index), "buffer_tool": request["tool"], "distance_m": 500}
    if expectation["mode"] == "geodesic":
        assert buffer.crs.to_epsg() == 4326 and request["tool"] == {"id": "python-wgs84-geodesic-buffer", "version": 1}
        assert "wgs84_geodesic_buffer.py" in log and '"method": "geodesic"' in log
        cap_distance = WGS84_SEMI_MAJOR_M * radians(EQUATORIAL_ORIGIN - buffer.total_bounds[0])
        assert abs(cap_distance - 500) <= 0.05, cap_distance
        evidence["independent_equatorial_cap_m"] = cap_distance
    else:
        assert buffer.crs.to_epsg() == 32650 and request["tool"] == {"id": "qgis-buffer", "version": 1}
        assert "native:buffer" in log and '"method": "local_projected"' in log

    csv_path = directory / downloads["zone_statistics"]["file"]
    with csv_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    counts = {row["zone_id"] or "unassigned": int(row["count"]) for row in rows}
    counts["total"] = sum(counts.values())
    assert counts == expectation["counts"]
    assert sorted(identifier for row in rows for identifier in json.loads(row["source_feature_ids"])) == list(selected.index)
    evidence["counts"] = counts
    return evidence, buffer.geometry.union_all(), csv_path.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory
    expected = load(directory / "expected.json")
    record = load(directory / "browser.json")
    browser = record["results"]
    results = {item["name"]: checked_case(directory, item, browser[item["name"]]) for item in expected}
    _, prior_geometry, prior_csv = results["local-projected-v2"]
    _, current_geometry, current_csv = results["local-projected-v3"]
    difference = float(prior_geometry.hausdorff_distance(current_geometry))
    assert difference <= 1e-6 and prior_csv == current_csv
    evidence = {
        "cases": {name: result[0] for name, result in results.items()},
        "projected_v2_v3_hausdorff_m": difference,
        "projected_v2_v3_same_csv_bytes": True,
    }
    if replay := record.get("legacy_replay"):
        legacy = next(item for item in expected if item["name"] == "local-projected-v2")
        replay_evidence, replay_geometry, replay_csv = checked_case(directory, legacy, replay)
        assert replay_geometry.hausdorff_distance(prior_geometry) <= 1e-6 and replay_csv == prior_csv
        evidence["confirmed_v2_replay"] = {**replay_evidence, "original_run_id": replay["original_run_id"]}
    (directory / "download-verification.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    total = len(results) + int(bool(record.get("legacy_replay")))
    print(f"PASS: {total} download sets and T04 equatorial cap reopened; projected v2/v3 Hausdorff {difference:g} m")


if __name__ == "__main__":
    main()
