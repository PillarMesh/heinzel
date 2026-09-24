from __future__ import annotations

import pytest
from heinzel_console.operation_handles import (
    CONSOLE_HANDLE_PATTERN,
    InMemoryOperationHandleRepository,
    OperationHandleRecord,
    PublicOperationStatus,
    mint_console_handle,
    serialize_operation,
)

_PRIVATE_IDENTITY = "whb-0123456789abcdef01234567/whop-canary-private-operation"


def _record(
    *,
    tenant_id: str = "tenant-alpha",
    console_handle: str | None = None,
    private_identity: str = _PRIVATE_IDENTITY,
) -> OperationHandleRecord:
    return OperationHandleRecord(
        tenant_id=tenant_id,
        console_handle=console_handle or mint_console_handle(),
        capability_kind="warehouse_lifecycle",
        private_identity=private_identity,
    )


def test_minted_console_handle_uses_the_public_opaque_handle_format() -> None:
    handle = mint_console_handle()

    assert CONSOLE_HANDLE_PATTERN.fullmatch(handle) is not None


def test_console_handle_is_random_rather_than_derived_from_the_private_identity() -> None:
    entropy = iter((bytes(range(16)), bytes(reversed(range(16)))))

    first = mint_console_handle(entropy=lambda _: next(entropy))
    second = mint_console_handle(entropy=lambda _: next(entropy))

    assert first != second
    assert _PRIVATE_IDENTITY not in first
    assert _PRIVATE_IDENTITY not in second


def test_console_handle_never_encodes_or_hashes_the_private_identity() -> None:
    import hashlib

    handle = mint_console_handle()
    hexadecimal_body = handle.removeprefix("op_")

    forbidden = {
        hashlib.sha256(_PRIVATE_IDENTITY.encode()).hexdigest()[:32],
        hashlib.md5(_PRIVATE_IDENTITY.encode(), usedforsecurity=False).hexdigest(),
        _PRIVATE_IDENTITY.encode().hex()[:32],
    }
    assert hexadecimal_body not in forbidden


def test_record_rejects_a_handle_that_is_not_the_public_format() -> None:
    with pytest.raises(ValueError, match="console handle"):
        OperationHandleRecord(
            tenant_id="tenant-alpha",
            console_handle="whop-private-operation",
            capability_kind="warehouse_lifecycle",
            private_identity=_PRIVATE_IDENTITY,
        )


def test_repository_returns_the_record_for_the_owning_tenant() -> None:
    repository = InMemoryOperationHandleRepository()
    record = _record()
    repository.store(record)

    assert repository.load(tenant_id="tenant-alpha", console_handle=record.console_handle) == record


def test_wrong_tenant_lookup_is_indistinguishable_from_an_unknown_handle() -> None:
    repository = InMemoryOperationHandleRepository()
    record = _record()
    repository.store(record)

    wrong_tenant = repository.load(tenant_id="tenant-beta", console_handle=record.console_handle)
    unknown_handle = repository.load(tenant_id="tenant-beta", console_handle=mint_console_handle())

    assert wrong_tenant is None
    assert wrong_tenant == unknown_handle


def test_serialized_operation_exposes_only_phase_state_summary_and_evidence() -> None:
    record = _record()
    status = PublicOperationStatus(
        state="running",
        phase="provider_created",
        summary="Warehouse provisioning is running.",
        revision=3,
    )

    view = serialize_operation(console_handle=record.console_handle, status=status)
    payload = view.model_dump(mode="json")

    assert payload == {
        "operation_id": record.console_handle,
        "revision": 3,
        "state": "running",
        "phase": "provider_created",
        "summary": "Warehouse provisioning is running.",
        "evidence_ref": None,
        "failure": None,
        "recovery_actions": [],
        "operation_digest": None,
        "retry_token": None,
    }


def test_serialized_operation_never_carries_the_private_identity() -> None:
    record = _record()

    view = serialize_operation(
        console_handle=record.console_handle,
        status=PublicOperationStatus(
            state="failed",
            phase="provider_created",
            summary="Warehouse provisioning failed.",
        ),
    )

    assert _PRIVATE_IDENTITY not in view.model_dump_json()
