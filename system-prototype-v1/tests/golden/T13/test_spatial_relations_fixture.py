import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pyogrio
import pytest
import shapely


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/vector/t13"
LAYERS = ("points", "lines", "polygons")


def _document(name):
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def _build(output):
    return subprocess.run(
        [sys.executable, str(FIXTURE / "build_fixture.py"), str(output)],
        capture_output=True, text=True, timeout=30,
    )


def _read_layer(path, layer):
    return pyogrio.read_dataframe(path, layer=layer).set_index("source_feature_id")


@pytest.mark.parametrize("rebuild", [False, True], ids=["shipped", "rebuilt"])
def test_geopackage_reopens_with_exact_geometries_and_known_crs(tmp_path, rebuild):
    source = _document("source.json")
    expected = _document("expected.json")
    package = FIXTURE / expected["input_file"]
    if rebuild:
        package = tmp_path / package.name
        built = _build(package)
        assert built.returncode == 0, built.stderr

    assert dict(pyogrio.list_layers(package)) == expected["geometry_types"]
    with closing(sqlite3.connect(f"{package.as_uri()}?mode=ro", uri=True)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        layers = connection.execute("SELECT table_name, srs_id FROM gpkg_geometry_columns").fetchall()
    assert dict(layers) == dict.fromkeys(expected["geometry_types"], 32650)

    for layer, records in source["layers"].items():
        frame = _read_layer(package, layer)
        assert frame.crs.to_string() == expected["crs"] == source["crs"]
        assert frame.index.is_unique
        assert sorted(frame.index) == expected["input_feature_ids"][layer]
        assert frame.geom_type.eq(expected["geometry_types"][layer]).all()
        assert frame.geometry.is_valid.all() and not frame.geometry.is_empty.any()
        for record in records:
            actual = frame.geometry.loc[record["source_feature_id"]]
            assert actual.equals_exact(shapely.from_wkt(record["wkt"]), tolerance=0)

    reference = expected["reference"]
    area = _read_layer(package, reference["layer"]).geometry.loc[reference["source_feature_id"]]
    assert area.exterior.bounds == tuple(reference["shell_bounds_xy"])
    assert [ring.bounds for ring in area.interiors] == [tuple(reference["hole_bounds_xy"])]


def test_constructor_protects_an_existing_file(tmp_path):
    output = tmp_path / "spatial-relations.gpkg"
    output.write_bytes(b"keep existing bytes")

    result = _build(output)

    assert result.returncode == 2 and "already exists" in result.stderr
    assert output.read_bytes() == b"keep existing bytes"


@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("predicate", ["intersects", "within", "touches"])
def test_real_predicates_select_the_manually_derived_ids(layer, predicate):
    expected = _document("expected.json")
    package = FIXTURE / expected["input_file"]
    reference = expected["reference"]
    area = _read_layer(package, reference["layer"]).geometry.loc[reference["source_feature_id"]]
    targets = _read_layer(package, layer)

    actual = getattr(shapely, predicate)(targets.geometry.array, area)

    expected_ids = expected["boundary_only_ids"] if predicate == "touches" else expected["relations"][predicate]
    assert sorted(targets.index[actual]) == expected_ids[layer]
