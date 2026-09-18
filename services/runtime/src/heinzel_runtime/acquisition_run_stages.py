"""The stages a state-owned acquisition run executes, composed from the product's applications.

`LeasedRunExecutor` runs replay-stable stages and resumes an attempt by replaying the ones that
already committed. These two are that pair for an acquisition contract: prepare the batch, then
land it and acknowledge its checkpoint.

The landing stage prepares again rather than carrying state between stages. Preparation is
replay-stable, so the second call returns the receipt the first attempt committed without reaching
the source, which is exactly what a resumed attempt must land.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from heinzel_provider_sdk import AcquisitionIntent
from heinzel_provider_sdk.acquisition_models import AcquisitionMode

from .acquisition import AcquisitionPreparationResult
from .acquisition_application import AcquisitionRunPreparation
from .acquisition_landing import AcquisitionLandingResult
from .leased_run import RunLease, RunStage


class AcquisitionRunPreparer(Protocol):
    def prepare_now(
        self,
        *,
        tenant_id: str,
        contract_ref: str,
        trigger_window: str,
        acquisition_mode: AcquisitionMode,
    ) -> AcquisitionRunPreparation: ...


class AcquisitionRunLanding(Protocol):
    async def land(
        self,
        *,
        trigger_window: str,
        intent: AcquisitionIntent,
        preparation: AcquisitionPreparationResult,
    ) -> AcquisitionLandingResult: ...


def compose_acquisition_run_stages(
    *,
    application: AcquisitionRunPreparer,
    landing: AcquisitionRunLanding,
    tenant_id: str,
    contract_ref: str,
    trigger_window: str,
    acquisition_mode: AcquisitionMode,
) -> tuple[RunStage, ...]:
    def prepare() -> AcquisitionRunPreparation:
        return application.prepare_now(
            tenant_id=tenant_id,
            contract_ref=contract_ref,
            trigger_window=trigger_window,
            acquisition_mode=acquisition_mode,
        )

    def prepared(_lease: RunLease) -> str:
        receipt = prepare().result.prepared_receipt
        if receipt is None:
            raise AcquisitionRunStageError("acquisition_did_not_prepare_a_batch")
        return receipt.prepared_receipt_id

    def acknowledged(_lease: RunLease) -> str:
        preparation = prepare()
        landed = asyncio.run(
            landing.land(
                trigger_window=trigger_window,
                intent=preparation.intent,
                preparation=preparation.result,
            )
        )
        return landed.checkpoint_receipt.checkpoint_receipt_id

    return (
        RunStage("acquisition_prepared", prepared),
        RunStage("land_acknowledged", acknowledged),
    )


class AcquisitionRunStageError(RuntimeError):
    """A stage could not produce the boundary it exists to prove."""
