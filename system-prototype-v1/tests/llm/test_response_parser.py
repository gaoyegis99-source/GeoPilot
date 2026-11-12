import json
import traceback
from pathlib import Path

import pytest
import yaml

from geopilot.application.task_context import build_task_context
from geopilot.data.assets import register_asset
from geopilot.domain.task import TaskGoal
from geopilot.domain.tool_contract import ToolContract
from geopilot.llm.base import ModelRequest, ModelResponse
from geopilot.llm.context_builder import build_outbound_context
from geopilot.llm.prompt_builder import build_model_request
from geopilot.llm.response_parser import ModelResponseError, ModelSuggestion, ParsedModelSuggestion, parse_model_response
from geopilot.methods.loader import MethodCatalog, load_method_catalog
from geopilot.methods.models import MethodRef
from geopilot.persistence.schema import initialize_database
from geopilot.tools.registry import ToolRegistry


SECRET = "synthetic-model-response-key-do-not-display"
ROOT = Path(__file__).resolve().parents[2]


def _request():
    return ModelRequest(payload={
        "context": {"goal": "选择可用的空间分析方法", "roles": []},
        "candidate_methods": [
            {"id": "road-buffer-statistics", "version": 2,
             "inputs": [{"role": "targets", "type": "vector_point"}],
             "options": [
                 {"name": "spatial_relation", "type": "enum", "required": True, "values": ["intersects", "within"]},
                 {"name": "distance_m", "type": "number", "required": True, "minimum_exclusive": 0, "unit": "meter"},
             ], "steps": [{"key": "choose_measurement_method", "tool": {"id": "prepare-lines", "version": 1}}]},
            {"id": "attribute-filter-export", "version": 1, "inputs": [], "options": [], "steps": []},
        ],
        "candidate_tools": [{"id": "prepare-lines", "version": 1}],
    })


def _document(**changes):
    return {
        "method": {"id": "road-buffer-statistics", "version": 2},
        "goal": {"summary": "筛选影响范围内的目标"},
        "options": {"spatial_relation": "within"},
        "missing": ["options.distance_m", "inputs.targets"],
        **changes,
    }


def _parse(request=None, **changes):
    return parse_model_response(ModelResponse(text=json.dumps(_document(**changes))), request=request or _request())


def test_valid_suggestion_preserves_missing_fields_and_typed_references():
    suggestion = _parse(explanation="当前需要确认缓冲距离。")
    assert isinstance(suggestion, ParsedModelSuggestion)
    assert suggestion.method == MethodRef(id="road-buffer-statistics", version=2)
    assert suggestion.goal == TaskGoal(summary="筛选影响范围内的目标")
    assert suggestion.options == {"spatial_relation": "within"}
    assert suggestion.missing == ["options.distance_m", "inputs.targets"]
    assert suggestion.explanation == "当前需要确认缓冲距离。" and suggestion.explanation_issue is None


def test_wire_schema_excludes_parser_diagnostics_and_execution_fields():
    schema = ModelSuggestion.model_json_schema()
    assert set(schema["required"]) == {"method", "goal", "options", "missing"}
    assert set(schema["properties"]) == {"method", "goal", "options", "missing", "explanation"}
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["MethodRef"]["additionalProperties"] is False
    assert schema["$defs"]["TaskGoal"]["additionalProperties"] is False


@pytest.mark.parametrize("explanation", [False, 1, [SECRET], {SECRET: "unexpected"}, chr(0xD800)])
def test_invalid_explanation_keeps_valid_structure_with_separate_issue(explanation):
    suggestion = _parse(explanation=explanation)
    assert suggestion.method.id == "road-buffer-statistics" and suggestion.options["spatial_relation"] == "within"
    assert suggestion.missing == ["options.distance_m", "inputs.targets"]
    assert suggestion.explanation is None
    assert (suggestion.explanation_issue.code, suggestion.explanation_issue.field) == ("MODEL_EXPLANATION_INVALID", "explanation")
    assert SECRET not in suggestion.explanation_issue.model_dump_json()


def test_explanation_can_be_omitted_or_null():
    assert _parse().explanation is None and _parse().explanation_issue is None
    assert _parse(explanation=None).explanation is None and _parse(explanation=None).explanation_issue is None


@pytest.mark.parametrize(("changes", "field"), [
    ({"method": {"id": "road-buffer-statistics", "version": True}}, "method.version"),
    ({"method": {"id": "road-buffer-statistics", "version": "2"}}, "method.version"),
    ({"method": {"id": "road-buffer-statistics", "version": 0}}, "method.version"),
    ({"method": {"id": "road-buffer-statistics", "version": 2, SECRET: True}}, "method"),
    ({"goal": {"summary": " "}}, "goal.summary"),
    ({"goal": {"summary": "new goal", SECRET: True}}, "goal"),
    ({"goal": "new goal"}, "goal"),
    ({"options": []}, "options"),
    ({"missing": "options.distance_m"}, "missing"),
    ({"missing": ["/private/sample"]}, "missing"),
    ({"missing": ["options..distance_m"]}, "missing"),
    ({"missing": [1]}, "missing"),
    ({"steps": [{"run": SECRET}]}, "response"),
    ({"tool": {"id": SECRET, "version": 1}}, "response"),
    ({SECRET: "unexpected"}, "response"),
])
def test_schema_rejects_unknown_execution_fields_and_wrong_types(changes, field):
    with pytest.raises(ModelResponseError) as failure:
        _parse(**changes)
    assert (failure.value.code, failure.value.field) == ("MODEL_INVALID_RESPONSE", field)
    assert SECRET not in str(failure.value) + "".join(traceback.format_exception(failure.value))


