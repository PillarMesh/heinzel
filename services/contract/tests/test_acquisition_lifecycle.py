from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pillarmesh_contract_service import (
    AcquisitionContractLifecycleNotFoundError,
    SQLiteAcquisitionContractLifecycleRepository,
    StaleAcquisitionContractLifecycleError,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def test_activation_is_idempotent_and_tenant_qualified() -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")

    first = repository.activate(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        activated_at=NOW,
    )
    replay = repository.activate(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        activated_at=NOW + timedelta(seconds=1),
    )

    assert replay == first
    with pytest.raises(AcquisitionContractLifecycleNotFoundError):
        repository.get("tenant-b", "4" * 64)


def test_retirement_is_exact_cas_and_cannot_reactivate() -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    activated = repository.activate(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        activated_at=NOW,
    )

    with pytest.raises(StaleAcquisitionContractLifecycleError):
        repository.retire(
            tenant_id="tenant-a",
            contract_digest="4" * 64,
            expected_revision=activated.revision + 1,
            retired_at=NOW + timedelta(seconds=1),
        )
    retired = repository.retire(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        expected_revision=activated.revision,
        retired_at=NOW + timedelta(seconds=1),
    )
    replay = repository.retire(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        expected_revision=activated.revision,
        retired_at=NOW + timedelta(seconds=2),
    )

    assert replay == retired
    assert retired.revision == 2
    assert retired.lifecycle_state == "retired"
    with pytest.raises(StaleAcquisitionContractLifecycleError, match="cannot reactivate"):
        repository.activate(
            tenant_id="tenant-a",
            contract_digest="4" * 64,
            activated_at=NOW + timedelta(seconds=3),
        )


def test_activated_contracts_list_only_for_their_own_tenant() -> None:
    """A tenant-scoped listing is the first half of deriving a run's tenant.

    The repository could only read one contract at a time, so nothing could answer
    "which contracts are activated for this tenant". Listing is what makes the
    tenant derivable from a run's contract digest without storing a tenant on the
    run itself.
    """
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    repository.activate(tenant_id="tenant-a", contract_digest="4" * 64, activated_at=NOW)
    repository.activate(tenant_id="tenant-a", contract_digest="5" * 64, activated_at=NOW)
    repository.activate(tenant_id="tenant-b", contract_digest="6" * 64, activated_at=NOW)

    listed = repository.list_activated("tenant-a")

    assert {state.contract_digest for state in listed} == {"4" * 64, "5" * 64}
    assert {state.tenant_id for state in listed} == {"tenant-a"}


def test_a_retired_contract_leaves_the_activated_listing() -> None:
    """Listing means *activated*, not *ever activated*.

    A retired contract's runs were still witnessed under this tenant, but the
    listing answers which contracts are live, and retirement is exactly the
    transition that ends that.
    """
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")
    activated = repository.activate(
        tenant_id="tenant-a", contract_digest="4" * 64, activated_at=NOW
    )
    repository.activate(tenant_id="tenant-a", contract_digest="5" * 64, activated_at=NOW)

    repository.retire(
        tenant_id="tenant-a",
        contract_digest="4" * 64,
        expected_revision=activated.revision,
        retired_at=NOW + timedelta(seconds=1),
    )

    listed = repository.list_activated("tenant-a")
    assert {state.contract_digest for state in listed} == {"5" * 64}


def test_a_tenant_with_no_activated_contracts_lists_empty() -> None:
    repository = SQLiteAcquisitionContractLifecycleRepository(":memory:")

    assert repository.list_activated("tenant-unknown") == ()
