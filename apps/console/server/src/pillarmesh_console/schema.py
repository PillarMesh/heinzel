from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contracts import FIXTURE_EVIDENCE_POLICY_MARKER, ConsoleApiSchema

_SCHEMA_ID = "https://pillarmesh.com/schemas/console-api-v1.json"
_ENVELOPE_MARKER = "x-pillarmesh-console-envelope"
_NO_AUTHORITATIVE_EVIDENCE = "NoAuthoritativeEvidence"


def _remove_discriminator_metadata(value: object) -> None:
    if isinstance(value, dict):
        value.pop("discriminator", None)
        for nested_value in value.values():
            _remove_discriminator_metadata(nested_value)
    elif isinstance(value, list):
        for nested_value in value:
            _remove_discriminator_metadata(nested_value)


def _discover_fixture_evidence_policies(value: object) -> dict[str, object]:
    policies: dict[str, object] = {}

    def visit(nested_value: object) -> None:
        if isinstance(nested_value, dict):
            properties = nested_value.get("properties")
            if isinstance(properties, dict):
                for property_name, property_schema in properties.items():
                    if not isinstance(property_name, str) or not isinstance(property_schema, dict):
                        continue
                    policy = property_schema.pop(FIXTURE_EVIDENCE_POLICY_MARKER, None)
                    if policy is not None:
                        if not isinstance(policy, dict):
                            raise ValueError("fixture evidence policy marker must contain a schema")
                        existing_policy = policies.get(property_name)
                        if existing_policy is not None and existing_policy != policy:
                            raise ValueError(
                                f"conflicting fixture evidence policies for {property_name}"
                            )
                        policies[property_name] = policy
            for child_value in nested_value.values():
                visit(child_value)
        elif isinstance(nested_value, list):
            for child_value in nested_value:
                visit(child_value)

    visit(value)
    if not policies:
        raise ValueError("console API schema did not contain fixture evidence policy markers")
    return {property_name: policies[property_name] for property_name in sorted(policies)}


def _no_authoritative_evidence_schema(
    evidence_properties: dict[str, object],
) -> dict[str, object]:
    recursive_reference = {"$ref": f"#/$defs/{_NO_AUTHORITATIVE_EVIDENCE}"}
    return {
        "anyOf": [
            {
                "type": "array",
                "items": recursive_reference,
            },
            {
                "type": "object",
                "properties": evidence_properties,
                "additionalProperties": recursive_reference,
            },
            {
                "not": {
                    "anyOf": [
                        {"type": "array"},
                        {"type": "object"},
                    ]
                }
            },
        ]
    }


def _fixture_evidence_guard() -> dict[str, object]:
    return {
        "if": {
            "type": "object",
            "properties": {
                "meta": {
                    "type": "object",
                    "properties": {"data_provenance": {"const": "demo_fixture"}},
                    "required": ["data_provenance"],
                }
            },
            "required": ["meta"],
        },
        "then": {
            "type": "object",
            "properties": {
                "data": {"$ref": f"#/$defs/{_NO_AUTHORITATIVE_EVIDENCE}"},
            },
            "required": ["data"],
        },
    }


def _add_fixture_evidence_guards(schema: dict[str, object]) -> None:
    evidence_properties = _discover_fixture_evidence_policies(schema)
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise ValueError("console API schema must define reusable models")

    guarded_count = 0
    for definition in definitions.values():
        if not isinstance(definition, dict) or definition.pop(_ENVELOPE_MARKER, None) is not True:
            continue
        all_of = definition.setdefault("allOf", [])
        if not isinstance(all_of, list):
            raise ValueError("console envelope schema allOf must be an array")
        all_of.append(_fixture_evidence_guard())
        guarded_count += 1

    if guarded_count == 0:
        raise ValueError("console API schema did not contain any response envelopes")
    definitions[_NO_AUTHORITATIVE_EVIDENCE] = _no_authoritative_evidence_schema(evidence_properties)


def console_api_schema() -> dict[str, object]:
    schema = ConsoleApiSchema.model_json_schema(ref_template="#/$defs/{model}")
    _add_fixture_evidence_guards(schema)
    _remove_discriminator_metadata(schema)
    schema["$id"] = _SCHEMA_ID
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "ConsoleApiSchema"
    return schema


def canonical_schema_json() -> str:
    return json.dumps(console_api_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_schema(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(canonical_schema_json(), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the canonical console API JSON Schema")
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    write_schema(arguments.output)


if __name__ == "__main__":
    main()
