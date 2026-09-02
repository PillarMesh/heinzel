"""Opaque console operation handles.

The owning services' operations are private state: a warehouse operation carries a
provider resource handle, and an acquisition batch carries cursors and checkpoints.
The console therefore mints its own random handle, keeps the tenant-scoped map from
that handle to the private identity in a private repository, and serializes only a
lifecycle phase, a typed state, a safe summary, and a public evidence reference.
"""

from __future__ import annotations

import re
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .contracts import OperationFailureView, OperationState, OperationView, RecoveryAction

type OperationCapabilityKind = Literal[
    "warehouse_lifecycle", "warehouse_binding", "acquisition_batch"
]

CONSOLE_HANDLE_PATTERN = re.compile(r"^op_[0-9a-f]{32}$")
_HANDLE_ENTROPY_BYTES = 16


def mint_console_handle(*, entropy: Callable[[int], bytes] = secrets.token_bytes) -> str:
    """Mint a fresh public handle.

    The value is random rather than a hash or encoding of the private identity, so a
    holder of the handle learns nothing about the owning service's operation.
    """
    return "op_" + entropy(_HANDLE_ENTROPY_BYTES).hex()


@dataclass(frozen=True, slots=True)
class OperationHandleRecord:
    tenant_id: str
    console_handle: str
    capability_kind: OperationCapabilityKind
    private_identity: str

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("operation handle record requires a tenant")
        if not self.private_identity:
            raise ValueError("operation handle record requires a private identity")
        if CONSOLE_HANDLE_PATTERN.fullmatch(self.console_handle) is None:
            raise ValueError(
                "console handle must be 'op_' followed by 32 lowercase hexadecimal characters"
            )


class OperationHandleRepository(Protocol):
    def store(self, record: OperationHandleRecord) -> None: ...

    def load(self, *, tenant_id: str, console_handle: str) -> OperationHandleRecord | None: ...


class InMemoryOperationHandleRepository:
    """Process-local handle map.

    Lookup is keyed by tenant and handle together, so a handle presented by another
    tenant is an ordinary miss. The caller cannot distinguish "not yours" from
    "never minted", which is what keeps handle probing non-enumerating.
    """

    def __init__(self, *, maximum_records: int = 4096) -> None:
        if maximum_records < 1:
            raise ValueError("the operation handle limit must be positive")
        self._maximum_records = maximum_records
        self._records: OrderedDict[tuple[str, str], OperationHandleRecord] = OrderedDict()

    def store(self, record: OperationHandleRecord) -> None:
        # Bounded because nothing ever removed a handle: a long-lived process kept
        # one record per operation for its whole lifetime. The oldest is dropped,
        # and a caller holding an evicted handle sees the same non-enumerating miss
        # as one that was never minted.
        self._records[(record.tenant_id, record.console_handle)] = record
        self._records.move_to_end((record.tenant_id, record.console_handle))
        while len(self._records) > self._maximum_records:
            self._records.popitem(last=False)

    def load(self, *, tenant_id: str, console_handle: str) -> OperationHandleRecord | None:
        return self._records.get((tenant_id, console_handle))

    def tracked_records(self) -> int:
        return len(self._records)


@dataclass(frozen=True, slots=True)
class PublicOperationStatus:
    state: OperationState
    phase: str
    summary: str
    revision: int = 1
    evidence_ref: str | None = None
    failure: OperationFailureView | None = None
    recovery_actions: tuple[RecoveryAction, ...] = field(default=())


def serialize_operation(*, console_handle: str, status: PublicOperationStatus) -> OperationView:
    if CONSOLE_HANDLE_PATTERN.fullmatch(console_handle) is None:
        raise ValueError("an operation projection requires a minted console handle")
    return OperationView(
        operation_id=console_handle,
        revision=status.revision,
        state=status.state,
        phase=status.phase,
        summary=status.summary,
        evidence_ref=status.evidence_ref,
        failure=status.failure,
        recovery_actions=status.recovery_actions,
    )
