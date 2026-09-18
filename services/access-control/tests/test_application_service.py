from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from heinzel_access_control import (
    AccessGrant,
    AccessGrantAdmissionAuthorityInvalid,
    AccessGrantApplicationService,
    AccessGrantDenied,
    AccessGrantStaleRevision,
    AdmittedAccessProposal,
    CurrentEntitlementSnapshot,
    SQLiteAccessGrantRepository,
)
from heinzel_contract_model import ArtifactReference, digest
from heinzel_provider_sdk import (
    AccessEffectCommand,
    AccessEffectFailure,
    AccessEffectProvider,
    AccessEffectProviderError,
    AccessEffectResult,
    AccessEffectSurface,
)

NOW = datetime(2026, 9, 12, 21, tzinfo=UTC)
PRODUCT = ArtifactReference(artifact_id="product:revenue", version=1, digest="a" * 64)
ADMISSION = ArtifactReference(artifact_id="admission:access-1", version=1, digest="b" * 64)
PURPOSE = "Review regional revenue"


def _entitlement(**changes: object) -> CurrentEntitlementSnapshot:
    values: dict[str, object] = {
        "snapshot_id": "entitlement-snapshot-1",
        "snapshot_digest": "pending",
        "tenant_id": "tenant-a",
        "principal_ref": "principal:requester-a",
        "purpose_digest": digest(PURPOSE),
        "connected_authority_ref": "authority:policy-a",
        "source_revision": 4,
        "source_payload_digest": "c" * 64,
        "observation_id": "observation-4",
        "product_version_refs": (PRODUCT,),
        "semantic_refs": (PRODUCT,),
        "filter_domains": (),
        "permissions": ("dashboard", "download", "query", "view"),
        "effective_at": NOW - timedelta(hours=1),
        "valid_until": NOW + timedelta(hours=1),
        "resolved_at": NOW,
    }
    values.update(changes)
    values["snapshot_digest"] = digest(
        {
            "schema_version": "1",
            "tenant_id": values["tenant_id"],
            "principal_ref": values["principal_ref"],
            "purpose_digest": values["purpose_digest"],
            "connected_authority_ref": values["connected_authority_ref"],
            "source_revision": values["source_revision"],
            "source_payload_digest": values["source_payload_digest"],
            "product_version_refs": values["product_version_refs"],
            "semantic_refs": values["semantic_refs"],
            "filter_domains": values["filter_domains"],
            "permissions": values["permissions"],
            "effective_at": values["effective_at"],
            "valid_until": values["valid_until"],
        }
    )
    return CurrentEntitlementSnapshot.model_validate(values)


def _proposal(**changes: object) -> AdmittedAccessProposal:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "request_id": "request-1",
        "proposal_id": "proposal-1",
        "proposal_revision": 2,
        "admission_receipt_ref": ADMISSION,
        "entitlement_snapshot_digest": _entitlement().snapshot_digest,
        "principal_ref": "principal:requester-a",
        "purpose": PURPOSE,
        "data_product_version_ref": PRODUCT,
        "fields": ("region", "revenue"),
        "classification_refs": (),
        "access_mode": "dashboard",
        "permissions": ("dashboard", "download", "view"),
        "effective_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(minutes=30),
        "policy_revision": 3,
        "targets": (
            {"surface": "result", "provider_resource_ref": "result:answer-1"},
            {"surface": "superset", "provider_resource_ref": "dashboard:revenue"},
            {"surface": "warehouse", "provider_resource_ref": "relation:revenue"},
        ),
    }
    values.update(changes)
    return AdmittedAccessProposal.model_validate(values)


class _Proposals:
    def __init__(self, proposal: AdmittedAccessProposal | None) -> None:
        self.proposal = proposal

    def read_admitted(self, *, tenant_id: str, request_id: str) -> AdmittedAccessProposal | None:
        assert (tenant_id, request_id) == ("tenant-a", "request-1")
        return self.proposal


class _InvalidProposalAuthority:
    def read_admitted(self, *, tenant_id: str, request_id: str) -> AdmittedAccessProposal | None:
        raise AccessGrantAdmissionAuthorityInvalid("private stored payload detail")


