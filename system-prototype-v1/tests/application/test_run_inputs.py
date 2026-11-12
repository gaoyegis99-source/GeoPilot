from pathlib import Path
from shutil import copyfile

import pyogrio
import pytest

from geopilot.application.run_inputs import stage_run_inputs
from geopilot.data.assets import InputChangedError, register_asset
from geopilot.data.fingerprint import fingerprint_file
from geopilot.data.geopackage_reader import read_geopackage_metadata
from geopilot.data.vector_reader import read_vector_metadata
from geopilot.persistence.schema import initialize_database
from geopilot.persistence.workspace import create_run_workspace


SOURCE = Path(__file__).resolve().parents[1] / "fixtures/vector/road-buffer.gpkg"


def test_stage_exact_asset_roles_and_shapefile_companion_group(tmp_path):
    database = tmp_path / "assets.sqlite"
    initialize_database(database)
    points = pyogrio.read_dataframe(SOURCE, layer="targets").rename(columns={"source_feature_id": "point_id"})
    shapefile = tmp_path / "points source.shp"
    pyogrio.write_dataframe(points, shapefile, driver="ESRI Shapefile")
    registered = {
        "roads": register_asset(database, SOURCE, layer="roads"),
        "zones": register_asset(database, SOURCE, layer="zones"),
        "targets": register_asset(database, shapefile),
    }
    workspace = create_run_workspace(tmp_path / "workspace", "task-001", "run-001")

    copied = stage_run_inputs(database, {role: asset.ref for role, asset in registered.items()}, workspace.path)

    assert list(copied) == list(registered)
    assert copied["roads"].path == copied["zones"].path
    assert (copied["roads"].layer, copied["zones"].layer) == ("roads", "zones")
    assert len({ref.path for ref in copied.values()}) == 2
    for role, asset in registered.items():
        ref = copied[role]
        target = workspace.path / ref.path
        assert fingerprint_file(target).digest == asset.fingerprint
        assert fingerprint_file(asset.source.path).digest == asset.fingerprint
        if target.suffix == ".gpkg":
            assert read_geopackage_metadata(target, ref.layer)[1] == asset.metadata
        else:
            assert target.with_suffix(".prj").is_file() and target.with_suffix(".dbf").is_file()
            assert read_vector_metadata(target) == asset.metadata


def test_changed_source_does_not_stage_any_role(tmp_path):
    database = tmp_path / "assets.sqlite"
    initialize_database(database)
    path = tmp_path / "roads.gpkg"
    copyfile(SOURCE, path)
    roads = register_asset(database, path, layer="roads")
    zones = register_asset(database, SOURCE, layer="zones")
    additional = pyogrio.read_dataframe(path, layer="roads", max_features=1)
    pyogrio.write_dataframe(additional, path, layer="roads", append=True)
    workspace = create_run_workspace(tmp_path / "workspace", "task-001", "run-002")

    with pytest.raises(InputChangedError):
        stage_run_inputs(database, {"zones": zones.ref, "roads": roads.ref}, workspace.path)

    assert not list((workspace.path / "inputs").iterdir())
