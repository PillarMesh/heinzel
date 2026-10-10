"""Run an acquisition the console commanded, land what it prepared, and report the receipt.

The acquisition application prepares and nothing more. A console command that stopped there
would leave a verified batch in a staging store that nothing in the product can land, and
report the capability as delivered -- so this composes the landing behind the same call, over
the same stores and the same least-privilege roles the demonstration's first acquisition ran
under.

What it does not do is build a product over what landed. The raw generation is tagged with
its own identifier and the materialized product names the generation it was built from, so a
newly landed generation changes no answer until something materializes over it. That is the
next thing to build, and saying so is better than a command that half does it.
"""

from __future__ import annotations

import asyncio

from heinzel_evidence import AcquisitionEvidenceReceipt
from heinzel_provider_sdk.acquisition_models import AcquisitionMode

from ..governed_adapters import refuse_a_snapshot_of_an_already_acquired_source
from .generation import DemoAcquisition
from .stores import DemoStores

__all__ = ["DemoAcquisitionCommands"]


class DemoAcquisitionCommands:
    """The one activated contract this demonstration can be asked to acquire again.

    It serves exactly one tenant and one contract, because that is what the demonstration
    has. Anything else is a `KeyError`, which the console backend turns into a not-found
    rather than reporting a downstream failure for a reference it never held.
    """

    def __init__(
        self,
        acquisition: DemoAcquisition,
        *,
        tenant_id: str,
        contract_ref: str,
        landing_dsn: str,
        stores: DemoStores,
    ) -> None:
        self._acquisition = acquisition
        self._tenant_id = tenant_id
        self._contract_ref = contract_ref
        self._landing_dsn = landing_dsn
        self._stores = stores

    def run_now(
        self,
        *,
        tenant_id: str,
        contract_ref: str,
        trigger_window: str,
        acquisition_mode: AcquisitionMode,
    ) -> AcquisitionEvidenceReceipt:
        """Prepare one batch under the activated contract and land it if there is one.

        `asyncio.run` because landing is asynchronous and this is called from the thread the
        console serves a command on, which has no loop of its own. A command route runs in a
        worker thread for exactly this kind of work, so the loop is created, used and closed
        inside the call rather than borrowed from the server.
        """
        if tenant_id != self._tenant_id or contract_ref != self._contract_ref:
            raise KeyError("the demonstration has no such activated acquisition contract")
        refuse_a_snapshot_of_an_already_acquired_source(
            tenant_id=tenant_id,
            contract_ref=contract_ref,
            acquisition_mode=acquisition_mode,
            receipts=self._stores.acquisition_evidence,
        )
        preparation = self._acquisition.prepare_run(
            acquisition_mode=acquisition_mode, trigger_window=trigger_window
        )
        if preparation.result.batch_manifest is None:
            # Governed to no batch rather than failed: a `No Valid Plan` or a
            # resynchronization. The receipt the runtime already wrote says which, and there
            # is nothing to land. A batch of no records is not this case -- it has a manifest
            # and no segment, and running it through landing is how the checkpoint records
            # that the source was read and had nothing new.
            return preparation.result.evidence
        landed = asyncio.run(
            self._acquisition.land_run(
                preparation,
                trigger_window=trigger_window,
                landing_dsn=self._landing_dsn,
            )
        )
        return self._acknowledgement_receipt(landed.checkpoint_receipt_id)

    def _acknowledgement_receipt(self, checkpoint_receipt_id: str) -> AcquisitionEvidenceReceipt:
        """The receipt the runtime wrote for this acknowledgement, read back from its store.

        The runner appends it and returns the checkpoint receipt instead, so this is a lookup
        rather than a hand-off. It matches on the checkpoint reference the landing just
        committed, which one receipt carries and no other can, rather than on recency.
        """
        receipt = next(
            (
                item
                for item in self._stores.acquisition_evidence.list_acquisition_receipts(
                    self._tenant_id
                )
                if item.checkpoint_receipt_ref == checkpoint_receipt_id
            ),
            None,
        )
        if receipt is None:
            raise RuntimeError(
                "the demonstration's acquisition landed and acknowledged a batch, and the "
                "evidence store holds no receipt naming that acknowledgement"
            )
        return receipt
