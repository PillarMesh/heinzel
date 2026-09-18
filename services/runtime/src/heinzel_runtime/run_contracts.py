"""The trigger service's view of an activated contract, read from contract-service's own record.

Trigger decides which windows are due; it must only materialize runs for a contract revision that
contract-service currently holds activated. This adapter reads the current activated record
through the same resolver acquisition composition uses and answers for exactly one revision, so a
superseded, deactivated or unknown contract yields no run authority. A store that cannot be read
raises rather than answering "no contract", so an outage is never recorded as a policy decision.

The run's plan digest is the activated acquisition contract's digest: it is the exact authority an
acquisition run executes under, so a changed contract gives a distinct canonical run intent.
"""

from __future__ import annotations

from heinzel_trigger import ActivatedRunContract

from .acquisition_composition import ActivatedContractReader, activated_contract_resolver
from .acquisition_errors import AcquisitionContractError


class ActivatedAcquisitionRunContracts:
    def __init__(self, repository: ActivatedContractReader) -> None:
        self._resolve = activated_contract_resolver(repository)

    def load_activated(
        self, tenant_id: str, contract_ref: str, revision: int
    ) -> ActivatedRunContract | None:
        try:
            record = self._resolve(tenant_id, contract_ref)
        except AcquisitionContractError:
            return None
        if record.revision != revision:
            return None
        return ActivatedRunContract(
            tenant_id=record.tenant_id,
            contract_ref=record.contract_ref,
            revision=record.revision,
            plan_digest=record.contract_digest,
        )
