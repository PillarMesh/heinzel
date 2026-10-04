"""Acquire the demonstration's source rows and land them as a raw generation.

The governed answer the demonstration serves is asked of a product generation, and a
generation exists only if rows were read from a source and landed under a receipt. This
module does both, through the real providers rather than by writing the landed relation
itself: acquisition observes the source and opens a session against the approved schema,
and landing runs through `LandingRunner` so the generation ledger records what arrived.

Writing `raw.raw_customer_orders` directly would be shorter and would leave the
demonstration with a landed relation no receipt describes -- which is the one thing a
generation is for. The providers connect as the least-privilege roles `warehouse.py`
creates, so a step reaching past its grant fails here exactly as it would elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from heinzel_contract_model import ManagedIntegrationContract, canonical_bytes, digest
from heinzel_provider_postgresql import (
    PostgreSQLAcquisitionProvider,
    PostgreSQLAcquisitionSettings,
    PostgreSQLDestinationProvider,
    PostgreSQLLandStore,
    PostgreSQLLandStoreSettings,
    PostgreSQLSourceObjectDeclaration,
)
from heinzel_provider_sdk import (
    AcquisitionField,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    RawGenerationTarget,
    SourceObservationRequest,
    StagedSegment,
    acquisition_intent_key,
    staged_segment_digest,
)
from heinzel_runtime import GenerationLedger, LandingResult, LandingRunner
from pydantic import SecretStr

from .warehouse import DEMO_MAX_WRITE_TRANSACTION_DURATION, SOURCE_SCHEMA, SOURCE_TABLE

__all__ = [
    "DEMO_LOGICAL_OBJECT",
    "DEMO_RAW_TABLE",
    "AcquiredRows",
    "acquire_demo_rows",
    "land_demo_rows",
]

# The logical object the contract's mapping names, so the landed relation and the semantic
# version describe the same thing.
DEMO_LOGICAL_OBJECT = "customer_orders"

# The source binding and run identities the demonstration acquires under. They are fixed
# because the demonstration is one deterministic run: a generated identity would make the
# landed receipt differ between two starts of the same container.
_SOURCE_BINDING_REF = "source-demo-orders"
_RUN_INTENT_REF = "1" * 64
DEMO_RAW_TABLE = "raw_customer_orders"

# The schema the source is approved against. A field acquisition reads that is not here is
# refused by the provider before PostgreSQL is asked, and a field here the grant omits is
# refused by PostgreSQL -- two independent boundaries over the same list.
_APPROVED_FIELDS = (
    AcquisitionField(name="order_id", value_type="integer", nullable=False),
    AcquisitionField(name="customer_id", value_type="integer", nullable=False),
    AcquisitionField(name="ordered_on", value_type="string", nullable=False),
    AcquisitionField(name="order_total", value_type="decimal", nullable=False),
    AcquisitionField(name="updated_at", value_type="timestamp", nullable=False),
)


@dataclass(frozen=True, slots=True)
class AcquiredRows:
    """What one acquisition produced, with the watermark it measured."""

    rows: tuple[bytes, ...]
    watermark_at: datetime
    observed_at: datetime


def acquire_demo_rows(acquisition_dsn: str, *, tenant_id: str) -> AcquiredRows:
    """Read the approved source columns through the PostgreSQL acquisition provider."""
    approved_schema = AcquisitionObjectSchema(
        logical_object_ref=DEMO_LOGICAL_OBJECT,
        schema_digest=digest(_APPROVED_FIELDS),
        fields=_APPROVED_FIELDS,
        record_key_fields=("order_id",),
        source_updated_at_field="updated_at",
    )
    provider = PostgreSQLAcquisitionProvider(
        PostgreSQLAcquisitionSettings(
            dsn=SecretStr(acquisition_dsn),
            connection_handle="demo-source",
            objects=(
                PostgreSQLSourceObjectDeclaration(
                    logical_object_ref=DEMO_LOGICAL_OBJECT,
                    schema_name=SOURCE_SCHEMA,
                    table_name=SOURCE_TABLE,
                    field_names=tuple(field.name for field in _APPROVED_FIELDS),
                    key_name="order_id",
                    source_updated_at_field="updated_at",
                ),
            ),
            # The schema acquisition must never reach into. The provider proves least
            # privilege against it: it refuses the acquisition unless the connecting role
            # holds no privilege on a relation outside its declaration, so `warehouse.py`
            # creates this schema and a relation in it for the check to have a subject.
            unrelated_schema_name="private_admin",
            max_write_transaction_duration=DEMO_MAX_WRITE_TRANSACTION_DURATION,
        ),
        private_boundary_reference_factory=lambda tenant, reference: (
            f"private://{tenant}/{reference}"
        ),
        # The demonstration writes no private boundary payload: it has nowhere governed to
        # put one, and a destination that silently discarded it would be worse than none.
        private_boundary_writer=lambda _tenant, _reference, _payload: None,
    )
    observation = provider.observe_source(
        SourceObservationRequest(
            tenant_id=tenant_id,
            source_binding_ref=_SOURCE_BINDING_REF,
            object_refs=(DEMO_LOGICAL_OBJECT,),
        )
    )
    intent = AcquisitionIntent(
        intent_key=acquisition_intent_key(
            tenant_id=tenant_id,
            run_intent_ref=_RUN_INTENT_REF,
            contract_digest="2" * 64,
            source_binding_ref=_SOURCE_BINDING_REF,
            acquisition_mode="snapshot",
            object_refs=(DEMO_LOGICAL_OBJECT,),
            prior_checkpoint_revision=0,
        ),
        tenant_id=tenant_id,
        run_intent_ref=_RUN_INTENT_REF,
        contract_ref="contract-orders",
        contract_digest="2" * 64,
        source_binding_ref=_SOURCE_BINDING_REF,
        source_observation_digest=digest(observation),
        acquisition_mode="snapshot",
        object_refs=(DEMO_LOGICAL_OBJECT,),
        prior_checkpoint_revision=0,
        prior_checkpoint_digest=None,
        record_ceiling=100,
        encoded_byte_ceiling=1_000_000,
        admitted_at=datetime.now(UTC),
    )
    session = provider.open_acquisition(intent, (approved_schema,), None)
    records = tuple(session)
    completed = session.complete()

    watermarks = tuple(
        record.source_updated_at for record in records if record.source_updated_at is not None
    )
    if len(watermarks) != len(records):
        raise RuntimeError("a snapshot record arrived without the measured source watermark")
    return AcquiredRows(
        rows=tuple(
            canonical_bytes({field.name: field.value for field in record.fields})
            for record in records
        ),
        watermark_at=max(watermarks),
        observed_at=completed.boundaries[0].closed_at,
    )


async def land_demo_rows(
    landing_dsn: str,
    acquired: AcquiredRows,
    *,
    contract: ManagedIntegrationContract,
    ledger: GenerationLedger,
) -> LandingResult:
    """Land the acquired rows under the demonstration's contract identity.

    The contract is passed rather than rebuilt because the generation ledger is what later
    proves input cardinality, and that resolver requires the landed receipt's contract
    identity to equal the one it resolves against. Two contracts that merely look alike
    would fail there, far from here.
    """
    segment = StagedSegment(
        segment_digest=staged_segment_digest(acquired.rows),
        schema_digest=digest(_APPROVED_FIELDS),
        record_count=len(acquired.rows),
        rows=acquired.rows,
    )
    target = RawGenerationTarget(
        tenant_id=contract.tenant_id,
        contract_ref=contract.contract_id,
        contract_revision=contract.version,
        trigger_window="2026-09-12T00:00:00Z/PT1H",
        destination_binding_ref="destination-demo",
        logical_object_ref=DEMO_LOGICAL_OBJECT,
        table_ref=DEMO_RAW_TABLE,
        schema_digest=segment.schema_digest,
    )
    provider = PostgreSQLDestinationProvider(
        store=PostgreSQLLandStore(
            PostgreSQLLandStoreSettings(
                dsn=SecretStr(landing_dsn),
                raw_schema_name="raw",
                ledger_schema_name="land_control",
                ledger_table_name="land_receipts",
            )
        )
    )
    return await LandingRunner(provider=provider, ledger=ledger).land(
        segment=segment,
        target=target,
        idempotency_key="4" * 64,
        batch_id="7" * 64,
        batch_manifest_digest="5" * 64,
        candidate_checkpoint_digest="6" * 64,
        prior_checkpoint_revision=0,
        contract_digest=digest(contract),
        source_binding_ref=_SOURCE_BINDING_REF,
        consumer_ref="destination-demo",
    )
