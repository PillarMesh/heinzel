from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from typing import get_args, get_origin

import pytest
from pillarmesh_console import contracts
from pillarmesh_console.contracts import (
    AccessPreviewProposalView,
    ApiMeta,
    ClarifiedOutcomeAcceptanceCommand,
    ConsoleEnvelope,
    ConversationMessageCommand,
    CreateRequestCommand,
    DataAccessRequestInput,
    DecisionCommand,
    OperationView,
    ProcessPackageCommand,
    RequesterRequestView,
    RequestInput,
    RequestProposalView,
    ResetCommand,
    RetryOperationCommand,
    SessionView,
    SetupView,
    StakeholderAnswerProposalView,
    StakeholderQuestionInput,
    StrictModel,
    WarehouseBindingCommand,
)
from pillarmesh_request_management import ConversationAuthorRole
from pydantic import TypeAdapter, ValidationError


class _NestedEvidence(StrictModel):
    evidence_ref: str | None = None


class _NestedEvidenceReferences(StrictModel):
    evidence_refs: tuple[str, ...] = ()


class _OpaqueEvidence:
    def __init__(self) -> None:
        self.evidence_ref = "ev-0001"


def _session_payload() -> dict[str, object]:
    return {
        "actor": {"display_name": "Dana Architect"},
        "roles": ["data_architect", "data_owner"],
        "active_role": "data_architect",
        "tenant": {"ref": "tenant-display", "display_name": "Northwind Demo"},
        "workspace": {"ref": "workspace-main", "display_name": "Revenue to cash"},
        "csrf_token": "csrf-token-0000000000000000000000000001",
    }


def _requester_payload(updated_at: object) -> dict[str, object]:
    return {
        "request_id": "req-0001",
        "kind": "stakeholder_question",
        "state": "clarifying",
        "title": "Revenue trend",
        "requested_outcome": "Explain the weekly revenue trend",
        "revision": 2,
        "updated_at": updated_at,
        "own_decisions": [],
    }


def test_decision_rejects_browser_supplied_tenant_authority() -> None:
    with pytest.raises(ValidationError):
        DecisionCommand.model_validate(
            {
                "tenant_id": "tenant-other",
                "expected_revision": 3,
                "reviewed_digest": "a" * 64,
                "active_role": "data_architect",
                "decision": "approve",
            }
        )


def test_every_browser_command_excludes_actor_and_tenant_authority() -> None:
    command_models = (
        WarehouseBindingCommand,
        ProcessPackageCommand,
        DecisionCommand,
        CreateRequestCommand,
        ConversationMessageCommand,
        ClarifiedOutcomeAcceptanceCommand,
        RetryOperationCommand,
        ResetCommand,
    )

    for command_model in command_models:
        assert "tenant_id" not in command_model.model_fields
        assert "actor_id" not in command_model.model_fields


def test_fixture_envelope_cannot_claim_real_evidence() -> None:
    with pytest.raises(ValidationError, match="fixture responses cannot carry evidence"):
        ConsoleEnvelope[OperationView](
            meta=ApiMeta(data_provenance="demo_fixture", correlation_id="corr-0001"),
            data=OperationView(
                operation_id="op-0001", revision=1, state="succeeded", evidence_ref="ev-0001"
            ),
        )


def test_governed_envelope_may_reference_authoritative_evidence() -> None:
    envelope = ConsoleEnvelope[OperationView](
        meta=ApiMeta(data_provenance="governed_local", correlation_id="corr-0001"),
        data=OperationView(
            operation_id="op-0001",
            revision=1,
            state="succeeded",
            phase="complete",
            summary="Provisioning completed",
            evidence_ref="ev-0001",
        ),
    )

    assert envelope.data.evidence_ref == "ev-0001"


