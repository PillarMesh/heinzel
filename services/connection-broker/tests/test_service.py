from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pillarmesh_connection_broker import (
    PrivateSourceCapability,
    SourceBindingBoundaryError,
    SourceBindingNotFoundError,
    SourceBindingService,
    SourceBindingValidationEvidence,
    SourceConnectionBinding,
    SourceConnectionBindingState,
    SQLiteSourceBindingRepository,
    StaleSourceBindingRevisionError,
)
from pillarmesh_contract_model import canonical_bytes

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


class Resolver:
    def resolve(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        provider_kind: str,
        connection_handle: str,
        account_mode: str,
        credential_revision: int,
    ) -> PrivateSourceCapability:
        return PrivateSourceCapability(
            tenant_id=tenant_id,
            binding_id=binding_id,
            provider_kind=provider_kind,
            connection_handle=connection_handle,
            account_mode=account_mode,
            credential_revision=credential_revision,
            endpoint_reference=f"endpoint-ref:{credential_revision:064x}",
            credential_reference=f"credential-ref:{credential_revision:064x}",
        )


class Probe:
    def validate(
        self,
        *,
        binding: SourceConnectionBinding,
        capability: PrivateSourceCapability,
        observed_at: datetime,
    ) -> SourceBindingValidationEvidence:
        return SourceBindingValidationEvidence(
            evidence_id=f"validation-{binding.revision}",
            tenant_id=binding.tenant_id,
            binding_id=binding.binding_id,
            binding_revision=binding.revision,
            credential_revision=capability.credential_revision,
            provider_kind=binding.provider_kind,
            positive_probe_succeeded=True,
            positive_probe_digest="1" * 64,
            denial_probe_succeeded=True,
            denial_probe_digest="2" * 64,
            source_observation_ref="source-observation-a",
            capability_profile_digest="3" * 64,
            observed_at=observed_at,
        )


class ForgedResolver(Resolver):
    def __init__(self, update: dict[str, object]) -> None:
        self._update = update

    def resolve(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        provider_kind: str,
        connection_handle: str,
        account_mode: str,
        credential_revision: int,
    ) -> PrivateSourceCapability:
        capability = super().resolve(
            tenant_id=tenant_id,
            binding_id=binding_id,
            provider_kind=provider_kind,
            connection_handle=connection_handle,
            account_mode=account_mode,
            credential_revision=credential_revision,
        )
        return capability.model_copy(update=self._update)


class ForgedProbe(Probe):
    def __init__(self, update: dict[str, object]) -> None:
        self._update = update

    def validate(
        self,
        *,
        binding: SourceConnectionBinding,
        capability: PrivateSourceCapability,
        observed_at: datetime,
    ) -> SourceBindingValidationEvidence:
        evidence = super().validate(
            binding=binding,
            capability=capability,
            observed_at=observed_at,
        )
        return evidence.model_copy(update=self._update)


class AuthorityInvalidator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def invalidate_source_binding_authority(
        self,
        tenant_id: str,
        source_binding_ref: str,
        *,
        invalidated_revision: int,
    ) -> None:
        self.calls.append((tenant_id, source_binding_ref, invalidated_revision))


def service() -> SourceBindingService:
    return SourceBindingService(
        SQLiteSourceBindingRepository(":memory:"),
        secret_resolver=Resolver(),
        capability_probes={"postgresql": Probe()},
        clock=lambda: NOW,
        authority_invalidator=AuthorityInvalidator(),
    )


def create(control: SourceBindingService) -> SourceConnectionBinding:
    return control.create_draft(
        tenant_id="tenant-a",
        provider_kind="postgresql",
        connection_handle="connection-handle-a",
        account_mode="not_applicable",
        approved_object_refs=("order",),
    )


def test_create_assigns_the_canonical_opaque_binding_identity() -> None:
    draft = create(service())

    assert draft.binding_id == "src-ab336d8316d875fcd8ba04ec"


def test_ready_requires_matching_positive_and_denied_probe_evidence() -> None:
    control = service()
    draft = create(control)
    validating = control.transition(
        "tenant-a",
        draft.binding_id,
        SourceConnectionBindingState.VALIDATING,
        expected_revision=1,
    )

    with pytest.raises(ValueError, match="validation evidence"):
        control.transition(
            "tenant-a",
            draft.binding_id,
            SourceConnectionBindingState.READY,
            expected_revision=validating.revision,
        )

    ready = control.validate(
        "tenant-a",
        draft.binding_id,
        expected_revision=validating.revision,
    )

    assert ready.lifecycle_state is SourceConnectionBindingState.READY
    assert ready.capability_profile_digest == "3" * 64
    assert ready.source_observation_ref == "source-observation-a"


