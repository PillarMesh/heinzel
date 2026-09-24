from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from heinzel_connection_broker import (
    PrivateSourceCapability,
    SourceConnectionBinding,
    SourceConnectionBindingState,
    SQLiteSourceBindingRepository,
)
from heinzel_contract_model import canonical_bytes
from heinzel_runtime import (
    AcquisitionAuthorizationError,
    AcquisitionIntegrityError,
    AcquisitionTransientError,
    source_binding_resolver,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _binding(binding_id: str = "source-binding-a") -> SourceConnectionBinding:
    return SourceConnectionBinding(
        binding_id=binding_id,
        tenant_id="tenant-a",
        provider_kind="postgresql",
        connection_handle="connection-handle-a",
        account_mode="not_applicable",
        lifecycle_state=SourceConnectionBindingState.DRAFT,
        approved_object_refs=("order",),
        capability_profile_digest=None,
        source_observation_ref=None,
        credential_revision=1,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _capability(binding_id: str = "source-binding-a") -> PrivateSourceCapability:
    return PrivateSourceCapability(
        tenant_id="tenant-a",
        binding_id=binding_id,
        provider_kind="postgresql",
        connection_handle="connection-handle-a",
        account_mode="not_applicable",
        credential_revision=1,
        endpoint_reference=f"endpoint-ref:{'a' * 64}",
        credential_reference=f"credential-ref:{'b' * 64}",
    )


def _repository(path: Path) -> SQLiteSourceBindingRepository:
    repository = SQLiteSourceBindingRepository(str(path))
    repository.create(_binding(), _capability())
    return repository


def test_a_stored_binding_resolves_to_the_binding_itself(tmp_path: Path) -> None:
    resolve = source_binding_resolver(_repository(tmp_path / "bindings.sqlite"))

    assert resolve("tenant-a", "source-binding-a") == _binding()


def test_an_absent_binding_is_an_authorization_failure(tmp_path: Path) -> None:
    """Not visible to this tenant and not existing are the same answer."""
    resolve = source_binding_resolver(_repository(tmp_path / "bindings.sqlite"))

    with pytest.raises(AcquisitionAuthorizationError):
        resolve("tenant-a", "source-binding-somebody-elses")
    with pytest.raises(AcquisitionAuthorizationError):
        resolve("tenant-somebody-else", "source-binding-a")


def test_a_store_that_cannot_be_read_is_transient_rather_than_a_denial(
    tmp_path: Path,
) -> None:
    """The defect this resolver exists to prevent.

    Handing `repository.load` to the runtime directly made every driver failure
    arrive as an unclassified exception, which the runtime turned into
    `AcquisitionAuthorizationError` and then wrote into durable evidence as
    `authorization_denied`. A momentary database failure is not a denial, and a
    receipt that says it is misreports the tenant's own authority.
    """
    repository = _repository(tmp_path / "bindings.sqlite")
    repository.close()
    resolve = source_binding_resolver(repository)

    with pytest.raises(AcquisitionTransientError):
        resolve("tenant-a", "source-binding-a")


def test_a_corrupt_stored_binding_is_an_integrity_failure_not_a_transient_one(
    tmp_path: Path,
) -> None:
    """Corruption is permanent, so reporting it as retryable would loop forever.

    `SourceBindingIntegrityError` subclasses `SourceBindingPersistenceError`, so a
    handler that checks the base class first silently swallows this case. The order
    of the two clauses is the whole behaviour here.
    """
    path = tmp_path / "bindings.sqlite"
    repository = _repository(path)
    repository.close()
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE source_bindings SET payload = ? WHERE tenant_id = ? AND binding_id = ?",
        (
            canonical_bytes(_binding().model_copy(update={"tenant_id": "tenant-b"})),
            "tenant-a",
            "source-binding-a",
        ),
    )
    connection.commit()
    connection.close()
    resolve = source_binding_resolver(SQLiteSourceBindingRepository(str(path)))

    with pytest.raises(AcquisitionIntegrityError):
        resolve("tenant-a", "source-binding-a")


def test_the_resolver_never_leaks_the_broker_s_own_exception_types(tmp_path: Path) -> None:
    """A boundary that leaks its dependency's types forces every caller to import it."""
    repository = _repository(tmp_path / "bindings.sqlite")
    repository.close()
    resolve = source_binding_resolver(repository)

    with pytest.raises(AcquisitionTransientError) as captured:
        resolve("tenant-a", "source-binding-a")

    assert captured.value.__cause__ is None
    assert "sqlite" not in str(captured.value).lower()