@pytest.mark.parametrize(
    "nested_data",
    (
        ({"evidence_ref": "ev-0001"},),
        [_NestedEvidence(evidence_ref="ev-0001")],
        {"nested": {"evidence_refs": ["ev-0001"]}},
        {_NestedEvidence(evidence_ref="ev-0001")},
        frozenset({_NestedEvidence(evidence_ref="ev-0001")}),
    ),
    ids=("tuple", "list", "mapping", "set", "frozenset"),
)
def test_fixture_generic_envelope_rejects_evidence_in_every_typed_container(
    nested_data: object,
) -> None:
    with pytest.raises(ValidationError, match="fixture responses cannot carry evidence"):
        ConsoleEnvelope[object](
            meta=ApiMeta(data_provenance="demo_fixture", correlation_id="corr-0001"),
            data=nested_data,
        )


def test_fixture_mapping_inspects_hashable_keys_for_nested_evidence() -> None:
    nested_key = ("nested", _NestedEvidence(evidence_ref="ev-0001"))

    with pytest.raises(ValidationError, match="fixture responses cannot carry evidence"):
        ConsoleEnvelope[object](
            meta=ApiMeta(data_provenance="demo_fixture", correlation_id="corr-0001"),
            data={nested_key: "safe-value"},
        )


def test_fixture_mapping_cycle_without_evidence_remains_safe() -> None:
    cyclic_data: dict[str, object] = {}
    cyclic_data["self"] = cyclic_data

    envelope = ConsoleEnvelope[object](
        meta=ApiMeta(data_provenance="demo_fixture", correlation_id="corr-0001"),
        data=cyclic_data,
    )

    assert envelope.data is cyclic_data


def test_fixture_generic_envelope_rejects_opaque_objects_instead_of_bypassing_safety() -> None:
    with pytest.raises(ValidationError, match="fixture responses cannot carry evidence"):
        ConsoleEnvelope[object](
            meta=ApiMeta(data_provenance="demo_fixture", correlation_id="corr-0001"),
            data=_OpaqueEvidence(),
        )


def test_fixture_generic_envelope_allows_only_empty_evidence_values() -> None:
    envelope = ConsoleEnvelope[object](
        meta=ApiMeta(data_provenance="demo_fixture", correlation_id="corr-0001"),
        data={
            "empty_ref": {"evidence_ref": ""},
            "null_ref": {"evidence_ref": None},
            "empty_refs": {"evidence_refs": []},
        },
    )

    assert isinstance(envelope.data, dict)


def test_governed_generic_envelope_allows_deeply_nested_evidence() -> None:
    envelope = ConsoleEnvelope[object](
        meta=ApiMeta(data_provenance="governed_local", correlation_id="corr-0001"),
        data={"nested": [{"evidence_refs": ["ev-0001"]}]},
    )

    assert isinstance(envelope.data, dict)


def test_pydantic_policy_registry_drives_schema_and_runtime_enforcement() -> None:
    registry = contracts.FIXTURE_EVIDENCE_POLICIES
    raw_schema = contracts.ConsoleApiSchema.model_json_schema(ref_template="#/$defs/{model}")
    definitions = raw_schema["$defs"]
    emitted_policies: dict[str, object] = {}

    assert isinstance(definitions, dict)
    for definition in definitions.values():
        if not isinstance(definition, dict):
            continue
        properties = definition.get("properties")
        if not isinstance(properties, dict):
            continue
        for field_name, field_schema in properties.items():
            if not isinstance(field_name, str) or not isinstance(field_schema, dict):
                continue
            policy = field_schema.get(contracts.FIXTURE_EVIDENCE_POLICY_MARKER)
            if policy is not None:
                emitted_policies[field_name] = policy

    model_cases = {
        "evidence_ref": _NestedEvidence(evidence_ref="ev-0001"),
        "evidence_refs": _NestedEvidenceReferences(evidence_refs=("ev-0001",)),
    }
    mapping_cases = {
        "evidence_ref": "ev-0001",
        "evidence_refs": ["ev-0001"],
    }
    assert emitted_policies == dict(registry)
    assert set(model_cases) == set(registry)
    assert set(mapping_cases) == set(registry)

    for field_name in registry:
        for data in (model_cases[field_name], {field_name: mapping_cases[field_name]}):
            with pytest.raises(ValidationError, match="fixture responses cannot carry evidence"):
                ConsoleEnvelope[object](
                    meta=ApiMeta(data_provenance="demo_fixture", correlation_id="corr-0001"),
                    data=data,
                )


