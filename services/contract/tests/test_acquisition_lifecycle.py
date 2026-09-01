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
