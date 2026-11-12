from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from geopilot.api.app import create_app
from geopilot.domain.tool_contract import ToolContract
from geopilot.methods.loader import load_method_catalog
from geopilot.persistence.schema import initialize_database
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]


def test_asset_catalog_registers_and_lists_exact_local_geopackage_layers(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    source = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
    with TestClient(create_app(database)) as client:
        assert client.get("/api/assets").json() == {"assets": []}
        layers = client.get("/api/assets/layers", params={"path": str(source)}).json()["layers"]
        assert {layer["name"] for layer in layers} == {"analysis_extent", "roads", "targets", "zones"}
        request = {"path": str(source), "layer": "roads"}
        response = client.post("/api/assets", json=request)
        assert response.status_code == 200
        roads = response.json()
        assert roads["asset_version"] == 1 and roads["metadata"]["geometry_type"] == "LineString"
        assert roads["source"]["layer"] == "roads" and roads["metadata"]["crs"] == "EPSG:32650"
        assert client.post("/api/assets", json=request).json() == roads
        targets = client.post("/api/assets", json={**request, "layer": "targets"}).json()
        assert targets["asset_id"] != roads["asset_id"] and targets["metadata"]["geometry_type"] == "Point"
        preview = client.get(f"/api/assets/{targets['asset_id']}/preview", params={"version": 1})
        assert preview.status_code == 200 and preview.headers["content-type"].startswith("application/geo+json")
        point = preview.json()["features"][0]
        assert point["geometry"]["type"] == "Point" and "source_feature_id" in point["properties"]
        longitude, latitude = point["geometry"]["coordinates"]
        assert 110 < longitude < 130 and 30 < latitude < 50
        response = client.post("/api/assets", json={**request, "layer": "unknown"})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "SOURCE_UNREADABLE"
        assert response.json()["detail"]["field"] == "source.layer"
        assert client.get("/api/assets").json() == {"assets": [targets, roads]}


def test_method_catalog_exposes_exact_registered_versions_and_tool_diagnostics(tmp_path):
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    catalog = load_method_catalog(ROOT / "config/methods")
    implementations = {"geopilot.select_spatial_relation", "geopilot.clip_vector_to_extent"}
    registry = ToolRegistry(python_implementations=implementations, qgis_process=tmp_path / "missing-qgis")
    for path in sorted((ROOT / "config/tools").glob("*.yaml")):
        registry.register(ToolContract.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))))
    with TestClient(create_app(database, catalog=catalog, registry=registry)) as client:
        response = client.get("/api/methods")
        assert response.status_code == 200
        methods = {(item["definition"]["id"], item["definition"]["version"]): item for item in response.json()["methods"]}
        assert set(methods) == {(item.id, item.version) for item in catalog.list()}
        ready = methods["spatial-select-and-clip", 1]
        assert ready["available"] and not ready["issues"] and ready["title"] and ready["description"]
        relation = ready["definition"]["critical_options"]["spatial_relation"]
        assert relation["type"] == "enum" and relation["values"] == ["intersects", "within"]
        missing = methods["attribute-filter-export", 1]
        assert not missing["available"]
        assert any(issue["code"] == "TOOL_UNAVAILABLE" for issue in missing["issues"])