def test_models_are_frozen_and_reject_unknown_fields() -> None:
    operation = OperationView(
        operation_id="op-0001",
        revision=1,
        state="accepted",
        phase="provisioning",
        summary="Provisioning accepted",
    )

    with pytest.raises(ValidationError, match="frozen"):
        operation.state = "running"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OperationView.model_validate(
            {
                "operation_id": "op-0001",
                "revision": 1,
                "state": "accepted",
                "phase": "provisioning",
                "summary": "Provisioning accepted",
                "provider_operation_id": "private-provider-id",
            }
        )


def test_retry_contract_is_publicly_constructible_without_private_identity() -> None:
    retry_token = "opaque_retry_" + ("7" * 52)
    operation = OperationView.model_validate(
        {
            "operation_id": "op-retryable",
            "revision": 4,
            "state": "failed",
            "phase": "reconciliation",
            "summary": "A transient operation can be retried.",
            "operation_digest": "9" * 64,
            "retry_token": retry_token,
            "failure": {
                "code": "transient-failure",
                "classification": "transient",
                "safe_message": "The operation can be retried safely.",
            },
            "recovery_actions": ["retry"],
        }
    )
    command = RetryOperationCommand.model_validate(
        {
            "expected_revision": operation.revision,
            "operation_digest": operation.operation_digest,
            "retry_token": operation.retry_token,
            "active_role": "data_architect",
        }
    )

    assert command.retry_token == retry_token
    assert "provider" not in operation.model_dump_json()
    assert "private" not in operation.model_dump_json()


def test_operation_revision_and_retry_token_are_required_at_the_correct_boundaries() -> None:
    with pytest.raises(ValidationError):
        OperationView.model_validate(
            {
                "operation_id": "op-0001",
                "state": "accepted",
                "phase": "provisioning",
                "summary": "Provisioning accepted",
            }
        )

    with pytest.raises(ValidationError):
        RetryOperationCommand.model_validate(
            {
                "expected_revision": 1,
                "operation_digest": "9" * 64,
                "active_role": "data_architect",
            }
        )


def test_reset_command_requires_exact_server_issued_snapshot_and_token_authority() -> None:
    valid = {
        "expected_revision": 4,
        "setup_digest": "a" * 64,
        "reset_token": "reset_token_fixture_sequence-0004",
        "active_role": "data_architect",
    }

    assert ResetCommand.model_validate(valid).model_dump() == valid
    for missing in valid:
        with pytest.raises(ValidationError):
            ResetCommand.model_validate(
                {key: value for key, value in valid.items() if key != missing}
            )
    for unknown in ({"tenant_id": "tenant-other"}, {"actor_id": "actor-other"}, {"x": 1}):
        with pytest.raises(ValidationError):
            ResetCommand.model_validate(valid | unknown)


def test_create_request_requires_the_initial_resource_revision() -> None:
    with pytest.raises(ValidationError):
        CreateRequestCommand.model_validate(
            {
                "request_digest": "a" * 64,
                "active_role": "requester",
                "title": "Explain revenue",
                "request": {
                    "kind": "stakeholder_question",
                    "purpose": "Prepare a governed answer.",
                    "question": "Why did revenue change?",
                },
            }
        )


