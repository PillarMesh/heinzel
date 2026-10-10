from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from heinzel_connection_broker import (
    PrivateSourceCapability,
    SourceBindingConflictError,
    SourceBindingIntegrityError,
    SourceBindingNotFoundError,
    SourceBindingPersistenceError,
    SourceBindingValidationEvidence,
    SourceConnectionBinding,
    SourceConnectionBindingState,
    SQLiteSourceBindingRepository,
    StaleSourceBindingRevisionError,
)
from heinzel_contract_model import canonical_bytes

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def binding(
    *, revision: int = 1, state: SourceConnectionBindingState | None = None
) -> SourceConnectionBinding:
    return SourceConnectionBinding(
        binding_id="source-binding-a",
        tenant_id="tenant-a",
        provider_kind="postgresql",
        connection_handle="connection-handle-a",
        account_mode="not_applicable",
        lifecycle_state=state or SourceConnectionBindingState.DRAFT,
        approved_object_refs=("order",),
        capability_profile_digest=None,
        source_observation_ref=None,
        credential_revision=1,
        revision=revision,
        created_at=NOW,
        updated_at=NOW,
    )


def capability() -> PrivateSourceCapability:
    return PrivateSourceCapability(
        tenant_id="tenant-a",
        binding_id="source-binding-a",
        provider_kind="postgresql",
        connection_handle="connection-handle-a",
        account_mode="not_applicable",
        credential_revision=1,
        endpoint_reference=f"endpoint-ref:{'a' * 64}",
        credential_reference=f"credential-ref:{'b' * 64}",
    )


def validation_evidence(
    update: dict[str, object] | None = None,
) -> SourceBindingValidationEvidence:
    evidence = SourceBindingValidationEvidence(
        evidence_id="source-validation-a",
        tenant_id="tenant-a",
        binding_id="source-binding-a",
        binding_revision=2,
        credential_revision=1,
        provider_kind="postgresql",
        positive_probe_succeeded=True,
        positive_probe_digest="1" * 64,
        denial_probe_succeeded=True,
        denial_probe_digest="2" * 64,
        source_observation_ref="source-observation-a",
        capability_profile_digest="3" * 64,
        observed_at=NOW,
    )
    return evidence.model_copy(update=update or {})


def test_repository_creates_public_binding_and_private_capability_atomically() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")

    repository.create(binding(), capability())

    assert repository.load("tenant-a", "source-binding-a") == binding()
    assert repository.load_capability("tenant-a", "source-binding-a", 1) == capability()


@pytest.mark.parametrize(
    ("binding_update", "capability_update"),
    (
        ({"revision": 2}, {}),
        ({"credential_revision": 2}, {"credential_revision": 2}),
    ),
)
def test_repository_requires_each_initial_revision_to_be_one(
    binding_update: dict[str, object],
    capability_update: dict[str, object],
) -> None:
    repository = SQLiteSourceBindingRepository(":memory:")

    with pytest.raises(ValueError, match="revision one"):
        repository.create(
            binding().model_copy(update=binding_update),
            capability().model_copy(update=capability_update),
        )

    with pytest.raises(SourceBindingNotFoundError):
        repository.load("tenant-a", "source-binding-a")


def test_cross_tenant_and_missing_reads_have_the_same_generic_error() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())

    for tenant_id, binding_id in (
        ("tenant-b", "source-binding-a"),
        ("tenant-a", "missing-binding"),
    ):
        with pytest.raises(SourceBindingNotFoundError) as captured:
            repository.load(tenant_id, binding_id)
        assert str(captured.value) == "source binding not found"


def test_stale_append_discloses_no_new_revision() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())
    validating = binding(revision=2, state=SourceConnectionBindingState.VALIDATING)

    with pytest.raises(StaleSourceBindingRevisionError):
        repository.append(validating, expected_revision=0)

    assert repository.load("tenant-a", "source-binding-a") == binding()


def test_validation_binding_and_evidence_commit_in_one_transaction() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())
    validating = binding(revision=2, state=SourceConnectionBindingState.VALIDATING)
    repository.append(validating, expected_revision=1)
    evidence = SourceBindingValidationEvidence(
        evidence_id="source-validation-a",
        tenant_id="tenant-a",
        binding_id="source-binding-a",
        binding_revision=2,
        credential_revision=1,
        provider_kind="postgresql",
        positive_probe_succeeded=True,
        positive_probe_digest="1" * 64,
        denial_probe_succeeded=True,
        denial_probe_digest="2" * 64,
        source_observation_ref="source-observation-a",
        capability_profile_digest="3" * 64,
        observed_at=NOW,
    )
    ready = validating.model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.READY,
            "capability_profile_digest": evidence.capability_profile_digest,
            "source_observation_ref": evidence.source_observation_ref,
            "revision": 3,
        }
    )

    repository.record_validation(ready, evidence, expected_revision=2)

    assert repository.load("tenant-a", "source-binding-a") == ready
    assert repository.load_validation("tenant-a", "source-binding-a", 2) == evidence


