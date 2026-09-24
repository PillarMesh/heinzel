"""Acquisition activation bound to an approved product intent.

An activated acquisition contract names the product intent it serves. The lifecycle repository
checks that the contract, its activation approval, and its source validation agree with each other,
but it cannot see request management, so on its own it would accept a `product_intent_ref` that
names an intent nobody approved. This service closes that gap: it asks the product intent authority
whether the reference resolves to an approved intent for the tenant, and whether the contract's
source binding is one that intent was approved for, before any activation is recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from heinzel_contract_model import ArtifactReference

from .acquisition_lifecycle import (
    AcquisitionContractActivationDeniedError,
    AcquisitionContractLifecycleRepository,
)
from .process_models import (
    AcquisitionActivationApproval,
    ActivatedAcquisitionContract,
    ActivatedAcquisitionContractRecord,
)
from .source_observation import ValidatedSourceBinding


class ApprovedProductIntentSources(Protocol):
    def approved_source_refs(
        self, *, tenant_id: str, product_intent_ref: ArtifactReference
    ) -> tuple[str, ...] | None:
        """The source refs of the tenant's approved intent at exactly this reference, or None."""
        ...


class ProductIntentBoundActivationService:
    def __init__(
        self,
        repository: AcquisitionContractLifecycleRepository,
        *,
        product_intents: ApprovedProductIntentSources,
    ) -> None:
        self._repository = repository
        self._product_intents = product_intents

    def activate(
        self,
        *,
        idempotency_key: str,
        contract: ActivatedAcquisitionContract | Mapping[str, object],
        approval: AcquisitionActivationApproval | Mapping[str, object],
        source_validation: ValidatedSourceBinding | Mapping[str, object],
    ) -> ActivatedAcquisitionContractRecord:
        candidate = ActivatedAcquisitionContract.model_validate(contract, strict=True)
        approved_sources = self._product_intents.approved_source_refs(
            tenant_id=candidate.tenant_id,
            product_intent_ref=candidate.product_intent_ref,
        )
        if approved_sources is None or candidate.source_binding_ref not in approved_sources:
            raise AcquisitionContractActivationDeniedError()
        return self._repository.activate_contract(
            idempotency_key=idempotency_key,
            contract=candidate,
            approval=approval,
            source_validation=source_validation,
        )