def test_setup_snapshot_digest_changes_with_revision_and_material_state() -> None:
    from pillarmesh_console.contracts import setup_snapshot_digest

    payload = {
        "workspace_ref": "workspace-revenue",
        "revision": 1,
        "reset_token": "reset_token_fixture_sequence-0000",
        "active_stage": "foundation",
        "stages": [{"stage": "foundation", "label": "Foundation", "state": "current"}],
        "warehouse_options": [
            {
                "engine": "postgresql",
                "label": "PostgreSQL",
                "supported_region": "us-west-2",
                "fixed_capacity": "fixed-small",
            }
        ],
    }
    unbound = SetupView.model_validate(payload | {"setup_digest": "0" * 64})
    digest = setup_snapshot_digest(unbound)
    setup = SetupView.model_validate(unbound.model_dump() | {"setup_digest": digest})

    assert setup_snapshot_digest(setup) == digest
    assert setup_snapshot_digest(setup.model_copy(update={"revision": 2})) != digest
    # The reset token is an authorization capability minted per read, not part of
    # the state being agreed. Covering it would make two reads of an unchanged
    # setup disagree, and no digest-guarded command could ever be satisfied.
    assert (
        setup_snapshot_digest(
            setup.model_copy(update={"reset_token": "reset_token_fixture_sequence-0001"})
        )
        == digest
    )
    assert (
        setup_snapshot_digest(setup.model_copy(update={"active_stage": "managed_services"}))
        != digest
    )