class _Entitlements:
    def __init__(self, snapshot: CurrentEntitlementSnapshot) -> None:
        self.snapshot = snapshot

    def resolve_current(
        self, *, tenant_id: str, principal_ref: str, purpose_digest: str
    ) -> CurrentEntitlementSnapshot:
        assert tenant_id == self.snapshot.tenant_id
        assert principal_ref == self.snapshot.principal_ref
        assert purpose_digest == self.snapshot.purpose_digest
        return self.snapshot


class _RevocationAuthority:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def may_revoke(self, *, tenant_id: str, request_id: str, actor_id: str) -> bool:
        return self.allowed and (tenant_id, request_id, actor_id) == (
            "tenant-a",
            "request-1",
            "actor-requester-a",
        )


_DEFAULT_REVOCATION_AUTHORITY = _RevocationAuthority()


class _Provider:
    def __init__(
        self,
        surface: AccessEffectSurface,
        *outcomes: AccessEffectFailure | Literal["succeeded"],
        observed_grants: list[AccessGrant | None] | None = None,
        repository: SQLiteAccessGrantRepository | None = None,
        events: list[tuple[str, str]] | None = None,
    ) -> None:
        self.surface = surface
        self._outcomes = list(outcomes)
        self.commands: list[AccessEffectCommand] = []
        self._observed_grants = observed_grants
        self._repository = repository
        self._events = events

    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        self.commands.append(command)
        if self._events is not None:
            self._events.append((self.surface, command.action))
        if self._observed_grants is not None and self._repository is not None:
            self._observed_grants.append(
                self._repository.load_current(command.tenant_id, command.grant_id)
            )
        outcome = self._outcomes.pop(0) if self._outcomes else "succeeded"
        if outcome != "succeeded":
            raise AccessEffectProviderError(
                outcome=outcome,
                provider_receipt_digest=digest(
                    {"surface": self.surface, "outcome": outcome, "attempt": len(self.commands)}
                ),
            )
        return AccessEffectResult(
            surface=command.surface,
            action=command.action,
            idempotency_key=command.idempotency_key,
            provider_receipt_digest=digest({"surface": self.surface, "action": command.action}),
        )


def _service(
    repository: SQLiteAccessGrantRepository,
    providers: tuple[AccessEffectProvider, ...],
    *,
    proposal: AdmittedAccessProposal | None = None,
    entitlement: CurrentEntitlementSnapshot | None = None,
    now: datetime = NOW,
    revocation_authority: _RevocationAuthority | None = _DEFAULT_REVOCATION_AUTHORITY,
    clock: Callable[[], datetime] | None = None,
) -> AccessGrantApplicationService:
    return AccessGrantApplicationService(
        grants=repository,
        admitted_proposals=_Proposals(proposal or _proposal()),
        entitlements=_Entitlements(entitlement or _entitlement()),
        providers=providers,
        revocation_authority=revocation_authority,
        clock=clock or (lambda: now),
    )


def test_grant_is_persisted_pending_before_any_provider_effect() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    observed: list[AccessGrant | None] = []
    providers = (
        _Provider("result", observed_grants=observed, repository=repository),
        _Provider("superset", observed_grants=observed, repository=repository),
        _Provider("warehouse", observed_grants=observed, repository=repository),
    )

    grant = _service(repository, providers).apply(
        tenant_id="tenant-a", request_id="request-1", grant_id="grant-1"
    )

    assert grant.state == "active"
    assert all(item is not None and item.state == "pending" for item in observed)
    assert repository.successful_effect_surfaces("tenant-a", "grant-1", 1, action="apply") == (
        "result",
        "superset",
        "warehouse",
    )
    commands = {provider.surface: provider.commands[0] for provider in providers}
    assert commands["result"].permissions == ("download", "view")
    assert commands["superset"].permissions == ("dashboard", "view")
    assert commands["warehouse"].permissions == ("dashboard", "download", "view")


def test_invalid_request_admission_is_denied_without_leaking_authority_detail() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    service = AccessGrantApplicationService(
        grants=repository,
        admitted_proposals=_InvalidProposalAuthority(),
        entitlements=_Entitlements(_entitlement()),
        providers=(_Provider("result"),),
        clock=lambda: NOW,
    )

    with pytest.raises(AccessGrantDenied) as captured:
        service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")

    assert "private stored payload detail" not in str(captured.value)
    assert repository.load_current("tenant-a", "grant-1") is None


