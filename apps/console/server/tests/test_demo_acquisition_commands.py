"""What a commanded acquisition decides before it reaches a source or a warehouse.

Landing needs a cluster and is covered by the live suite. What is asked here is the part that
is decided first: whose contract this is, whether the mode asked for is one the source can
still be read in, and what comes back from a run the runtime governed to no batch at all.

The acquisition is a double, because composing a real one takes an activated contract over a
real reading and this is about the command surface in front of it, not about that composition.
`DemoAcquisition`'s own preconditions are in `test_demo_generation.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from heinzel_console.demo.acquisition_commands import DemoAcquisitionCommands
from heinzel_console.demo.cursor_cipher import DemoCursorCipher
from heinzel_console.demo.generation import DemoAcquisition
from heinzel_console.demo.stores import DemoStores
from heinzel_console.errors import ConsoleInvalidRequest
from heinzel_evidence import AcquisitionEvidenceReceipt, SQLiteAcquisitionEvidenceWriter
from heinzel_provider_sdk import AcquisitionNoValidPlan
from heinzel_provider_sdk.acquisition_models import AcquisitionMode
from heinzel_runtime import AcquisitionPreparationResult, AcquisitionRunPreparation

_NOW = datetime(2026, 9, 13, tzinfo=UTC)
_TENANT = "tenant-demo"
_CONTRACT = "contract:demo-orders:v1"
_BINDING = "src-0123456789abcdef01234567"


@pytest.fixture(name="stores")
def _stores(tmp_path: Path) -> Iterator[DemoStores]:
    stores = DemoStores(tmp_path / "state", cursor_cipher_factory=DemoCursorCipher)
    try:
        yield stores
    finally:
        stores.close()


def _receipt(
    *,
    outcome: str,
    evidence_id: str,
    acquisition_mode: AcquisitionMode = "snapshot",
    checkpoint_receipt_ref: str | None = None,
) -> AcquisitionEvidenceReceipt:
    return AcquisitionEvidenceReceipt(
        evidence_id=evidence_id,
        tenant_id=_TENANT,
        run_intent_ref="a" * 64,
        contract_ref=_CONTRACT,
        source_binding_ref=_BINDING,
        acquisition_mode=acquisition_mode,
        logical_object_refs=("customer_orders",),
        prepared_receipt_ref=("prepared-1" if checkpoint_receipt_ref is not None else None),
        checkpoint_receipt_ref=checkpoint_receipt_ref,
        prior_checkpoint_revision=0,
        resulting_checkpoint_revision=1 if checkpoint_receipt_ref is not None else None,
        reason_codes=() if outcome == "acknowledged" else ("acquisition_mode_not_admitted",),
        outcome=outcome,  # type: ignore[arg-type]
        created_at=_NOW,
    )


class _PreparingNothing:
    """An acquisition the runtime governed to no batch, and which must not then land.

    `result` is a real `AcquisitionPreparationResult`, whose validator refuses a shape the
    runtime would never produce, so what this hands back is a result the real one could be.
    `intent` is absent because nothing downstream of a run with no batch reads it: a run that
    reached for one here would fail rather than quietly land something else.
    """

    def __init__(self, receipt: AcquisitionEvidenceReceipt) -> None:
        self._result = AcquisitionPreparationResult(
            evidence=receipt,
            prepared_receipt=None,
            batch_manifest=None,
            governed_outcome=AcquisitionNoValidPlan(
                reason_codes=("acquisition_mode_not_admitted",),
                failed_constraints=("contract.acquisition_modes",),
            ),
        )
        self.prepared: list[tuple[str, str]] = []

    def prepare_run(
        self, *, acquisition_mode: AcquisitionMode, trigger_window: str
    ) -> AcquisitionRunPreparation:
        self.prepared.append((acquisition_mode, trigger_window))
        return cast(AcquisitionRunPreparation, SimpleNamespace(result=self._result))


def _commands(stores: DemoStores, acquisition: object) -> DemoAcquisitionCommands:
    return DemoAcquisitionCommands(
        cast(DemoAcquisition, acquisition),
        tenant_id=_TENANT,
        contract_ref=_CONTRACT,
        landing_dsn="postgresql://landing@127.0.0.1:1/heinzel",
        stores=stores,
    )


def test_another_tenants_contract_is_not_found_rather_than_run(stores: DemoStores) -> None:
    """A reference this deployment never held reads as absent, from either direction."""
    acquisition = _PreparingNothing(_receipt(outcome="no_valid_plan", evidence_id="evidence-1"))
    commands = _commands(stores, acquisition)

    with pytest.raises(KeyError):
        commands.run_now(
            tenant_id="tenant-somebody-else",
            contract_ref=_CONTRACT,
            trigger_window="2026-09-14T12:00:00Z/PT1H",
            acquisition_mode="incremental",
        )
    with pytest.raises(KeyError):
        commands.run_now(
            tenant_id=_TENANT,
            contract_ref="contract:something-else:v1",
            trigger_window="2026-09-14T12:00:00Z/PT1H",
            acquisition_mode="incremental",
        )

    assert acquisition.prepared == []


def test_a_snapshot_of_an_acquired_source_names_the_mode_rather_than_the_service(
    stores: DemoStores,
) -> None:
    """The refusal an operator can act on, raised before the provider is asked.

    An `acknowledged` receipt is the record of a checkpoint having advanced, and a snapshot is
    admitted only from revision 0. Left to the provider this came back
    `permanent_configuration`, which the console can only publish as state it cannot trust.
    """
    SQLiteAcquisitionEvidenceWriter(stores.acquisition_evidence).append(
        _receipt(
            outcome="acknowledged",
            evidence_id="evidence-acknowledged",
            checkpoint_receipt_ref="checkpoint-1",
        )
    )
    acquisition = _PreparingNothing(_receipt(outcome="no_valid_plan", evidence_id="evidence-2"))
    commands = _commands(stores, acquisition)

    with pytest.raises(ConsoleInvalidRequest) as refusal:
        commands.run_now(
            tenant_id=_TENANT,
            contract_ref=_CONTRACT,
            trigger_window="2026-09-14T12:00:00Z/PT1H",
            acquisition_mode="snapshot",
        )

    assert refusal.value.code == "acquisition_mode_not_available"
    assert refusal.value.field == "acquisition_mode"
    assert acquisition.prepared == []


def test_an_incremental_run_of_an_acquired_source_is_not_refused(stores: DemoStores) -> None:
    """The complement: having acquired once is what an incremental run is for."""
    SQLiteAcquisitionEvidenceWriter(stores.acquisition_evidence).append(
        _receipt(
            outcome="acknowledged",
            evidence_id="evidence-acknowledged",
            checkpoint_receipt_ref="checkpoint-1",
        )
    )
    governed = _receipt(
        outcome="no_valid_plan", evidence_id="evidence-3", acquisition_mode="incremental"
    )
    acquisition = _PreparingNothing(governed)

    reported = _commands(stores, acquisition).run_now(
        tenant_id=_TENANT,
        contract_ref=_CONTRACT,
        trigger_window="2026-09-14T13:00:00Z/PT1H",
        acquisition_mode="incremental",
    )

    assert reported == governed
    assert acquisition.prepared == [("incremental", "2026-09-14T13:00:00Z/PT1H")]


def test_a_run_governed_to_no_batch_reports_its_own_receipt_and_lands_nothing(
    stores: DemoStores,
) -> None:
    """An incremental run of a source nothing has written to is an outcome, not a failure.

    The landing DSN names a port nothing serves, so a run that tried to land would raise
    `psycopg.OperationalError` rather than return. Returning the receipt is the assertion.
    """
    governed = _receipt(
        outcome="no_valid_plan", evidence_id="evidence-4", acquisition_mode="incremental"
    )

    reported = _commands(stores, _PreparingNothing(governed)).run_now(
        tenant_id=_TENANT,
        contract_ref=_CONTRACT,
        trigger_window="2026-09-14T14:00:00Z/PT1H",
        acquisition_mode="incremental",
    )

    assert reported.outcome == "no_valid_plan"
    assert reported.reason_codes == ("acquisition_mode_not_admitted",)
