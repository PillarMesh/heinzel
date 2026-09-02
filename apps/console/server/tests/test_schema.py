from __future__ import annotations

import json
from pathlib import Path

from pillarmesh_console.contracts import ConsoleApiSchema
from pillarmesh_console.schema import canonical_schema_json, console_api_schema

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_SCHEMA_PATH = _REPOSITORY_ROOT / "apps/console/schema/console-api-v1.json"
_ENVELOPE_MARKER = "x-pillarmesh-console-envelope"
_EVIDENCE_POLICY_MARKER = "x-pillarmesh-fixture-evidence-policy"


def _raw_console_api_schema() -> dict[str, object]:
    return ConsoleApiSchema.model_json_schema(ref_template="#/$defs/{model}")


def _fixture_guard_count(definition: object) -> int:
    if not isinstance(definition, dict):
        return 0
    all_of = definition.get("allOf")
    if not isinstance(all_of, list):
        return 0

    count = 0
    for candidate in all_of:
        if not isinstance(candidate, dict):
            continue
        condition = candidate.get("if")
        if not isinstance(condition, dict):
            continue
        properties = condition.get("properties")
        if not isinstance(properties, dict):
            continue
        meta = properties.get("meta")
        if not isinstance(meta, dict):
            continue
        meta_properties = meta.get("properties")
        if not isinstance(meta_properties, dict):
            continue
        provenance = meta_properties.get("data_provenance")
        if isinstance(provenance, dict) and provenance.get("const") == "demo_fixture":
            count += 1
    return count


def _marked_evidence_policies(schema: dict[str, object]) -> dict[str, object]:
    definitions = schema["$defs"]
    policies: dict[str, object] = {}

    assert isinstance(definitions, dict)
    for definition in definitions.values():
        if not isinstance(definition, dict):
            continue
        properties = definition.get("properties")
        if not isinstance(properties, dict):
            continue
        for property_name, property_schema in properties.items():
            if not isinstance(property_name, str) or not isinstance(property_schema, dict):
                continue
            policy = property_schema.get(_EVIDENCE_POLICY_MARKER)
            if policy is None:
                continue
            assert property_name not in policies or policies[property_name] == policy
            policies[property_name] = policy
    return policies


def _root_data_envelope_definitions(schema: dict[str, object]) -> set[str]:
    properties = schema["properties"]
    definitions = schema["$defs"]
    envelope_definitions: set[str] = set()

    assert isinstance(properties, dict)
    assert isinstance(definitions, dict)
    for response_name, response_schema in properties.items():
        if not isinstance(response_name, str) or not response_name.endswith("_response"):
            continue
        if not isinstance(response_schema, dict):
            continue
        reference = response_schema.get("$ref")
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            continue
        definition_name = reference.removeprefix("#/$defs/")
        definition = definitions[definition_name]
        if not isinstance(definition, dict):
            continue
        definition_properties = definition.get("properties")
        if isinstance(definition_properties, dict) and "data" in definition_properties:
            envelope_definitions.add(definition_name)
    return envelope_definitions


def test_committed_schema_matches_the_canonical_contract() -> None:
    assert _SCHEMA_PATH.read_text(encoding="utf-8") == canonical_schema_json()


def test_schema_generation_is_deterministic() -> None:
    first = canonical_schema_json()
    second = canonical_schema_json()

    assert first == second
    assert json.dumps(json.loads(first), indent=2, sort_keys=True) + "\n" == first


def test_every_generated_object_schema_rejects_unknown_fields() -> None:
    schema = console_api_schema()
    definitions = schema["$defs"]
    object_count = 0

    assert isinstance(definitions, dict)
    for name, definition in definitions.items():
        if name == "NoAuthoritativeEvidence" or not isinstance(definition, dict):
            continue
        if definition.get("type") == "object":
            object_count += 1
            assert definition.get("additionalProperties") is False

    assert object_count > 20


def test_schema_exposes_every_modeled_response_and_command() -> None:
    properties = console_api_schema()["properties"]

    assert set(properties) == {
        "session_response",
        "workspace_response",
        "setup_response",
        "review_response",
        "inbox_response",
        "request_detail_response",
        "requester_requests_response",
        "requester_request_response",
        "conversation_response",
        "clarified_outcome_response",
        "data_product_response",
        "runs_response",
        "catalog_asset_response",
        "dashboard_response",
        "evidence_response",
        "operation_response",
        "error_response",
        "warehouse_binding_command",
        "process_package_command",
        "decision_command",
        "create_request_command",
        "conversation_message_command",
        "clarified_outcome_acceptance_command",
        "retry_operation_command",
        "reset_command",
    }


def test_schema_expresses_session_role_membership_and_complete_retry_authority() -> None:
    definitions = console_api_schema()["$defs"]

    assert isinstance(definitions, dict)
    session_schema = definitions["SessionView"]
    operation_schema = definitions["OperationView"]
    assert isinstance(session_schema, dict)
    assert isinstance(operation_schema, dict)

    session_constraints = json.dumps(session_schema.get("allOf"), sort_keys=True)
    operation_constraints = json.dumps(operation_schema.get("allOf"), sort_keys=True)
    for role in (
        "requester",
        "data_architect",
        "data_owner",
        "policy_approver",
        "budget_approver",
    ):
        assert f'"const": "{role}"' in session_constraints
        assert f'"contains": {{"const": "{role}"}}' in session_constraints
    assert '"operation_digest", "retry_token"' in operation_constraints
    assert '"classification": {"const": "transient"}' in operation_constraints
    assert '"contains": {"const": "retry"}' in operation_constraints


def test_schema_derives_fixture_evidence_properties_from_pydantic_markers() -> None:
    marked_policies = _marked_evidence_policies(_raw_console_api_schema())
    final_schema = console_api_schema()
    definitions = final_schema["$defs"]

    assert marked_policies
    assert isinstance(definitions, dict)
    recursive_policy = definitions["NoAuthoritativeEvidence"]
    assert isinstance(recursive_policy, dict)
    object_branches = [
        branch
        for branch in recursive_policy["anyOf"]
        if isinstance(branch, dict) and branch.get("type") == "object"
    ]
    assert len(object_branches) == 1
    assert object_branches[0]["properties"] == marked_policies
    assert _EVIDENCE_POLICY_MARKER not in json.dumps(final_schema, sort_keys=True)


def test_every_raw_marked_envelope_gets_exactly_one_fixture_guard() -> None:
    raw_schema = _raw_console_api_schema()
    raw_definitions = raw_schema["$defs"]
    final_definitions = console_api_schema()["$defs"]

    assert isinstance(raw_definitions, dict)
    assert isinstance(final_definitions, dict)
    expected_envelopes = _root_data_envelope_definitions(raw_schema)
    marked_envelopes = {
        name
        for name, definition in raw_definitions.items()
        if isinstance(definition, dict) and definition.get(_ENVELOPE_MARKER) is True
    }
    guarded_envelopes = {
        name
        for name, definition in final_definitions.items()
        if _fixture_guard_count(definition) > 0
    }

    assert expected_envelopes
    assert expected_envelopes == marked_envelopes == guarded_envelopes
    for envelope_name in expected_envelopes:
        assert _fixture_guard_count(final_definitions[envelope_name]) == 1


def test_final_schema_removes_internal_markers_and_discriminator_metadata() -> None:
    serialized = json.dumps(console_api_schema(), sort_keys=True)

    assert "discriminator" not in serialized
    assert _ENVELOPE_MARKER not in serialized
    assert _EVIDENCE_POLICY_MARKER not in serialized