def test_partial_apply_retries_only_transient_or_ambiguous_surfaces() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    result = _Provider("result")
    superset = _Provider("superset", "ambiguous_outcome", "succeeded")
    warehouse = _Provider("warehouse", "transient_failure", "succeeded")
    service = _service(repository, (result, superset, warehouse))

    first = service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")
    second = service.reconcile(tenant_id="tenant-a", grant_id="grant-1")

    assert first.state == "pending"
    assert second.state == "active"
    assert len(result.commands) == 1
    assert len(superset.commands) == len(warehouse.commands) == 2
    assert superset.commands[0].idempotency_key == superset.commands[1].idempotency_key


def test_permanent_apply_failure_is_not_retried() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    result = _Provider("result")
    superset = _Provider("superset", "permanent_failure")
    warehouse = _Provider("warehouse")
    service = _service(repository, (result, superset, warehouse))

    first = service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")
    second = service.reconcile(tenant_id="tenant-a", grant_id="grant-1")

    assert first.state == second.state == "failed"
    assert sum(command.action == "apply" for command in superset.commands) == 1
    assert all(
        sum(command.action == "revoke" for command in provider.commands) == 1
        for provider in (result, superset, warehouse)
    )
    assert tuple(len(provider.commands) for provider in (result, superset, warehouse)) == (2, 2, 2)


def test_superseded_entitlement_denies_without_provider_effects() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    provider = _Provider("result")
    changed = _entitlement(source_revision=5, source_payload_digest="d" * 64)

    with pytest.raises(AccessGrantDenied, match="proposal is outside current entitlement"):
        _service(repository, (provider,), entitlement=changed).apply(
            tenant_id="tenant-a", request_id="request-1", grant_id="grant-1"
        )

    assert repository.load_current("tenant-a", "grant-1") is None
    assert provider.commands == []


@pytest.mark.parametrize(
    ("entitlement_changes", "proposal_changes", "now"),
    (
        (
            {
                "product_version_refs": (
                    ArtifactReference(artifact_id="other", version=1, digest="e" * 64),
                )
            },
            {},
            NOW,
        ),
        ({"permissions": ("dashboard", "view")}, {}, NOW),
        ({"effective_at": NOW}, {}, NOW),
        ({"valid_until": NOW + timedelta(minutes=10)}, {}, NOW),
        ({"valid_until": NOW}, {"expires_at": NOW}, NOW),
    ),
)
def test_approval_never_substitutes_for_current_entitlement_scope(
    entitlement_changes: dict[str, object],
    proposal_changes: dict[str, object],
    now: datetime,
) -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    provider = _Provider("result")
    entitlement = _entitlement(**entitlement_changes)
    proposal = _proposal(
        access_mode="query",
        permissions=("query", "view"),
        targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
        entitlement_snapshot_digest=entitlement.snapshot_digest,
        **proposal_changes,
    )

    with pytest.raises(AccessGrantDenied, match="outside current entitlement"):
        _service(
            repository,
            (provider,),
            proposal=proposal,
            entitlement=entitlement,
            now=now,
        ).apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")

    assert repository.load_current("tenant-a", "grant-1") is None
    assert provider.commands == []


def test_equal_authority_and_proposal_time_boundaries_are_admitted() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    entitlement = _entitlement(effective_at=NOW, valid_until=NOW + timedelta(minutes=30))
    proposal = _proposal(
        access_mode="query",
        permissions=("query", "view"),
        effective_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
        targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
        entitlement_snapshot_digest=entitlement.snapshot_digest,
    )

    grant = _service(
        repository,
        (_Provider("result"),),
        proposal=proposal,
        entitlement=entitlement,
        now=NOW,
    ).apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")

    assert grant.state == "active"