def test_every_api_model_is_frozen_strict_and_rejects_unknown_fields() -> None:
    api_models = tuple(
        model
        for _, model in inspect.getmembers(contracts, inspect.isclass)
        if issubclass(model, StrictModel)
    )

    assert len(api_models) > 40
    for api_model in api_models:
        assert api_model.model_config.get("extra") == "forbid"
        assert api_model.model_config.get("frozen") is True
        assert api_model.model_config.get("strict") is True


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("operation_id", "OP-0001"),
        ("operation_id", "a"),
        ("operation_id", "a" + ("b" * 128)),
        ("state", "complete"),
    ),
)
def test_operation_rejects_invalid_public_identifiers_and_closed_states(
    field: str, value: str
) -> None:
    payload = {
        "operation_id": "op-0001",
        "revision": 1,
        "state": "accepted",
        "phase": "provisioning",
        "summary": "Provisioning accepted",
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        OperationView.model_validate(payload)


def test_decision_requires_positive_revision_and_lowercase_sha256_digest() -> None:
    valid = {
        "expected_revision": 1,
        "reviewed_digest": "a" * 64,
        "active_role": "data_architect",
        "decision": "approve",
    }

    assert DecisionCommand.model_validate(valid).expected_revision == 1

    for invalid in (
        {**valid, "expected_revision": 0},
        {**valid, "expected_revision": "1"},
        {**valid, "reviewed_digest": "A" * 64},
        {**valid, "reviewed_digest": "a" * 63},
        {**valid, "active_role": "administrator"},
        {**valid, "decision": "override"},
    ):
        with pytest.raises(ValidationError):
            DecisionCommand.model_validate(invalid)


def test_session_contains_display_context_without_identity_authority() -> None:
    session = SessionView.model_validate(_session_payload())

    assert session.actor.display_name == "Dana Architect"
    assert "tenant_id" not in SessionView.model_fields
    assert "actor_id" not in SessionView.model_fields


def test_json_arrays_become_immutable_tuples() -> None:
    session = SessionView.model_validate(_session_payload())

    assert session.roles == ("data_architect", "data_owner")
    assert isinstance(session.roles, tuple)


@pytest.mark.parametrize(
    "roles",
    (
        {"data_architect"},
        frozenset({"data_architect"}),
        iter(("data_architect",)),
        {"primary": "data_architect"},
        "data_architect",
    ),
    ids=("set", "frozenset", "generator", "mapping", "string"),
)
def test_tuple_fields_reject_non_json_collection_inputs(roles: object) -> None:
    payload = {**_session_payload(), "roles": roles}

    with pytest.raises(ValidationError, match="JSON array or tuple"):
        SessionView.model_validate(payload)


def test_timestamp_accepts_only_rfc3339_strings_or_aware_datetime_objects() -> None:
    from_string = RequesterRequestView.model_validate(_requester_payload("2026-09-01T12:00:00Z"))
    from_datetime = RequesterRequestView.model_validate(
        _requester_payload(datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
    )

    assert from_string.updated_at == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert from_datetime.updated_at == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "updated_at",
    (
        "0001-01-01T00:00:00Z",
        "0001-01-01T23:59:00+23:59",
        "9999-12-31T00:00:59-23:59",
        "9999-12-31T23:59:59.999999+23:59",
        "2024-02-29T23:59:59-23:59",
        "2026-01-01T00:00:00+00:00",
    ),
)
def test_timestamp_accepts_rfc3339_component_boundaries(updated_at: str) -> None:
    requester = RequesterRequestView.model_validate(_requester_payload(updated_at))

    assert requester.updated_at.tzinfo is UTC


@pytest.mark.parametrize(
    "updated_at",
    (0, 1.5, date(2026, 9, 1), "2026-09-01", "2026-09-01T12:00:00"),
)
def test_timestamp_rejects_numeric_date_and_non_timezone_inputs(updated_at: object) -> None:
    with pytest.raises(ValidationError, match=r"RFC 3339|datetime object|timezone-aware"):
        RequesterRequestView.model_validate(_requester_payload(updated_at))


@pytest.mark.parametrize(
    "updated_at",
    (
        "2026-02-31T12:00:00Z",
        "2025-02-29T12:00:00Z",
        "2026-13-01T12:00:00Z",
        "2026-04-31T12:00:00Z",
        "0000-01-01T00:00:00Z",
        "2026-01-01T24:00:00Z",
        "2026-01-01T23:60:00Z",
        "2026-01-01T23:59:60Z",
        "2026-01-01T23:59:59+24:00",
        "2026-01-01T23:59:59+23:60",
        "0001-01-01T00:00:00+23:59",
        "9999-12-31T23:59:59-23:59",
    ),
)
def test_timestamp_rejects_invalid_rfc3339_components_or_utc_rollover(updated_at: str) -> None:
    with pytest.raises(ValidationError, match=r"valid RFC 3339 datetime|normalize to UTC"):
        RequesterRequestView.model_validate(_requester_payload(updated_at))


def test_session_rejects_an_active_role_the_actor_does_not_hold() -> None:
    with pytest.raises(ValidationError, match="active role must be one of the session roles"):
        SessionView.model_validate(
            {
                "actor": {"display_name": "Riley Requester"},
                "roles": ["requester"],
                "active_role": "data_architect",
                "tenant": {"ref": "tenant-display", "display_name": "Northwind Demo"},
                "workspace": {"ref": "workspace-main", "display_name": "Revenue to cash"},
                "csrf_token": "csrf-token-0000000000000000000000000001",
            }
        )


def test_feature_projections_do_not_use_untyped_dictionary_fields() -> None:
    api_models = (
        model
        for _, model in inspect.getmembers(contracts, inspect.isclass)
        if issubclass(model, StrictModel)
    )

    for model in api_models:
        for field in model.model_fields.values():
            assert get_origin(field.annotation) is not dict


def test_requester_projection_cannot_accept_reviewer_or_proposal_details() -> None:
    safe_payload = _requester_payload(datetime(2026, 9, 1, 12, 0, tzinfo=UTC))

    for restricted_field in (
        "answer_candidate",
        "effective_access_scope",
        "reviewer_identities",
        "evidence_ref",
    ):
        with pytest.raises(ValidationError):
            RequesterRequestView.model_validate(
                {**safe_payload, restricted_field: "must-not-cross-boundary"}
            )


def test_request_input_union_requires_exact_kind_tags() -> None:
    adapter: TypeAdapter[RequestInput] = TypeAdapter(RequestInput)
    stakeholder = {
        "kind": "stakeholder_question",
        "purpose": "Understand revenue",
        "question": "Why did revenue change?",
    }
    access = {
        "kind": "data_access",
        "purpose": "Investigate revenue",
        "data_product_ref": "product-revenue",
        "requested_fields": ["order_id"],
        "access_mode": "query",
        "expires_at": "2026-09-02T12:00:00Z",
    }

    assert isinstance(adapter.validate_python(stakeholder), StakeholderQuestionInput)
    assert isinstance(adapter.validate_python(access), DataAccessRequestInput)

    for invalid in (
        {key: value for key, value in stakeholder.items() if key != "kind"},
        {**stakeholder, "kind": "unknown"},
        {**access, "kind": "stakeholder_question"},
    ):
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


def test_request_input_branch_models_require_kind_on_the_wire() -> None:
    with pytest.raises(ValidationError):
        StakeholderQuestionInput.model_validate(
            {"purpose": "Understand revenue", "question": "Why did revenue change?"}
        )

    with pytest.raises(ValidationError):
        DataAccessRequestInput.model_validate(
            {
                "purpose": "Investigate revenue",
                "data_product_ref": "product-revenue",
                "requested_fields": ["order_id"],
                "access_mode": "query",
                "expires_at": "2026-09-02T12:00:00Z",
            }
        )


def test_request_proposal_union_requires_exact_kind_tags() -> None:
    adapter: TypeAdapter[RequestProposalView] = TypeAdapter(RequestProposalView)
    answer = {
        "kind": "stakeholder_answer",
        "purpose": "Understand revenue",
        "candidate": "Revenue increased by ten percent.",
        "metric_version": "revenue-v1",
        "as_of": "2026-09-01T12:00:00Z",
        "freshness": "current",
        "quality_limitations": [],
        "datasets": [],
        "lineage_summary": "Orders to revenue",
        "authorization_summary": "Authorized for aggregate revenue",
        "required_authorities": [],
    }
    access = {
        "kind": "access_preview",
        "purpose": "Investigate revenue",
        "data_product_ref": "product-revenue",
        "access_mode": "query",
        "requested_fields": ["order_id"],
        "effective_scope": ["orders.order_id"],
        "exclusions": [],
        "expires_at": "2026-09-02T12:00:00Z",
        "intended_checks": ["can read order_id"],
        "denied_checks": ["cannot read payment_token"],
        "authority_summary": "Data owner approval required",
        "required_authorities": [],
    }

    assert isinstance(adapter.validate_python(answer), StakeholderAnswerProposalView)
    assert isinstance(adapter.validate_python(access), AccessPreviewProposalView)

    for invalid in (
        {key: value for key, value in answer.items() if key != "kind"},
        {**answer, "kind": "unknown"},
        {**access, "kind": "stakeholder_answer"},
    ):
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


def test_request_proposal_branch_models_require_kind_on_the_wire() -> None:
    with pytest.raises(ValidationError):
        StakeholderAnswerProposalView.model_validate(
            {
                "purpose": "Understand revenue",
                "candidate": "Revenue increased by ten percent.",
                "metric_version": "revenue-v1",
                "as_of": "2026-09-01T12:00:00Z",
                "freshness": "current",
                "lineage_summary": "Orders to revenue",
                "authorization_summary": "Authorized for aggregate revenue",
            }
        )


def test_conversation_role_vocabulary_matches_the_owning_entry_contract() -> None:
    owning_roles = set(get_args(ConversationAuthorRole.__value__))
    projected_roles = set(get_args(contracts.ActorRole.__value__)) | {"pillarmesh"}
    assert owning_roles == projected_roles
    assert (
        contracts.ConversationMessageView.model_validate(
            {
                "message_id": "message-legacy",
                "author_label": "Recorded actor",
                "author_role": None,
                "body": "Legacy entry",
                "created_at": "2026-09-08T00:00:00Z",
            }
        ).author_role
        is None
    )


def test_conversation_commands_cannot_supply_a_separate_author_role() -> None:
    with pytest.raises(ValidationError):
        ConversationMessageCommand.model_validate(
            {
                "expected_revision": 1,
                "conversation_digest": "a" * 64,
                "active_role": "requester",
                "author_role": "data_owner",
                "body": "Claimed role",
            }
        )
