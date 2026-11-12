from pathlib import Path

import pytest
import yaml

from geopilot.methods.loader import MethodLoadIssue, load_method_catalog
from geopilot.methods.models import MethodRef


def _definition(method_id, version=1):
    return {
        "id": method_id,
        "version": version,
        "locked_order": True,
        "inputs": {"features": "vector_point"},
        "critical_options": {},
        "steps": [{"key": "filter"}],
        "expected_artifacts": [],
        "check_rules": [],
    }


def _write_method(directory, filename, definition):
    path = directory / filename
    path.write_text(yaml.safe_dump(definition, sort_keys=False), encoding="utf-8")
    return path


def test_catalog_lists_methods_and_queries_exact_or_latest_version(tmp_path):
    _write_method(tmp_path, "z-latest.yaml", _definition("filter", 10))
    _write_method(tmp_path, "m-first.yml", _definition("filter", 2))
    _write_method(tmp_path, "a-method.yaml", _definition("summary", 1))

    catalog = load_method_catalog(str(tmp_path))

    assert not catalog.issues
    assert [method.ref for method in catalog.list()] == [
        MethodRef(id="filter", version=2),
        MethodRef(id="filter", version=10),
        MethodRef(id="summary", version=1),
    ]
    assert catalog.get("filter").version == 10
    assert catalog.get("filter", 2).version == 2
    with pytest.raises(KeyError):
        catalog.get("missing")
    with pytest.raises(KeyError):
        catalog.get("filter", 1)
    with pytest.raises(KeyError):
        catalog.get("summary", True)


def test_only_yaml_files_immediately_in_directory_are_loaded(tmp_path):
    _write_method(tmp_path, "ignored.json", _definition("ignored"))
    nested = tmp_path / "nested.yaml"
    nested.mkdir()
    _write_method(nested, "method.yaml", _definition("nested"))

    catalog = load_method_catalog(tmp_path)

    assert not catalog.list()
    assert not catalog.issues


def test_a_missing_catalog_directory_reports_the_filesystem_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_method_catalog(tmp_path / "missing")


def test_file_errors_are_located_and_do_not_hide_valid_methods(tmp_path):
    _write_method(tmp_path, "working.yaml", _definition("working"))
    schema_failure = _definition("bad-schema")
    schema_failure["steps"] = [{"key": 42}]
    schema_path = _write_method(tmp_path, "z-schema.yaml", schema_failure)
    yaml_path = tmp_path / "m-syntax.yml"
    yaml_path.write_text("steps: [\n", encoding="utf-8")
    encoding_path = tmp_path / "a-encoding.yaml"
    encoding_path.write_bytes(b"\xff\xfe\xff")

    catalog = load_method_catalog(tmp_path)

    assert [method.id for method in catalog.list()] == ["working"]
    assert all(isinstance(issue, MethodLoadIssue) for issue in catalog.issues)
    encoding, syntax, schema = catalog.issues
    assert (encoding.path, encoding.field) == (encoding_path, "$")
    assert "utf-8" in encoding.message.lower()
    assert (syntax.path, syntax.field) == (yaml_path, "$")
    assert "line" in syntax.message
    assert (schema.path, schema.field) == (schema_path, "steps.0.key")
    assert "string" in schema.message


@pytest.mark.parametrize(
    "contents",
    [
        "id: first\nid: second\n",
        "steps: [{key: filter, parameters: {criteria: {literal: {field: a, field: b}}}}]\n",
        "inputs:\n  <<: &defaults {features: vector_point}\n  features: vector_line\n",
        "inputs:\n  <<: {features: vector_point}\n  <<: {extent: vector_polygon}\n",
    ],
)
def test_yaml_duplicate_keys_are_rejected_at_any_depth(tmp_path, contents):
    path = tmp_path / "duplicate.yaml"
    path.write_text(contents, encoding="utf-8")
    _write_method(tmp_path, "valid.yaml", _definition("valid"))

    catalog = load_method_catalog(tmp_path)

    assert [method.id for method in catalog.list()] == ["valid"]
    (issue,) = catalog.issues
    assert issue.path == path
    assert "duplicate key" in issue.message
    assert "line" in issue.message