def test_superset_revoke_failure_after_warehouse_success_retries_only_superset() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    events: list[tuple[str, str]] = []
    result = _Provider("result", events=events)
    superset = _Provider("superset", "succeeded", "transient_failure", "succeeded", events=events)
    warehouse = _Provider("warehouse", events=events)
    service = _service(repository, (result, superset, warehouse))
    service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")

    pending = service.revoke(tenant_id="tenant-a", grant_id="grant-1")
    with pytest.raises(AccessGrantDenied, match="not active"):
        service.authorize(
            tenant_id="tenant-a",
            grant_id="grant-1",
            principal_ref="principal:requester-a",
            purpose=PURPOSE,
            permission="view",
            product_version_ref=PRODUCT,
        )
    revoked = service.reconcile(tenant_id="tenant-a", grant_id="grant-1")

    assert pending.state == "revocation_pending"
    assert revoked.state == "revoked"
    revoke_counts = {
        provider.surface: sum(command.action == "revoke" for command in provider.commands)
        for provider in (result, superset, warehouse)
    }
    assert revoke_counts == {"result": 1, "superset": 2, "warehouse": 1}
    assert [event for event in events if event[1] == "revoke"][:3] == [
        ("warehouse", "revoke"),
        ("result", "revoke"),
        ("superset", "revoke"),
    ]


def test_permanent_revoke_failure_is_terminal_and_never_retried() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    result = _Provider("result")
    superset = _Provider("superset", "succeeded", "permanent_failure")
    warehouse = _Provider("warehouse")
    service = _service(repository, (result, superset, warehouse))
    service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")

    failed = service.revoke(tenant_id="tenant-a", grant_id="grant-1")
    replay = service.reconcile(tenant_id="tenant-a", grant_id="grant-1")

    assert failed == replay
    assert failed.state == "failed"
    assert failed.failed_action == "revoke"
    assert sum(command.action == "revoke" for command in superset.commands) == 1


def test_manual_revocation_records_owner_reason_before_provider_cleanup() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    observed: list[AccessGrant | None] = []
    provider = _Provider("result", observed_grants=observed, repository=repository)
    moments = iter(NOW + timedelta(seconds=offset) for offset in range(10))
    service = _service(
        repository,
        (provider,),
        proposal=_proposal(
            access_mode="query",
            permissions=("query", "view"),
            targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
        ),
        clock=lambda: next(moments),
    )
    active = service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")

    revoked = service.revoke_for_request(
        tenant_id="tenant-a",
        request_id="request-1",
        actor_id="actor-requester-a",
        expected_revision=active.revision,
        reason="The analysis is complete.",
    )

    assert revoked.state == "revoked"
    assert revoked.manual_revocation is not None
    assert revoked.manual_revocation.actor_id == "actor-requester-a"
    assert revoked.manual_revocation.reason == "The analysis is complete."
    assert revoked.manual_revocation.base_revision == active.revision
    assert observed[-1] is not None
    assert observed[-1].state == "revocation_pending"
    assert observed[-1].manual_revocation == revoked.manual_revocation
    assert observed[-1].updated_at > active.updated_at


def test_manual_revocation_replay_retries_only_incomplete_cleanup() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    result = _Provider("result")
    warehouse = _Provider("warehouse", "succeeded", "transient_failure", "succeeded")
    proposal = _proposal(
        access_mode="query",
        permissions=("query", "view"),
        targets=(
            {"surface": "result", "provider_resource_ref": "result:answer-1"},
            {"surface": "warehouse", "provider_resource_ref": "relation:revenue"},
        ),
    )
    service = _service(repository, (result, warehouse), proposal=proposal)
    active = service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")
    pending = service.revoke_for_request(
        tenant_id="tenant-a",
        request_id="request-1",
        actor_id="actor-requester-a",
        expected_revision=active.revision,
        reason="The analysis is complete.",
    )
    with pytest.raises(AccessGrantStaleRevision, match="stale"):
        service.revoke_for_request(
            tenant_id="tenant-a",
            request_id="request-1",
            actor_id="actor-requester-a",
            expected_revision=active.revision,
            reason="A different replay reason.",
        )
    revoked = service.revoke_for_request(
        tenant_id="tenant-a",
        request_id="request-1",
        actor_id="actor-requester-a",
        expected_revision=active.revision,
        reason="The analysis is complete.",
    )

    assert pending.state == "revocation_pending"
    assert revoked.state == "revoked"
    assert sum(item.action == "revoke" for item in result.commands) == 1
    assert sum(item.action == "revoke" for item in warehouse.commands) == 2


