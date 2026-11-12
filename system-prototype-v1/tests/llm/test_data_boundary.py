import json
import socket
import traceback
from pathlib import Path

import pytest
import yaml

from geopilot.application.task_context import InputAssessment, TaskContext
from geopilot.data.assets import register_asset
from geopilot.domain.tool_contract import ToolContract
from geopilot.llm.base import ModelRequest
from geopilot.llm.context_builder import OutboundContext, build_outbound_context
from geopilot.llm.data_boundary import DataBoundaryError, check_outbound_request
from geopilot.methods.loader import MethodCatalog, load_method_catalog
from geopilot.persistence.schema import initialize_database
from geopilot.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
SECRET = "p9-synthetic-private-sample"
UNKNOWN_KEY = "unexpected-private-field"
OPTION_KEYS = {
    "required", "values", "minimum", "maximum", "minimum_exclusive", "maximum_exclusive", "unit", "required_for",
}


@pytest.fixture(scope="module")
def local_assets(tmp_path_factory):
    database = tmp_path_factory.mktemp("outbound-assets") / "catalog.sqlite"
    initialize_database(database)
    vector = register_asset(database, ROOT / "tests/fixtures/vector/road-buffer.gpkg", layer="targets")
    raster = register_asset(database, ROOT / "tests/fixtures/raster/dem-plane.tif")
    return vector, raster


@pytest.fixture
def payload(local_assets):
    vector, raster = local_assets
    method = load_method_catalog(ROOT / "config/methods").get("road-buffer-statistics", 2)
    context = TaskContext(
        task_id="private-task-id", method=method.ref,
        goal={"summary": f"统计 {vector.source.path} 的 building-west，检查栅格分辨率"},
        roles={
            "targets": InputAssessment(asset=vector.ref, status="READY"),
            "dem": InputAssessment(asset=raster.ref, status="READY"),
            "missing": InputAssessment(asset=None, status="UNKNOWN"),
        },
    )
    options = []
    for name, spec in method.critical_options.items():
        fields = spec.model_dump(mode="json", exclude_none=True)
        constraints = {key: value for key, value in fields.items() if key in OPTION_KEYS}
        options.append({"name": name, "type": fields["type"], "required": True, **constraints})
    tools = {step.tool: step.tool.model_dump(mode="json") for step in method.steps}
    return {
        "context": build_outbound_context(context, local_assets, protected_literals=("building-west",)).model_dump(
            mode="json", exclude_none=True,
        ),
        "candidate_methods": [{
            **method.ref.model_dump(mode="json"),
            "inputs": [{"role": role, "type": kind} for role, kind in method.inputs.items()],
            "options": options,
            "steps": [{"key": step.key, "tool": dict(tools[step.tool])} for step in method.steps],
        }],
        "candidate_tools": list(tools.values()),
    }


def _wire(payload):
    return ModelRequest(payload=payload).model_dump_json()


def _rejected(serialized, field, *, protected=(), hidden=(SECRET, UNKNOWN_KEY)):
    with pytest.raises(DataBoundaryError) as caught:
        check_outbound_request(serialized, protected_literals=protected)
    error = caught.value
    assert (error.code, error.field) == ("OUTBOUND_DATA_REJECTED", field)
    shown = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert all(value not in shown for value in hidden)