def test_repository_rejects_tampered_schema_metadata(tmp_path: Path) -> None:
    database_path = str(tmp_path / "source-bindings.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    repository.close()
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE source_binding_schema_metadata SET checksum = 'tampered' WHERE singleton = 1"
    )
    connection.commit()
    connection.close()

    with pytest.raises(SourceBindingPersistenceError, match="checksum"):
        SQLiteSourceBindingRepository(database_path)


def test_repository_reopens_an_untampered_schema(tmp_path: Path) -> None:
    database_path = str(tmp_path / "source-bindings.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    repository.create(binding(), capability())
    repository.close()

    reopened = SQLiteSourceBindingRepository(database_path)

    assert reopened.load("tenant-a", "source-binding-a") == binding()


def test_repository_does_not_leak_sqlite_errors_after_close() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.close()

    with pytest.raises(SourceBindingPersistenceError, match="load source binding") as captured:
        repository.load("tenant-a", "source-binding-a")

    assert not isinstance(captured.value.__cause__, type(None))


def test_append_cannot_bypass_capability_rotation_or_validation_evidence() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())
    validating = binding(revision=2, state=SourceConnectionBindingState.VALIDATING)
    repository.append(validating, expected_revision=1)
    credential_bypass = validating.model_copy(update={"credential_revision": 2, "revision": 3})
    ready_bypass = validating.model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.READY,
            "capability_profile_digest": "3" * 64,
            "source_observation_ref": "source-observation-a",
            "revision": 3,
        }
    )

    for candidate in (credential_bypass, ready_bypass):
        with pytest.raises((StaleSourceBindingRevisionError, SourceBindingConflictError)):
            repository.append(candidate, expected_revision=2)

    assert repository.load("tenant-a", "source-binding-a") == validating


def test_validation_authority_mismatch_rolls_back_ready_revision() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())
    validating = binding(revision=2, state=SourceConnectionBindingState.VALIDATING)
    repository.append(validating, expected_revision=1)
    evidence = SourceBindingValidationEvidence(
        evidence_id="source-validation-a",
        tenant_id="tenant-a",
        binding_id="source-binding-a",
        binding_revision=2,
        credential_revision=1,
        provider_kind="postgresql",
        positive_probe_succeeded=True,
        positive_probe_digest="1" * 64,
        denial_probe_succeeded=True,
        denial_probe_digest="2" * 64,
        source_observation_ref="source-observation-a",
        capability_profile_digest="3" * 64,
        observed_at=NOW,
    )
    mismatched_ready = validating.model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.READY,
            "capability_profile_digest": "4" * 64,
            "source_observation_ref": evidence.source_observation_ref,
            "revision": 3,
        }
    )

    with pytest.raises(SourceBindingConflictError, match="authority"):
        repository.record_validation(mismatched_ready, evidence, expected_revision=2)

    assert repository.load("tenant-a", "source-binding-a") == validating


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
def test_create_rejects_each_private_capability_identity_mismatch(
    update: dict[str, object],
) -> None:
    repository = SQLiteSourceBindingRepository(":memory:")

    with pytest.raises(SourceBindingConflictError, match="capability"):
        repository.create(binding(), capability().model_copy(update=update))

    with pytest.raises(SourceBindingNotFoundError):
        repository.load("tenant-a", "source-binding-a")


@pytest.mark.parametrize(
    "update",
    (
        {"provider_kind": "stripe"},
        {"connection_handle": "connection-handle-b"},
        {"account_mode": "live"},
        {"approved_object_refs": ("invoice",)},
        {"created_at": datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC)},
        {"revision": 3},
    ),
)
def test_append_rejects_each_immutable_identity_or_revision_change(
    update: dict[str, object],
) -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())
    candidate = binding(
        revision=2,
        state=SourceConnectionBindingState.VALIDATING,
    ).model_copy(update=update)

    with pytest.raises(StaleSourceBindingRevisionError):
        repository.append(candidate, expected_revision=1)

    assert repository.load("tenant-a", "source-binding-a") == binding()