@pytest.mark.parametrize("text", [
    "", "[]", "null", "not JSON " + SECRET, "```json\n{}\n```",
    '{"method":{"id":"road-buffer-statistics","version":2},"goal":{"summary":"a"},"options":{"distance_m":NaN},"missing":[]}',
    '{"method":{"id":"road-buffer-statistics","version":2},"goal":{"summary":"a"},"options":{"distance_m":1e309},"missing":[]}',
    '{"method":{"id":"road-buffer-statistics","version":2},"goal":{"summary":"a"},"options":{"distance_m":1,"distance_m":2},"missing":[]}',
])
def test_invalid_or_nonstandard_json_is_rejected_without_response_echo(text):
    with pytest.raises(ModelResponseError) as failure:
        parse_model_response(ModelResponse(text=text), request=_request())
    assert failure.value.code == "MODEL_INVALID_RESPONSE"
    assert SECRET not in "".join(traceback.format_exception(failure.value))


@pytest.mark.parametrize("missing_key", ["method", "goal", "options", "missing"])
def test_structured_wire_requires_all_fields(missing_key):
    document = _document()
    document.pop(missing_key)
    with pytest.raises(ModelResponseError) as failure:
        parse_model_response(ModelResponse(text=json.dumps(document)), request=_request())
    assert (failure.value.code, failure.value.field) == ("MODEL_INVALID_RESPONSE", missing_key)


@pytest.mark.parametrize("method", [
    {"id": "road-buffer-statistics", "version": 1}, {"id": SECRET, "version": 2},
    {"id": "prepare-lines", "version": 1},
])
def test_only_exact_offered_method_id_and_version_are_accepted(method):
    with pytest.raises(ModelResponseError) as failure:
        _parse(method=method)
    assert (failure.value.code, failure.value.field) == ("MODEL_CANDIDATE_UNKNOWN", "method")
    assert SECRET not in str(failure.value)


@pytest.mark.parametrize("value", ["Within", "within ", True, ["within"], None, SECRET])
def test_declared_enum_values_are_exact(value):
    with pytest.raises(ModelResponseError) as failure:
        _parse(options={"spatial_relation": value})
    assert (failure.value.code, failure.value.field) == ("MODEL_OPTION_ENUM_INVALID", "options.spatial_relation")
    assert SECRET not in str(failure.value)


def test_unknown_option_keys_are_rejected_with_a_fixed_error_location():
    with pytest.raises(ModelResponseError) as failure:
        _parse(options={SECRET: "unknown"})
    assert (failure.value.code, failure.value.field) == ("MODEL_OPTION_UNKNOWN", "options")
    assert SECRET not in str(failure.value) + repr(failure.value)


def test_full_parameter_constraints_remain_for_local_candidate_validation():
    assert _parse(options={"distance_m": -5}).options == {"distance_m": -5}
    assert _parse(options={"distance_m": "needs human review"}).options == {"distance_m": "needs human review"}


@pytest.mark.parametrize("fault", ["no_candidates", "legacy_options_mapping", "duplicate_method", "duplicate_option", "invalid_enum"])
def test_malformed_candidate_wire_fails_closed(fault):
    payload = _request().model_dump(mode="json")["payload"]
    candidates = payload["candidate_methods"]
    if fault == "no_candidates":
        payload.pop("candidate_methods")
    elif fault == "legacy_options_mapping":
        candidates[0]["options"] = {"spatial_relation": {"type": "enum", "values": ["within"]}}
    elif fault == "duplicate_method":
        candidates.append(candidates[0])
    elif fault == "duplicate_option":
        candidates[0]["options"].append(candidates[0]["options"][0])
    else:
        candidates[0]["options"][0]["values"] = []
    with pytest.raises(ModelResponseError) as failure:
        _parse(ModelRequest(payload=payload))
    assert (failure.value.code, failure.value.field) == ("MODEL_REQUEST_INVALID", "candidate_methods")


def test_parser_consumes_real_prompt_builder_wire_from_a_registered_method(tmp_path):
    config = ROOT / "config"
    method = load_method_catalog(config / "methods").get("spatial-select-and-clip", 1)
    contracts = [ToolContract.model_validate(yaml.safe_load(
        (config / "tools" / f"{step.tool.id}.yaml").read_text(encoding="utf-8"),
    )) for step in method.steps]
    registry = ToolRegistry(python_implementations={contract.implementation for contract in contracts})
    for contract in contracts:
        registry.register(contract)
    database = tmp_path / "project.sqlite"
    initialize_database(database)
    source = ROOT / "tests/fixtures/vector/road-buffer.gpkg"
    points = register_asset(database, source, layer="targets")
    extent = register_asset(database, source, layer="analysis_extent")
    context = build_task_context(
        database, task_id="test-model-suggestion", goal=TaskGoal(summary="筛选分析范围内的目标"),
        method=method, selected={"targets": points.ref, "relation_area": extent.ref, "analysis_extent": extent.ref},
        registry=registry,
    )
    request = build_model_request(
        build_outbound_context(context, [points, extent]), MethodCatalog([method], ()),
        registry=registry, registered_rules={"output_exists"},
    )
    offered, = request.payload["candidate_methods"]
    assert offered["options"] == [{"name": "spatial_relation", "required": True,
                                  "type": "enum", "values": ["intersects", "within"]}]
    method_ref = method.ref.model_dump(mode="json")
    suggestion = _parse(request, method=method_ref, options={"spatial_relation": "within"}, missing=[])
    assert suggestion.method == method.ref and suggestion.options == {"spatial_relation": "within"}
    with pytest.raises(ModelResponseError) as failure:
        _parse(request, method=method_ref, options={"spatial_relation": "contains"}, missing=[])
    assert (failure.value.code, failure.value.field) == ("MODEL_OPTION_ENUM_INVALID", "options.spatial_relation")