@pytest.mark.parametrize(
    "contents",
    [
        "id: one\n---\nid: two\n",
        "id: !!python/name:builtins.str ''\n",
        "id: 2026-02-31\n",
        "id: !!timestamp invalid\n",
        "version: !!bool invalid\n",
    ],
)
def test_invalid_yaml_documents_are_isolated(tmp_path, contents):
    path = tmp_path / "invalid.yaml"
    path.write_text(contents, encoding="utf-8")
    _write_method(tmp_path, "valid.yaml", _definition("valid"))

    catalog = load_method_catalog(tmp_path)

    assert [method.id for method in catalog.list()] == ["valid"]
    (issue,) = catalog.issues
    assert (issue.path, issue.field) == (path, "$")
    assert issue.message


def test_duplicate_method_versions_reject_every_copy_and_keep_other_versions(tmp_path):
    paths = [
        _write_method(tmp_path, name, _definition("filter", 3))
        for name in ("a.yaml", "b.yml", "c.yaml")
    ]
    _write_method(tmp_path, "older.yml", _definition("filter", 2))
    _write_method(tmp_path, "other.yaml", _definition("summary", 3))

    catalog = load_method_catalog(tmp_path)

    assert [issue.path for issue in catalog.issues] == paths
    for issue in catalog.issues:
        assert issue.field == "version"
        assert "filter" in issue.message and "3" in issue.message
        assert all(path.name in issue.message for path in paths)
    assert [(method.id, method.version) for method in catalog.list()] == [
        ("filter", 2), ("summary", 3),
    ]
    assert catalog.get("filter").version == 2
    with pytest.raises(KeyError):
        catalog.get("filter", 3)


def test_get_and_list_return_deep_copies(tmp_path):
    definition = _definition("filter")
    definition["critical_options"]["criteria"] = {
        "type": "json", "default": {"values": ["park"]},
    }
    definition["steps"][0]["parameters"] = {"criteria": {"literal": {"values": ["park"]}}}
    definition["steps"][0]["outputs"] = {"filtered": "vector_point"}
    _write_method(tmp_path, "filter.yaml", definition)
    catalog = load_method_catalog(tmp_path)

    lookup = catalog.get("filter")
    lookup.inputs.clear()
    lookup.critical_options["criteria"].default["values"].append("road")
    lookup.steps[0].parameters["criteria"].literal["values"].clear()
    listing = catalog.list()
    listing[0].steps[0].outputs.clear()
    listing.clear()

    original = catalog.get("filter", 1)
    assert original.inputs == {"features": "vector_point"}
    assert original.critical_options["criteria"].default == {"values": ["park"]}
    assert original.steps[0].parameters["criteria"].literal == {"values": ["park"]}
    assert original.steps[0].outputs == {"filtered": "vector_point"}
    assert catalog.list() == [original]


def test_sample_catalog_loads_v2_road_and_nonmeasurement_attribute_filter():
    config = Path(__file__).resolve().parents[2] / "config" / "methods"
    catalog = load_method_catalog(config)

    assert not catalog.issues
    road = catalog.get("road-buffer-statistics", 1)
    assert road.locked_order
    assert [step.key for step in road.steps] == [
        "expand_analysis_extent",
        "select_roads_from_expanded_extent",
        "choose_measurement_method",
        "create_buffer",
        "apply_spatial_relation",
        "clip_back_to_analysis_extent",
        "aggregate_by_zone",
    ]
    assert road.expected_artifacts == ("buffer_extent", "selected_targets", "zone_statistics")

    attribute_filter = catalog.get("attribute-filter-export", 1)
    assert attribute_filter.inputs == {"features": "vector_point"}
    assert tuple(attribute_filter.critical_options) == ("field_name", "match_value")
    (step,) = attribute_filter.steps
    assert step.inputs["source"].asset == "features"
    assert step.parameters["field"].option == "field_name"
    assert step.parameters["value"].option == "match_value"
    (artifact,) = attribute_filter.expected_artifacts
    assert (artifact.step, artifact.output, artifact.type) == (step.key, "filtered", "vector_point")