@pytest.mark.parametrize(
    "evidence_update",
    (
        {"tenant_id": "tenant-b"},
        {"binding_id": "source-binding-b"},
        {"binding_revision": 3},
        {"credential_revision": 2},
        {"provider_kind": "stripe"},
    ),
)
def test_validation_rejects_each_evidence_identity_mismatch(
    evidence_update: dict[str, object],
) -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())
    validating = binding(revision=2, state=SourceConnectionBindingState.VALIDATING)
    repository.append(validating, expected_revision=1)
    evidence = validation_evidence(evidence_update)
    ready = validating.model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.READY,
            "capability_profile_digest": evidence.capability_profile_digest,
            "source_observation_ref": evidence.source_observation_ref,
            "revision": 3,
        }
    )

    with pytest.raises(
        SourceBindingConflictError,
        match="source validation evidence does not match binding",
    ):
        repository.record_validation(ready, evidence, expected_revision=2)

    assert repository.load("tenant-a", "source-binding-a") == validating


@pytest.mark.parametrize(
    ("binding_update", "evidence_update"),
    (
        ({"lifecycle_state": SourceConnectionBindingState.VALIDATING}, {}),
        ({"credential_revision": 2}, {}),
        ({"capability_profile_digest": "4" * 64}, {}),
        ({"source_observation_ref": "source-observation-b"}, {}),
        (
            {"updated_at": datetime(2026, 9, 1, 11, 59, 59, tzinfo=UTC)},
            {"observed_at": datetime(2026, 9, 1, 11, 59, 59, tzinfo=UTC)},
        ),
        ({"updated_at": datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC)}, {}),
    ),
)
def test_validation_rejects_each_ready_authority_mismatch(
    binding_update: dict[str, object],
    evidence_update: dict[str, object],
) -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())
    validating = binding(revision=2, state=SourceConnectionBindingState.VALIDATING)
    repository.append(validating, expected_revision=1)
    evidence = validation_evidence(evidence_update)
    ready = validating.model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.READY,
            "capability_profile_digest": evidence.capability_profile_digest,
            "source_observation_ref": evidence.source_observation_ref,
            "revision": 3,
        }
        | binding_update
    )

    with pytest.raises((SourceBindingConflictError, StaleSourceBindingRevisionError)) as captured:
        repository.record_validation(ready, evidence, expected_revision=2)

    if isinstance(captured.value, SourceBindingConflictError):
        assert "authority" in str(captured.value)

    assert repository.load("tenant-a", "source-binding-a") == validating


def test_private_capability_and_validation_cross_tenant_reads_are_generic() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())
    validating = binding(revision=2, state=SourceConnectionBindingState.VALIDATING)
    repository.append(validating, expected_revision=1)
    evidence = validation_evidence()
    ready = validating.model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.READY,
            "capability_profile_digest": evidence.capability_profile_digest,
            "source_observation_ref": evidence.source_observation_ref,
            "revision": 3,
        }
    )
    repository.record_validation(ready, evidence, expected_revision=2)

    for load in (
        lambda: repository.load_capability("tenant-b", "source-binding-a", 1),
        lambda: repository.load_validation("tenant-b", "source-binding-a", 2),
    ):
        with pytest.raises(SourceBindingNotFoundError, match="source binding not found"):
            load()


def test_duplicate_evidence_identity_rolls_back_the_ready_revision() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    first_binding = binding()
    first_capability = capability()
    second_binding = binding().model_copy(update={"binding_id": "source-binding-b"})
    second_capability = capability().model_copy(update={"binding_id": "source-binding-b"})
    repository.create(first_binding, first_capability)
    repository.create(second_binding, second_capability)
    first_validating = binding(revision=2, state=SourceConnectionBindingState.VALIDATING)
    second_validating = first_validating.model_copy(update={"binding_id": "source-binding-b"})
    repository.append(first_validating, expected_revision=1)
    repository.append(second_validating, expected_revision=1)
    first_evidence = validation_evidence()
    second_evidence = validation_evidence(
        {
            "binding_id": "source-binding-b",
        }
    )
    first_ready = first_validating.model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.READY,
            "capability_profile_digest": first_evidence.capability_profile_digest,
            "source_observation_ref": first_evidence.source_observation_ref,
            "revision": 3,
        }
    )
    second_ready = second_validating.model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.READY,
            "capability_profile_digest": second_evidence.capability_profile_digest,
            "source_observation_ref": second_evidence.source_observation_ref,
            "revision": 3,
        }
    )
    repository.record_validation(first_ready, first_evidence, expected_revision=2)

    with pytest.raises(SourceBindingConflictError, match="already exists"):
        repository.record_validation(second_ready, second_evidence, expected_revision=2)

    assert repository.load("tenant-a", "source-binding-b") == second_validating