@pytest.mark.parametrize("reason", ("   ", "x" * 513))
def test_manual_revocation_rejects_invalid_reason_boundaries(reason: str) -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    provider = _Provider("result")
    service = _service(
        repository,
        (provider,),
        proposal=_proposal(
            access_mode="query",
            permissions=("query", "view"),
            targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
        ),
    )
    active = service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")

    with pytest.raises(AccessGrantDenied, match="reason is invalid"):
        service.revoke_for_request(
            tenant_id="tenant-a",
            request_id="request-1",
            actor_id="actor-requester-a",
            expected_revision=active.revision,
            reason=reason,
        )

    assert sum(item.action == "revoke" for item in provider.commands) == 0


def test_manual_revocation_accepts_a_512_character_reason() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    provider = _Provider("result")
    service = _service(
        repository,
        (provider,),
        proposal=_proposal(
            access_mode="query",
            permissions=("query", "view"),
            targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
        ),
    )
    active = service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")

    revoked = service.revoke_for_request(
        tenant_id="tenant-a",
        request_id="request-1",
        actor_id="actor-requester-a",
        expected_revision=active.revision,
        reason="x" * 512,
    )

    assert revoked.state == "revoked"


def test_manual_revocation_requires_configured_authority_and_an_owned_grant() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    provider = _Provider("result")
    proposal = _proposal(
        access_mode="query",
        permissions=("query", "view"),
        targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
    )
    service = _service(repository, (provider,), proposal=proposal, revocation_authority=None)
    active = service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")

    with pytest.raises(AccessGrantDenied, match="unavailable"):
        service.revoke_for_request(
            tenant_id="tenant-a",
            request_id="request-1",
            actor_id="actor-requester-a",
            expected_revision=active.revision,
            reason="The analysis is complete.",
        )

    empty_repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    missing = _service(empty_repository, (provider,), proposal=proposal)
    with pytest.raises(AccessGrantDenied, match="unavailable"):
        missing.revoke_for_request(
            tenant_id="tenant-a",
            request_id="request-1",
            actor_id="actor-requester-a",
            expected_revision=active.revision,
            reason="The analysis is complete.",
        )


def test_manual_revocation_rejects_stale_or_unauthorized_commands_without_effects() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    provider = _Provider("result")
    proposal = _proposal(
        access_mode="query",
        permissions=("query", "view"),
        targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
    )
    service = _service(repository, (provider,), proposal=proposal)
    active = service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")

    with pytest.raises(AccessGrantStaleRevision, match="stale"):
        service.revoke_for_request(
            tenant_id="tenant-a",
            request_id="request-1",
            actor_id="actor-requester-a",
            expected_revision=active.revision - 1,
            reason="The analysis is complete.",
        )
    with pytest.raises(AccessGrantDenied, match="unavailable"):
        service.revoke_for_request(
            tenant_id="tenant-b",
            request_id="request-1",
            actor_id="actor-requester-a",
            expected_revision=active.revision,
            reason="The analysis is complete.",
        )
    with pytest.raises(AccessGrantDenied, match="unavailable"):
        service.revoke_for_request(
            tenant_id="tenant-a",
            request_id="request-1",
            actor_id="actor-other",
            expected_revision=active.revision,
            reason="The analysis is complete.",
        )

    assert sum(item.action == "revoke" for item in provider.commands) == 0


def test_expiry_clock_boundary_persists_denial_before_cleanup() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    observed: list[AccessGrant | None] = []
    provider = _Provider("result", observed_grants=observed, repository=repository)
    proposal = _proposal(
        access_mode="query",
        permissions=("query", "view"),
        expires_at=NOW,
        targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
    )
    service = _service(repository, (provider,), proposal=proposal, now=NOW - timedelta(seconds=1))
    active = service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")
    service = _service(repository, (provider,), proposal=proposal, now=NOW)

    expired = service.reconcile(tenant_id="tenant-a", grant_id="grant-1")

    assert active.state == "active"
    assert expired.state == "revoked"
    assert provider.commands[-1].action == "revoke"
    assert observed[-1] is not None and observed[-1].state == "expired"