def test_credential_rotation_preserves_immutable_identity_and_revalidates() -> None:
    control = service()
    draft = create(control)
    control.transition(
        "tenant-a", draft.binding_id, SourceConnectionBindingState.VALIDATING, expected_revision=1
    )
    ready = control.validate("tenant-a", draft.binding_id, expected_revision=2)

    rotated = control.rotate_credentials(
        "tenant-a", draft.binding_id, expected_revision=ready.revision
    )

    assert rotated.lifecycle_state is SourceConnectionBindingState.VALIDATING
    assert rotated.credential_revision == 2
    assert rotated.revision == ready.revision + 1
    assert rotated.capability_profile_digest is None
    assert rotated.source_observation_ref is None
    assert (
        rotated.tenant_id,
        rotated.provider_kind,
        rotated.connection_handle,
        rotated.account_mode,
    ) == (
        ready.tenant_id,
        ready.provider_kind,
        ready.connection_handle,
        ready.account_mode,
    )


def test_ready_binding_transition_invalidates_acquisition_authority_first() -> None:
    invalidator = AuthorityInvalidator()
    control = SourceBindingService(
        SQLiteSourceBindingRepository(":memory:"),
        secret_resolver=Resolver(),
        capability_probes={"postgresql": Probe()},
        clock=lambda: NOW,
        authority_invalidator=invalidator,
    )
    draft = create(control)
    control.transition(
        "tenant-a",
        draft.binding_id,
        SourceConnectionBindingState.VALIDATING,
        expected_revision=1,
    )
    ready = control.validate("tenant-a", draft.binding_id, expected_revision=2)

    suspended = control.transition(
        "tenant-a",
        draft.binding_id,
        SourceConnectionBindingState.SUSPENDED,
        expected_revision=ready.revision,
    )

    assert suspended.revision == ready.revision + 1
    assert invalidator.calls == [("tenant-a", draft.binding_id, ready.revision)]


def test_ready_binding_cannot_transition_without_acquisition_invalidator() -> None:
    control = SourceBindingService(
        SQLiteSourceBindingRepository(":memory:"),
        secret_resolver=Resolver(),
        capability_probes={"postgresql": Probe()},
        clock=lambda: NOW,
    )
    draft = create(control)
    control.transition(
        "tenant-a",
        draft.binding_id,
        SourceConnectionBindingState.VALIDATING,
        expected_revision=1,
    )
    ready = control.validate("tenant-a", draft.binding_id, expected_revision=2)

    with pytest.raises(SourceBindingBoundaryError, match="invalidate acquisition authority"):
        control.transition(
            "tenant-a",
            draft.binding_id,
            SourceConnectionBindingState.RETIRED,
            expected_revision=ready.revision,
        )

    assert control.get("tenant-a", draft.binding_id) == ready


def test_transition_table_and_retired_terminal_are_exact() -> None:
    control = service()
    draft = create(control)
    retired = control.transition(
        "tenant-a", draft.binding_id, SourceConnectionBindingState.RETIRED, expected_revision=1
    )

    with pytest.raises(ValueError, match="not allowed"):
        control.transition(
            "tenant-a",
            retired.binding_id,
            SourceConnectionBindingState.VALIDATING,
            expected_revision=retired.revision,
        )


def test_cross_tenant_and_stale_operations_disclose_no_binding() -> None:
    control = service()
    draft = create(control)

    with pytest.raises(SourceBindingNotFoundError, match="source binding not found"):
        control.transition(
            "tenant-b",
            draft.binding_id,
            SourceConnectionBindingState.VALIDATING,
            expected_revision=1,
        )
    with pytest.raises(StaleSourceBindingRevisionError, match="revision is stale"):
        control.transition(
            "tenant-a",
            draft.binding_id,
            SourceConnectionBindingState.VALIDATING,
            expected_revision=99,
        )


@pytest.mark.parametrize(
    "update",
    (
        {"tenant_id": "tenant-b"},
        {"binding_id": "source-binding-b"},
        {"provider_kind": "stripe"},
        {"connection_handle": "connection-handle-b"},
        {"account_mode": "live"},
        {"credential_revision": 2},
    ),
)
def test_create_revalidates_each_untrusted_private_capability_dimension(
    update: dict[str, object],
) -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    control = SourceBindingService(
        repository,
        secret_resolver=ForgedResolver(update),
        capability_probes={"postgresql": Probe()},
        clock=lambda: NOW,
    )

    with pytest.raises(SourceBindingBoundaryError, match="private capability"):
        create(control)