def test_accepts_entire_real_summary_and_frozen_candidate_request_without_network(payload, local_assets, monkeypatch):
    def no_network(*_args, **_kwargs):
        pytest.fail("Outbound validation attempted a connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    roles = payload["context"]["roles"]
    assert roles[0]["asset"]["feature_count"] == 9
    assert roles[1]["asset"] == {
        "kind": "raster", "width": 7, "height": 7, "band_count": 1, "resolution_x": 10.0, "resolution_y": 10.0,
    }
    assert "asset" not in roles[2]
    serialized = _wire(payload)
    protected = tuple(str(asset.source.path) for asset in local_assets) + ("building-west",)
    assert all(value not in serialized for value in protected)
    assert check_outbound_request(serialized, protected_literals=protected) is None
    assert check_outbound_request(serialized.encode("utf-8"), protected_literals=protected) is None
    _rejected('[]', '$')


def test_accepts_final_bytes_from_the_production_candidate_builder(payload, local_assets):
    from geopilot.llm.prompt_builder import build_model_request

    method = load_method_catalog(ROOT / "config/methods").get("spatial-select-and-clip", 1)
    contracts = []
    for step in method.steps:
        document = (ROOT / "config/tools" / f"{step.tool.id}.yaml").read_text(encoding="utf-8")
        contract = ToolContract.model_validate(yaml.safe_load(document))
        worker = ROOT / "src/geopilot/gis/tools" / (contract.implementation.rsplit(".", 1)[-1] + ".py")
        assert worker.is_file()
        contracts.append(contract)
    registry = ToolRegistry(python_implementations=(contract.implementation for contract in contracts))
    for contract in contracts:
        registry.register(contract)
    context = OutboundContext.model_validate(payload["context"])
    request = build_model_request(context, MethodCatalog([method], ()), registry=registry, registered_rules=("output_exists",))
    entry, = request.payload["candidate_methods"]
    assert entry["id"] == method.id and entry["version"] == method.version
    protected = tuple(str(asset.source.path) for asset in local_assets) + ("building-west",)
    check_outbound_request(request.model_dump_json().encode("utf-8"), protected_literals=protected)

    request.payload["candidate_tools"][0]["implementation"] = SECRET
    _rejected(request.model_dump_json(), "$.payload.candidate_tools[0]")


@pytest.mark.parametrize("goal", [
    "Read /private/work/dem.tif",
    r'Read "C:\Project Files\private.gpkg"',
    r"Read \\local-server\private\point-data",
    "Read C:private_folder",
    "Use staged/data/private_source",
    "Use survey.gpkg",
    "Connect postgres://analyst:example-secret@localhost/catalog",
    "Read host=localhost dbname=survey password=example-secret",
    "Clip POINT Z (500750 4401500 1)",
    "Clip SRID=32650;POLYGON EMPTY",
    "Clip CIRCULARSTRING (0 0, 1 1, 2 0)",
    "Clip near (500750 4401500)",
    "Clip near 500750,4401500",
    "Clip near 117.25 39.9",
    "参考经度：117.0087",
    '{"type":"Feature","geometry":{"type":"Point","coordinates":[117.25,39.9]}}',
    '{"record_id":"row-001","name":"local sample"}',
    'Use row (1,"local sample")',
    "Use [500750,4401500]",
    "Raw row\tprivate value",
    "Read %252Fprivate%252Fcoordinates",
    "Read ／private／coordinates",
    r"Read \u002fprivate\u002fcoordinates",
])
def test_rejects_unsanitized_goal_in_the_serialized_request(payload, goal):
    payload["context"]["goal"] = goal
    _rejected(_wire(payload), "$.payload.context.goal", hidden=(goal,))


@pytest.mark.parametrize(("location", "field"), [
    (("context", "goal"), "$.payload.context.goal"),
    (("context", "roles", 0, "asset", "fields", 0, "name"), "$.payload.context.roles[0].asset.fields[0].name"),
    (("candidate_methods", 0, "id"), "$.payload.candidate_methods[0].id"),
    (("candidate_tools", 0, "id"), "$.payload.candidate_tools[0].id"),
])
def test_known_sample_literals_are_rejected_at_any_allowlisted_string_location(payload, location, field):
    target = payload
    for part in location[:-1]:
        target = target[part]
    target[location[-1]] = SECRET.upper()
    _rejected(_wire(payload), field, protected=(SECRET,), hidden=(SECRET, SECRET.upper()))


def test_unicode_json_escapes_are_decoded_before_checking_nested_values(payload):
    candidate = payload["candidate_methods"][0]
    option_index = next(index for index, option in enumerate(candidate["options"]) if option["type"] == "enum")
    candidate["options"][option_index]["values"] = ["/private/seed.txt"]
    serialized = _wire(payload).replace("/", r"\u002f")
    assert "/private/" not in serialized
    _rejected(serialized, f"$.payload.candidate_methods[0].options[{option_index}].values[0]", hidden=("/private/seed.txt",))


def test_unknown_keys_are_rejected_at_a_known_parent_without_echoing_them(payload):
    _rejected(json.dumps({"payload": payload, UNKNOWN_KEY: SECRET}), "$")
    payload["candidate_methods"][0]["steps"][0]["tool"][UNKNOWN_KEY] = {"source": {"path": SECRET}}
    _rejected(_wire(payload), "$.payload.candidate_methods[0].steps[0].tool")


@pytest.mark.parametrize(("key", "value"), [
    ("geometry", {"type": "Point", "coordinates": [117.25, 39.9]}),
    ("bounds", [1, 2, 3, 4]),
    ("source", {"path": SECRET, "layer": "private"}),
    ("records", [{"source_feature_id": SECRET, "value": 4}]),
])
def test_raw_spatial_and_source_fields_are_not_allowlisted(payload, key, value):
    payload["context"]["roles"][0]["asset"][key] = value
    _rejected(_wire(payload), "$.payload.context.roles[0].asset")


@pytest.mark.parametrize(("key", "value", "field"), [
    ("resolution_x", [10, 20], "resolution_x"),
    ("resolution_y", "10", "resolution_y"),
    ("width", True, "width"),
    ("height", 0, "height"),
])
def test_only_typed_scalar_raster_statistics_can_pass(payload, key, value, field):
    payload["context"]["roles"][1]["asset"][key] = value
    _rejected(_wire(payload), f"$.payload.context.roles[1].asset.{field}")


def test_only_declared_option_types_and_string_enum_values_can_pass(payload):
    options = payload["candidate_methods"][0]["options"]
    options[:] = [{"name": "spatial_relation", "type": "enum", "required": True, "values": [[1, 2]]}]
    _rejected(_wire(payload), "$.payload.candidate_methods[0].options[0].values[0]")
    options[0]["values"] = ["117.25", "39.9"]
    _rejected(_wire(payload), "$.payload.candidate_methods[0].options[0].values[0]")
    options[:] = [{"name": "scope", "type": "record", "required": True}]
    _rejected(_wire(payload), "$.payload.candidate_methods[0].options[0].type")
    options[:] = [{"name": "scope", "type": "reference", "required": 1}]
    _rejected(_wire(payload), "$.payload.candidate_methods[0].options[0].required")
    options[:] = [{"name": "threshold", "type": "user_defined", "required": True},
                  {"name": "resampling", "type": "user_selected", "required": True}]
    check_outbound_request(_wire(payload))


def test_safe_crs_text_and_declared_table_statistics_are_accepted(payload):
    payload["context"]["goal"] = "使用 EPSG:32650 统计 500 米范围内要素"
    payload["context"]["roles"].append({
        "role": "records", "readiness": "READY",
        "asset": {"kind": "table", "row_count": 0, "fields": [{"name": "统计时间", "type": "datetime"}]},
    })
    check_outbound_request(_wire(payload))


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_json_numbers_are_rejected_before_structure_validation(payload, constant):
    serialized = _wire(payload)
    assert '"resolution_x":10.0' in serialized
    _rejected(serialized.replace('"resolution_x":10.0', f'"resolution_x":{constant}'), "$")


@pytest.mark.parametrize("malformed", [
    b"\xff", '{"payload":', '{"payload":{},"payload":{"private":"raw"}}', r'{"payload":{},"payl\u006fad":{}}',
])
def test_malformed_utf8_json_and_duplicate_keys_fail_closed(malformed):
    _rejected(malformed, "$")


def test_rejects_non_unicode_scalar_after_decoding_json_escapes(payload):
    payload["context"]["goal"] = "Use " + chr(0xD800)
    _rejected(json.dumps({"payload": payload}, ensure_ascii=True), "$.payload.context.goal")