def test_exact_apply_command_replay_returns_existing_grant_without_new_effects() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    provider = _Provider("result")
    proposal = _proposal(
        access_mode="query",
        permissions=("query", "view"),
        targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
    )
    service = _service(repository, (provider,), proposal=proposal)

    first = service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")
    replay = service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")

    assert replay == first
    assert len(provider.commands) == 1


@pytest.mark.parametrize(
    "proposal_changes",
    (
        {
            "admission_receipt_ref": ArtifactReference(
                artifact_id="admission:other", version=1, digest="d" * 64
            )
        },
        {"policy_revision": 4},
        {"fields": ("region",)},
        {"targets": ({"surface": "result", "provider_resource_ref": "result:other"},)},
    ),
)
def test_replayed_grant_id_rejects_changed_admitted_identity(
    proposal_changes: dict[str, object],
) -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    provider = _Provider("result")
    proposal = _proposal(
        access_mode="query",
        permissions=("query", "view"),
        targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
    )
    _service(repository, (provider,), proposal=proposal).apply(
        tenant_id="tenant-a", request_id="request-1", grant_id="grant-1"
    )
    changed = proposal.model_copy(update=proposal_changes)

    with pytest.raises(AccessGrantDenied, match="identity conflicts"):
        _service(repository, (provider,), proposal=changed).apply(
            tenant_id="tenant-a", request_id="request-1", grant_id="grant-1"
        )

    assert len(provider.commands) == 1


def test_reconciling_revoked_grant_is_an_effect_free_replay() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    provider = _Provider("result")
    proposal = _proposal(
        access_mode="query",
        permissions=("query", "view"),
        targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
    )
    service = _service(repository, (provider,), proposal=proposal)
    service.apply(tenant_id="tenant-a", request_id="request-1", grant_id="grant-1")
    revoked = service.revoke(tenant_id="tenant-a", grant_id="grant-1")

    replay = service.reconcile(tenant_id="tenant-a", grant_id="grant-1")

    assert replay == revoked
    assert [command.action for command in provider.commands] == ["apply", "revoke"]


def test_loaded_grant_with_missing_provider_fails_closed_without_key_error() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    service = _service(repository, (_Provider("result"),))
    repository.append(
        AccessGrant(
            grant_id="grant-1",
            tenant_id="tenant-a",
            request_id="request-1",
            revision=1,
            state="pending",
            principal_ref="principal:requester-a",
            purpose=PURPOSE,
            purpose_digest=digest(PURPOSE),
            data_product_version_ref=PRODUCT,
            fields=("region",),
            classification_refs=(),
            access_mode="dashboard",
            permissions=("dashboard", "view"),
            effective_at=NOW - timedelta(minutes=5),
            expires_at=NOW + timedelta(minutes=30),
            policy_revision=3,
            admission_receipt_ref=ADMISSION,
            entitlement_snapshot_digest=_entitlement().snapshot_digest,
            effect_targets=_proposal().targets,
            created_at=NOW,
            updated_at=NOW,
        ),
        expected_current_revision=0,
    )

    with pytest.raises(AccessGrantDenied, match="provider is unavailable"):
        service.reconcile(tenant_id="tenant-a", grant_id="grant-1")


class _MismatchedProvider(_Provider):
    def enact(self, command: AccessEffectCommand) -> AccessEffectResult:
        self.commands.append(command)
        return AccessEffectResult(
            surface="superset",
            action=command.action,
            idempotency_key=command.idempotency_key,
            provider_receipt_digest="f" * 64,
        )


def test_mismatched_provider_result_is_recorded_as_permanent_and_cleaned_up() -> None:
    repository = SQLiteAccessGrantRepository(sqlite3.connect(":memory:"))
    provider = _MismatchedProvider("result")
    proposal = _proposal(
        access_mode="query",
        permissions=("query", "view"),
        targets=({"surface": "result", "provider_resource_ref": "result:answer-1"},),
    )

    grant = _service(repository, (provider,), proposal=proposal).apply(
        tenant_id="tenant-a", request_id="request-1", grant_id="grant-1"
    )

    assert grant.state == "failed"
    assert repository.effect_receipts("tenant-a", "grant-1", 1, action="apply")[0].outcome == (
        "permanent_failure"
    )
    assert [command.action for command in provider.commands] == ["apply", "revoke"]
