from __future__ import annotations

from typing import Protocol

from heinzel_connection_broker import (
    SourceBindingIntegrityError,
    SourceBindingNotFoundError,
    SourceBindingPersistenceError,
    SourceConnectionBinding,
)

from .acquisition import BindingResolver
from .acquisition_errors import (
    AcquisitionAuthorizationError,
    AcquisitionIntegrityError,
    AcquisitionTransientError,
)


class SourceBindingReader(Protocol):
    """The one read the acquisition runtime needs from connection-broker."""

    def load(self, tenant_id: str, binding_id: str) -> SourceConnectionBinding: ...


def source_binding_resolver(repository: SourceBindingReader) -> BindingResolver:
    """Adapt connection-broker's binding read to the runtime's failure taxonomy.

    The signatures already match, so this exists only for the failures. Handing
    `repository.load` to the runner directly meant every broker exception arrived
    unclassified, and the runner's catch-all turned all of them into
    `AcquisitionAuthorizationError` -- recorded in durable evidence as
    `authorization_denied`. A momentary database failure is not a denial, and a
    corrupt stored row is not one either.

    The runner re-raises `AcquisitionRuntimeError` untouched, so classifying here
    is what reaches the receipt.
    """

    def resolve(tenant_id: str, binding_ref: str) -> SourceConnectionBinding:
        try:
            return repository.load(tenant_id, binding_ref)
        except SourceBindingNotFoundError:
            # A binding another tenant owns is absent to this one, so absence and
            # denial are the same answer and neither confirms the other exists.
            raise AcquisitionAuthorizationError("source_binding_not_found") from None
        # Integrity first: it subclasses the persistence error, so the broader
        # clause would swallow corruption and report it as retryable forever.
        except SourceBindingIntegrityError:
            raise AcquisitionIntegrityError("source_binding_stored_state_invalid") from None
        except SourceBindingPersistenceError:
            raise AcquisitionTransientError("source_binding_store_unavailable") from None

    return resolve