@pytest.mark.parametrize(
    "update",
    (
        {"tenant_id": "tenant-b"},
        {"binding_id": "source-binding-b"},
        {"binding_revision": 3},
        {"credential_revision": 2},
        {"provider_kind": "stripe"},
        {"positive_probe_succeeded": False},
        {"denial_probe_succeeded": False},
        {"observed_at": datetime(2026, 9, 1, 11, 59, 59, tzinfo=UTC)},
        {"observed_at": datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC)},
    ),
)
def test_validate_revalidates_each_untrusted_probe_evidence_dimension(
    update: dict[str, object],
) -> None:
    control = SourceBindingService(
        SQLiteSourceBindingRepository(":memory:"),
        secret_resolver=Resolver(),
        capability_probes={"postgresql": ForgedProbe(update)},
        clock=lambda: NOW,
    )
    draft = create(control)
    validating = control.transition(
        "tenant-a",
        draft.binding_id,
        SourceConnectionBindingState.VALIDATING,
        expected_revision=1,
    )

    with pytest.raises(SourceBindingBoundaryError, match="probe evidence"):
        control.validate(
            "tenant-a",
            draft.binding_id,
            expected_revision=validating.revision,
        )

    assert control.get("tenant-a", draft.binding_id) == validating


@pytest.mark.parametrize(
    "update",
    (
        {"provider_kind": "stripe"},
        {"connection_handle": "connection-handle-b"},
        {"account_mode": "live"},
    ),
)
def test_validate_rejects_each_persisted_capability_authority_mismatch(
    tmp_path: Path,
    update: dict[str, object],
) -> None:
    database_path = str(tmp_path / "capability-authority-corruption.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    control = SourceBindingService(
        repository,
        secret_resolver=Resolver(),
        capability_probes={"postgresql": Probe()},
        clock=lambda: NOW,
    )
    draft = create(control)
    validating = control.transition(
        "tenant-a",
        draft.binding_id,
        SourceConnectionBindingState.VALIDATING,
        expected_revision=1,
    )
    stored = repository.load_capability("tenant-a", draft.binding_id, 1)
    corrupted = stored.model_copy(update=update)
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE private_source_capabilities SET payload = ? "
        "WHERE tenant_id = ? AND binding_id = ? AND credential_revision = ?",
        (canonical_bytes(corrupted), "tenant-a", draft.binding_id, 1),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SourceBindingBoundaryError, match="private capability"):
        control.validate(
            "tenant-a",
            draft.binding_id,
            expected_revision=validating.revision,
        )

    assert control.get("tenant-a", draft.binding_id) == validating


class FailingResolver(Resolver):
    def resolve(self, **_arguments: object) -> PrivateSourceCapability:
        raise RuntimeError("postgresql://user:secret-canary@database/source")


class FailingProbe(Probe):
    def validate(self, **_arguments: object) -> SourceBindingValidationEvidence:
        raise RuntimeError("sk_live_secret_canary")


def test_private_boundary_failures_are_typed_and_sanitized() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    resolver_control = SourceBindingService(
        repository,
        secret_resolver=FailingResolver(),
        capability_probes={"postgresql": Probe()},
        clock=lambda: NOW,
    )

    with pytest.raises(SourceBindingBoundaryError) as resolver_failure:
        create(resolver_control)

    assert "secret-canary" not in str(resolver_failure.value)
    assert resolver_failure.value.__cause__ is None

    probe_control = SourceBindingService(
        SQLiteSourceBindingRepository(":memory:"),
        secret_resolver=Resolver(),
        capability_probes={"postgresql": FailingProbe()},
        clock=lambda: NOW,
    )
    draft = create(probe_control)
    validating = probe_control.transition(
        "tenant-a",
        draft.binding_id,
        SourceConnectionBindingState.VALIDATING,
        expected_revision=1,
    )

    with pytest.raises(SourceBindingBoundaryError) as probe_failure:
        probe_control.validate(
            "tenant-a",
            draft.binding_id,
            expected_revision=validating.revision,
        )

    assert "secret_canary" not in str(probe_failure.value)
    assert probe_failure.value.__cause__ is None