def test_repository_rejects_direct_lifecycle_bypasses() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    non_draft = binding().model_copy(
        update={"lifecycle_state": SourceConnectionBindingState.VALIDATING}
    )
    with pytest.raises(SourceBindingConflictError, match="draft"):
        repository.create(non_draft, capability())

    repository.create(binding(), capability())
    illegal_append = binding().model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.SUSPENDED,
            "revision": 2,
        }
    )
    with pytest.raises(SourceBindingConflictError, match="transition"):
        repository.append(illegal_append, expected_revision=1)

    invalid_rotation = binding().model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.SUSPENDED,
            "credential_revision": 2,
            "revision": 2,
        }
    )
    rotated_capability = capability().model_copy(update={"credential_revision": 2})
    with pytest.raises(SourceBindingConflictError, match="rotation"):
        repository.rotate(invalid_rotation, rotated_capability, expected_revision=1)

    evidence = validation_evidence({"binding_revision": 1})
    draft_to_ready = binding().model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.READY,
            "capability_profile_digest": evidence.capability_profile_digest,
            "source_observation_ref": evidence.source_observation_ref,
            "revision": 2,
        }
    )
    with pytest.raises(SourceBindingConflictError, match="validating"):
        repository.record_validation(draft_to_ready, evidence, expected_revision=1)

    assert repository.load("tenant-a", "source-binding-a") == binding()


@pytest.mark.parametrize(
    "stale_authority",
    (
        {"capability_profile_digest": "3" * 64},
        {"source_observation_ref": "source-observation-a"},
    ),
)
def test_repository_rotation_rejects_each_stale_validation_authority_field(
    stale_authority: dict[str, object],
) -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())
    validating = binding(revision=2, state=SourceConnectionBindingState.VALIDATING)
    repository.append(validating, expected_revision=1)
    rotated = validating.model_copy(
        update={
            "credential_revision": 2,
            "revision": 3,
        }
        | stale_authority
    )
    rotated_capability = capability().model_copy(update={"credential_revision": 2})

    with pytest.raises(SourceBindingConflictError, match="fresh validation"):
        repository.rotate(rotated, rotated_capability, expected_revision=2)

    assert repository.load("tenant-a", "source-binding-a") == validating


def test_repository_rejects_regressing_revision_timestamp() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())
    candidate = binding(
        revision=2,
        state=SourceConnectionBindingState.VALIDATING,
    ).model_copy(update={"updated_at": datetime(2026, 9, 1, 11, 59, 59, tzinfo=UTC)})

    with pytest.raises(StaleSourceBindingRevisionError):
        repository.append(candidate, expected_revision=1)

    assert repository.load("tenant-a", "source-binding-a") == binding()


def test_repository_rejects_binding_payload_identity_corruption(tmp_path: Path) -> None:
    database_path = str(tmp_path / "binding-corruption.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    repository.create(binding(), capability())
    connection = sqlite3.connect(database_path)
    corrupt = binding().model_copy(update={"tenant_id": "tenant-b"})
    connection.execute(
        "UPDATE source_bindings SET payload = ? WHERE tenant_id = ? AND binding_id = ?",
        (canonical_bytes(corrupt), "tenant-a", "source-binding-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SourceBindingPersistenceError, match="identity"):
        repository.load("tenant-a", "source-binding-a")


def test_repository_rejects_binding_index_identity_corruption(tmp_path: Path) -> None:
    database_path = str(tmp_path / "binding-index-corruption.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    repository.create(binding(), capability())
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE source_bindings SET revision = 2 "
        "WHERE tenant_id = ? AND binding_id = ? AND revision = 1",
        ("tenant-a", "source-binding-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SourceBindingPersistenceError, match="identity"):
        repository.load("tenant-a", "source-binding-a")


def test_repository_rejects_capability_payload_identity_corruption(tmp_path: Path) -> None:
    database_path = str(tmp_path / "capability-corruption.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    repository.create(binding(), capability())
    connection = sqlite3.connect(database_path)
    corrupt = capability().model_copy(update={"tenant_id": "tenant-b"})
    connection.execute(
        "UPDATE private_source_capabilities SET payload = ? "
        "WHERE tenant_id = ? AND binding_id = ? AND credential_revision = ?",
        (canonical_bytes(corrupt), "tenant-a", "source-binding-a", 1),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SourceBindingPersistenceError, match="identity"):
        repository.load_capability("tenant-a", "source-binding-a", 1)


def test_repository_rejects_evidence_payload_identity_corruption(tmp_path: Path) -> None:
    database_path = str(tmp_path / "evidence-corruption.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    repository.create(binding(), capability())
    validating = binding(revision=2, state=SourceConnectionBindingState.VALIDATING)
    repository.append(validating, expected_revision=1)
    evidence = validation_evidence()
    ready = validating.model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.READY,
            "capability_profile_digest": evidence.capability_profile_digest,
            "source_observation_ref": evidence.source_observation_ref,
            "revision": 3,
        }
    )
    repository.record_validation(ready, evidence, expected_revision=2)
    connection = sqlite3.connect(database_path)
    corrupt = evidence.model_copy(update={"tenant_id": "tenant-b"})
    connection.execute(
        "UPDATE source_binding_validation_evidence SET payload = ? "
        "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ?",
        (canonical_bytes(corrupt), "tenant-a", "source-binding-a", 2),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SourceBindingPersistenceError, match="identity"):
        repository.load_validation("tenant-a", "source-binding-a", 2)


def test_repository_rejects_evidence_index_identity_corruption(tmp_path: Path) -> None:
    database_path = str(tmp_path / "evidence-index-corruption.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    repository.create(binding(), capability())
    validating = binding(revision=2, state=SourceConnectionBindingState.VALIDATING)
    repository.append(validating, expected_revision=1)
    evidence = validation_evidence()
    ready = validating.model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.READY,
            "capability_profile_digest": evidence.capability_profile_digest,
            "source_observation_ref": evidence.source_observation_ref,
            "revision": 3,
        }
    )
    repository.record_validation(ready, evidence, expected_revision=2)
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE source_binding_validation_evidence SET evidence_id = ? "
        "WHERE tenant_id = ? AND binding_id = ? AND binding_revision = ?",
        ("source-validation-corrupt", "tenant-a", "source-binding-a", 2),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SourceBindingPersistenceError, match="identity"):
        repository.load_validation("tenant-a", "source-binding-a", 2)


def test_repository_rejects_live_schema_definition_drift(tmp_path: Path) -> None:
    database_path = str(tmp_path / "schema-drift.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    repository.close()
    connection = sqlite3.connect(database_path)
    connection.execute("DROP TABLE source_binding_validation_evidence")
    connection.execute(
        "CREATE TABLE source_binding_validation_evidence ("
        "tenant_id TEXT NOT NULL, binding_id TEXT NOT NULL, binding_revision INTEGER NOT NULL, "
        "evidence_id TEXT NOT NULL, payload BLOB NOT NULL, "
        "PRIMARY KEY (tenant_id, binding_id, binding_revision))"
    )
    connection.commit()
    connection.close()

    with pytest.raises(SourceBindingPersistenceError, match="definition"):
        SQLiteSourceBindingRepository(database_path)


def test_corrupt_stored_data_is_distinguishable_from_a_driver_failure(tmp_path: Path) -> None:
    """A consumer cannot retry its way out of corruption, and must be able to tell.

    Both conditions arrived as `SourceBindingPersistenceError`, so a caller that
    classified it had to choose one verdict for both: treat corruption as
    retryable, or treat a closed connection as permanent. The acquisition runtime
    made the second choice and recorded `authorization_denied` in durable evidence
    for what was a transient database failure.

    `SourceBindingIntegrityError` stays a subclass so every existing consumer keeps
    failing closed exactly as before.
    """
    database_path = str(tmp_path / "binding-classification.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    repository.create(binding(), capability())
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE source_bindings SET payload = ? WHERE tenant_id = ? AND binding_id = ?",
        (
            canonical_bytes(binding().model_copy(update={"tenant_id": "tenant-b"})),
            "tenant-a",
            "source-binding-a",
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SourceBindingIntegrityError, match="identity"):
        repository.load("tenant-a", "source-binding-a")

    driver_failure = SQLiteSourceBindingRepository(":memory:")
    driver_failure.close()

    with pytest.raises(SourceBindingPersistenceError) as captured:
        driver_failure.load("tenant-a", "source-binding-a")

    assert not isinstance(captured.value, SourceBindingIntegrityError)


def test_an_invalid_stored_payload_is_reported_as_corruption(tmp_path: Path) -> None:
    database_path = str(tmp_path / "binding-invalid-payload.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    repository.create(binding(), capability())
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE source_bindings SET payload = ? WHERE tenant_id = ? AND binding_id = ?",
        (b'{"not":"a binding"}', "tenant-a", "source-binding-a"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SourceBindingIntegrityError, match="invalid"):
        repository.load("tenant-a", "source-binding-a")


def test_a_tenant_listing_returns_each_binding_at_its_current_revision_only() -> None:
    """One row per binding, the latest, and nothing of another tenant's.

    The listing is what a console shows as registered, so a stale revision appearing beside the
    current one would show the same source twice in two different states.
    """
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.create(binding(), capability())
    repository.append(
        binding(revision=2, state=SourceConnectionBindingState.VALIDATING),
        expected_revision=1,
    )
    second = binding().model_copy(
        update={"binding_id": "source-binding-b", "connection_handle": "connection-handle-b"}
    )
    repository.create(
        second,
        capability().model_copy(
            update={"binding_id": "source-binding-b", "connection_handle": "connection-handle-b"}
        ),
    )
    other_tenant = binding().model_copy(update={"tenant_id": "tenant-b"})
    repository.create(other_tenant, capability().model_copy(update={"tenant_id": "tenant-b"}))

    listed = repository.list_for_tenant("tenant-a")

    assert [(item.binding_id, item.revision) for item in listed] == [
        ("source-binding-a", 2),
        ("source-binding-b", 1),
    ]
    assert listed[0].lifecycle_state is SourceConnectionBindingState.VALIDATING
    assert repository.list_for_tenant("tenant-b") == (other_tenant,)
    assert repository.list_for_tenant("tenant-unknown") == ()


def test_a_tenant_listing_refuses_a_stored_payload_that_contradicts_its_row(
    tmp_path: Path,
) -> None:
    """Corruption fails the listing rather than being returned inside it.

    A listing that skipped an unreadable row would answer "these are your sources" while holding
    one back, which is the one answer a register must never give.
    """
    database_path = str(tmp_path / "binding-listing-corruption.sqlite")
    repository = SQLiteSourceBindingRepository(database_path)
    repository.create(binding(), capability())
    connection = sqlite3.connect(database_path)
    connection.execute(
        "UPDATE source_bindings SET payload = ? WHERE tenant_id = ? AND binding_id = ?",
        (
            canonical_bytes(binding().model_copy(update={"binding_id": "source-binding-z"})),
            "tenant-a",
            "source-binding-a",
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SourceBindingIntegrityError, match="identity"):
        repository.list_for_tenant("tenant-a")


def test_a_closed_repository_reports_a_listing_failure_as_transient() -> None:
    repository = SQLiteSourceBindingRepository(":memory:")
    repository.close()

    with pytest.raises(SourceBindingPersistenceError) as captured:
        repository.list_for_tenant("tenant-a")

    assert not isinstance(captured.value, SourceBindingIntegrityError)


def test_a_register_opened_for_one_thread_refuses_another(tmp_path: Path) -> None:
    """The default, stated as a test: a connection carries the affinity of its opening thread.

    Asserted because the opposite is what a host composing this register needs, and a default
    that silently changed would make the opt-in below look unnecessary.
    """
    repository = SQLiteSourceBindingRepository(str(tmp_path / "bindings.sqlite3"))
    refused: list[SourceBindingPersistenceError] = []

    def read() -> None:
        try:
            repository.list_for_tenant("tenant-a")
        except SourceBindingPersistenceError as error:
            refused.append(error)

    thread = threading.Thread(target=read)
    thread.start()
    thread.join()

    assert [type(error) for error in refused] == [SourceBindingPersistenceError]
    repository.close()


def test_a_register_opened_across_threads_serves_another(tmp_path: Path) -> None:
    """The opt-in a host opening the register once at startup needs.

    A console reads on the thread it was composed on and runs commands on a threadpool, so the
    register has to answer both. Nothing is relaxed about the data: SQLite's serialized threading
    mode is what this relies on, and the write below is read back here to show it committed.
    """
    repository = SQLiteSourceBindingRepository(
        str(tmp_path / "bindings.sqlite3"), check_same_thread=False
    )
    thread = threading.Thread(target=lambda: repository.create(binding(), capability()))
    thread.start()
    thread.join()

    (stored,) = repository.list_for_tenant("tenant-a")
    assert stored.binding_id == "source-binding-a"
    assert stored.lifecycle_state is SourceConnectionBindingState.DRAFT
    repository.close()
