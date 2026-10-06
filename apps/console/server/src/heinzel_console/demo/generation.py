"""Acquire the demonstration's source rows and land them as a raw generation.

The governed answer the demonstration serves is asked of a product generation, and a
generation exists only if rows were read from a source and landed under a receipt. This
module does both through the runtime's own governed acquisition rather than by calling a
provider directly: an acquisition contract is activated over an observation of the source,
`AcquisitionApplication` prepares a batch under an intent derived from that contract, and
`AcquisitionLandingCoordinator` lands the prepared segments and acknowledges them. What
comes out is a real `AcquisitionEvidenceReceipt`, a batch manifest, segment artifacts and a
checkpoint, all composed by the services rather than by this demonstration.

Writing `raw.raw_customer_orders` directly would be shorter and would leave the
demonstration with a landed relation no receipt describes -- which is the one thing a
generation is for. The providers connect as the least-privilege roles `warehouse.py`
creates, so a step reaching past its grant fails here exactly as it would elsewhere.

Nothing in this package imports from `tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from heinzel_connection_broker import SourceConnectionBinding, SourceConnectionBindingState
from heinzel_contract_model import ArtifactReference, digest
from heinzel_contract_service import (
    AcquisitionActivationApproval,
    AcquisitionContractLifecycleState,
    ActivatedAcquisitionContract,
    ValidatedSourceBinding,
)
from heinzel_evidence import SQLiteAcquisitionEvidenceWriter
from heinzel_provider_postgresql import (
    PostgreSQLAcquisitionProvider,
    PostgreSQLAcquisitionSettings,
    PostgreSQLDestinationProvider,
    PostgreSQLLandStore,
    PostgreSQLLandStoreSettings,
    PostgreSQLSourceObjectDeclaration,
)
from heinzel_provider_sdk import (
    AcquisitionBatchManifest,
    AcquisitionField,
    AcquisitionObjectSchema,
    AcquisitionRecord,
    AcquisitionSegmentManifest,
    AcquisitionSourceObservation,
    LandReceipt,
    RawGenerationTarget,
    SourceObservationRequest,
)
from heinzel_runtime import (
    AcquisitionDeclaredActivation,
    AcquisitionLandingCoordinator,
    AcquisitionRunner,
    AcquisitionRunPreparation,
    LandingRunner,
    activated_contract_resolver,
    compose_acquisition_application,
    compose_activated_acquisition_contract,
    opaque_reference_factory,
    source_binding_resolver,
)
from heinzel_state import AcquisitionStateNotFoundError, SQLiteAcquisitionStateRepository
from pydantic import SecretStr

from .collaborators import DEMO_ARCHITECT_ID
from .publication import DemoPublication, demo_placeholder_digest
from .stores import DemoStores
from .warehouse import (
    DEMO_MAX_WRITE_TRANSACTION_DURATION,
    SOURCE_SCHEMA,
    SOURCE_TABLE,
    ProvisioningRefused,
)

__all__ = [
    "DEMO_LOGICAL_OBJECT",
    "DEMO_RAW_TABLE",
    "DemoAcquisition",
    "LandedDemoGeneration",
    "PreparedDemoAcquisition",
]

# The logical object the contract's mapping names, so the landed relation and the semantic
# version describe the same thing.
DEMO_LOGICAL_OBJECT = "customer_orders"

DEMO_RAW_TABLE = "raw_customer_orders"

# The source the demonstration acquires under, and the handle the provider connects by. The
# observation the contract is activated over is published under its own reference, because a
# ready binding carries one and the runner resolves the observation through it.
_SOURCE_BINDING_REF = "source-demo-orders"
_SOURCE_OBSERVATION_REF = "source-observation-demo-orders"
_CONNECTION_HANDLE = "demo-source"

# The destination the rows are landed into, and the consumer whose acknowledgement advances
# the source checkpoint. One name because one component does both here:
# `AcquisitionLandingCoordinator` acknowledges under the `consumer_ref` it is given, and
# `AcquisitionRunner.acknowledge` refuses any consumer other than the contract's
# `acknowledgement_consumer_ref` -- so the activated contract below declares this same name.
_DESTINATION_REF = "destination-demo"

# The window the one generation is landed for, and the key the activation is recorded under.
# Both fixed because the demonstration is one deterministic run: `prepare_now` derives the
# run intent reference from the contract record and this window, so a generated window would
# make the landed receipt differ between two starts of the same container.
_TRIGGER_WINDOW = "2026-09-12T00:00:00Z/PT1H"
_ACTIVATION_IDEMPOTENCY_KEY = "activate-demo-orders"

# What one acquisition of this source may read. Policy, not a measurement: the contract
# admits no more, and the provider refuses a batch that would exceed either.
_RECORD_CEILING = 100
_ENCODED_BYTE_CEILING = 1_000_000

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

_APPROVED_SCHEMA = AcquisitionObjectSchema(
    logical_object_ref=DEMO_LOGICAL_OBJECT,
    schema_digest=digest(_APPROVED_FIELDS),
    fields=_APPROVED_FIELDS,
    record_key_fields=("order_id",),
    source_updated_at_field="updated_at",
)


@dataclass(frozen=True, slots=True)
class PreparedDemoAcquisition:
    """One prepared acquisition batch, with what landing and the freshness observation need.

    `watermark_at` is re-derived from the prepared records rather than read off the
    preparation, which does not carry it: it is the latest `source_updated_at` the batch
    holds, so it describes when the source was last written rather than the day its rows
    fall on.
    """

    preparation: AcquisitionRunPreparation
    record_count: int
    watermark_at: datetime


@dataclass(frozen=True, slots=True)
class LandedDemoGeneration:
    """The raw generation one acquisition landed, and the watermark it was measured at."""

    receipt: LandReceipt
    generation_key: str
    watermark_at: datetime


class _DemoSourceBindingReader:
    """Serves the demonstration's one source binding, as connection-broker would.

    The binding is constructed rather than driven through `SQLiteSourceBindingRepository`,
    because reaching `READY` there requires a private source capability and the two-probe
    validation evidence a broker records -- neither of which this demonstration has, and
    neither of which it may invent. So the binding this demonstration stands behind is
    assembled here and served from the one read the acquisition runtime makes of a broker.
    """

    def __init__(self, binding: SourceConnectionBinding) -> None:
        self._binding = binding

    def load(self, tenant_id: str, binding_id: str) -> SourceConnectionBinding:
        if tenant_id != self._binding.tenant_id or binding_id != self._binding.binding_id:
            raise KeyError("the demonstration has no such source binding")
        return self._binding


class DemoAcquisition:
    """The demonstration's governed acquisition, composed over its own stores.

    One object rather than two functions because observing the source, activating a contract
    over that observation and preparing a batch under it must all happen in one process.
    `PostgreSQLAcquisitionProvider` stamps a wall-clock `observed_at` into every observation,
    nothing anywhere durably holds an `AcquisitionSourceObservation`, and the activated
    contract pins `source_observation_digest` -- so a contract activated by an earlier start
    can never be satisfied by a fresh observation, and the runner refuses the preparation
    with `source_observation_authority_mismatch`. A later start must therefore not reach
    acquisition at all, which is what the landed-generation record in `DemoStores` is for.

    Constructing this reads the source and writes the activation, so it is not a cheap
    object: it is the first half of one acquisition, not a handle to the capability.
    """

    def __init__(
        self,
        acquisition_dsn: str,
        *,
        publication: DemoPublication,
        stores: DemoStores,
        clock: Callable[[], datetime],
    ) -> None:
        contract = publication.contract
        state = _require_acquisition_state(stores)
        _refuse_a_second_acquisition(
            stores,
            tenant_id=contract.tenant_id,
            contract_ref=contract.contract_id,
            contract_digest=digest(contract),
            state=state,
        )
        self._stores = stores
        self._tenant_id = contract.tenant_id
        self._contract_ref = contract.contract_id
        self._contract_revision = contract.version
        provider = _acquisition_provider(acquisition_dsn)
        observation = provider.observe_source(
            SourceObservationRequest(
                tenant_id=self._tenant_id,
                source_binding_ref=_SOURCE_BINDING_REF,
                object_refs=(DEMO_LOGICAL_OBJECT,),
            )
        )
        activated_at = clock()
        binding = _source_binding(tenant_id=self._tenant_id, observed_at=activated_at)
        activated = _activated_contract(
            publication=publication,
            binding=binding,
            observation=observation,
            activated_at=activated_at,
        )
        stores.acquisition_lifecycle.activate_contract(
            idempotency_key=_ACTIVATION_IDEMPOTENCY_KEY,
            contract=activated,
            approval=AcquisitionActivationApproval(
                tenant_id=self._tenant_id,
                process_package_ref=activated.process_package_ref,
                product_intent_ref=activated.product_intent_ref,
                destination_product_ref=activated.destination_product_ref,
                approved_by=activated.activated_by,
                approved_at=activated_at,
            ),
            source_validation=ValidatedSourceBinding(
                tenant_id=self._tenant_id,
                source_binding_ref=binding.binding_id,
                source_binding_revision=binding.revision,
                credential_revision=binding.credential_revision,
                capability_profile_digest=activated.capability_profile_digest,
                source_observation_ref=activated.source_observation_ref,
                source_observation_digest=activated.source_observation_digest,
                validated_at=activated_at,
            ),
        )
        # The state store keeps its own record of which contracts may acquire, and
        # `admit_authority` refuses a contract it has never been told about.
        state.activate_contract_authority(self._tenant_id, activated.contract_digest)

        bindings = _DemoSourceBindingReader(binding)

        def resolve_observation(tenant_id: str, reference: str) -> AcquisitionSourceObservation:
            if (tenant_id, reference) != (self._tenant_id, _SOURCE_OBSERVATION_REF):
                raise KeyError("the demonstration has no such source observation")
            return observation

        def resolve_provider(resolved: SourceConnectionBinding) -> PostgreSQLAcquisitionProvider:
            if resolved.binding_id != _SOURCE_BINDING_REF:
                raise KeyError("the demonstration has no provider for that source binding")
            return provider

        evidence_writer = SQLiteAcquisitionEvidenceWriter(stores.acquisition_evidence)
        references = opaque_reference_factory()
        self._application = compose_acquisition_application(
            contract_repository=stores.acquisition_lifecycle,
            binding_repository=bindings,
            observation_resolver=resolve_observation,
            provider_resolver=resolve_provider,
            state_store=state,
            artifact_store=stores.acquisition_artifacts,
            evidence_writer=evidence_writer,
            reference_factory=references,
            clock=clock,
        )
        # The composed application prepares and nothing more. Acknowledging is the runner's
        # role, and the landing coordinator consumes it, so a second runner over these same
        # stores is composed here to serve it.
        self._acknowledger = AcquisitionRunner(
            binding_resolver=source_binding_resolver(bindings),
            contract_resolver=lambda tenant_id, contract_ref: (
                activated_contract_resolver(stores.acquisition_lifecycle)(
                    tenant_id, contract_ref
                ).contract
            ),
            observation_resolver=resolve_observation,
            provider_resolver=resolve_provider,
            state_store=state,
            artifact_store=stores.acquisition_artifacts,
            evidence_writer=evidence_writer,
            reference_factory=references,
            clock=clock,
        )

    def prepare(self) -> PreparedDemoAcquisition:
        """Prepare one snapshot batch under the activated contract.

        `prepare_now` rather than `run_now`, because landing binds its targets to the intent
        the batch was admitted under and `run_now` discards it.
        """
        preparation = self._application.prepare_now(
            tenant_id=self._tenant_id,
            contract_ref=self._contract_ref,
            trigger_window=_TRIGGER_WINDOW,
            acquisition_mode="snapshot",
        )
        manifest = preparation.result.batch_manifest
        if manifest is None:
            # Not a failure: the acquisition was governed to a `No Valid Plan` or a
            # resynchronization instead of a batch. The reason codes are the receipt's own
            # public ones, so naming them here discloses nothing the evidence does not.
            evidence = preparation.result.evidence
            raise ProvisioningRefused(
                "the demonstration's acquisition prepared no batch to land: "
                f"{evidence.outcome}, {', '.join(evidence.reason_codes) or 'no reason given'}. "
                "Discard the state directory and the warehouse together with "
                "`docker compose down -v`."
            )
        return PreparedDemoAcquisition(
            preparation=preparation,
            record_count=manifest.total_record_count,
            watermark_at=self._watermark(manifest),
        )

    async def land(
        self, prepared: PreparedDemoAcquisition, *, landing_dsn: str
    ) -> LandedDemoGeneration:
        """Land the prepared segments and acknowledge them, advancing the source checkpoint."""
        destination = PostgreSQLDestinationProvider(
            store=PostgreSQLLandStore(
                PostgreSQLLandStoreSettings(
                    dsn=SecretStr(landing_dsn),
                    raw_schema_name="raw",
                    ledger_schema_name="land_control",
                    ledger_table_name="land_receipts",
                )
            )
        )
        coordinator = AcquisitionLandingCoordinator(
            artifact_store=self._stores.acquisition_artifacts,
            landing=LandingRunner(provider=destination, ledger=self._stores.generations),
            target_resolver=self._landing_target,
            acknowledger=self._acknowledger,
            consumer_ref=_DESTINATION_REF,
        )
        landed = await coordinator.land_and_acknowledge(
            intent=prepared.preparation.intent,
            preparation=prepared.preparation.result,
        )
        if len(landed.landings) != 1:
            raise ProvisioningRefused(
                "the demonstration landed something other than its one segment, so no single "
                "generation describes it. Discard the state directory and the warehouse "
                "together with `docker compose down -v`."
            )
        landing = landed.landings[0]
        return LandedDemoGeneration(
            receipt=landing.receipt,
            generation_key=landing.generation_key,
            watermark_at=prepared.watermark_at,
        )

    def _landing_target(self, manifest: AcquisitionSegmentManifest) -> RawGenerationTarget:
        """Where one prepared segment lands.

        The coordinator re-derives the target for every segment and refuses one that does not
        agree with the intent, so this names the intent's own identities rather than a second
        copy of them.
        """
        return RawGenerationTarget(
            tenant_id=self._tenant_id,
            contract_ref=self._contract_ref,
            contract_revision=self._contract_revision,
            trigger_window=_TRIGGER_WINDOW,
            destination_binding_ref=_DESTINATION_REF,
            logical_object_ref=manifest.logical_object_ref,
            table_ref=DEMO_RAW_TABLE,
            schema_digest=manifest.record_schema_digest,
        )

    def _watermark(self, manifest: AcquisitionBatchManifest) -> datetime:
        """The latest `source_updated_at` the prepared batch holds.

        Read back from the segment artifacts the preparation published, because the
        preparation itself carries no watermark and the records are not otherwise kept. The
        artifact store verifies each payload against the digest the manifest names, so this
        reads what was prepared rather than whatever is on disk.
        """
        watermarks: list[datetime] = []
        records = 0
        for segment in manifest.segment_manifests:
            reader = self._stores.acquisition_artifacts.open_verified(
                tenant_id=self._tenant_id, artifact_digest=segment.content_digest
            )
            try:
                payload = reader.read()
            finally:
                reader.close()
            for line in payload.splitlines():
                record = AcquisitionRecord.model_validate_json(line, strict=True)
                records += 1
                if record.source_updated_at is not None:
                    watermarks.append(record.source_updated_at)
        if not watermarks or len(watermarks) != records:
            raise ProvisioningRefused(
                "a snapshot record arrived without the measured source watermark, so how "
                "current the demonstration's product is cannot be stated. Discard the state "
                "directory and the warehouse together with `docker compose down -v`."
            )
        return max(watermarks)


def _acquisition_provider(acquisition_dsn: str) -> PostgreSQLAcquisitionProvider:
    return PostgreSQLAcquisitionProvider(
        PostgreSQLAcquisitionSettings(
            dsn=SecretStr(acquisition_dsn),
            connection_handle=_CONNECTION_HANDLE,
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


def _source_binding(*, tenant_id: str, observed_at: datetime) -> SourceConnectionBinding:
    """The ready source binding the demonstration acquires under.

    `capability_profile_digest` is derived from a purpose label rather than from a capability
    profile, because the demonstration runs no connection broker and so has no profile to
    hash. A ready binding must carry one, so this names what it is instead of pretending to
    be the real thing -- the same posture the rest of the demonstration's placeholders take.
    """
    return SourceConnectionBinding(
        binding_id=_SOURCE_BINDING_REF,
        tenant_id=tenant_id,
        provider_kind="postgresql",
        connection_handle=_CONNECTION_HANDLE,
        account_mode="not_applicable",
        lifecycle_state=SourceConnectionBindingState.READY,
        approved_object_refs=(DEMO_LOGICAL_OBJECT,),
        capability_profile_digest=demo_placeholder_digest("source-capability-profile"),
        source_observation_ref=_SOURCE_OBSERVATION_REF,
        credential_revision=1,
        revision=1,
        created_at=observed_at,
        updated_at=observed_at,
    )


def _activated_contract(
    *,
    publication: DemoPublication,
    binding: SourceConnectionBinding,
    observation: AcquisitionSourceObservation,
    activated_at: datetime,
) -> ActivatedAcquisitionContract:
    """Compose the acquisition contract this demonstration activates over its source.

    `contract_digest` is `digest(publication.contract)` and `contract_ref` is that contract's
    own identifier, which is a coupling worth stating. The acquisition contract's digest is
    caller-chosen, and `AcquisitionLandingCoordinator` passes it into the acknowledgement it
    records in the generation ledger. `ProductInputCardinalityResolver.resolve` later refuses
    a generation whose acknowledgement carries a `contract_digest` other than
    `digest(contract)` over the `ManagedIntegrationContract` -- so any other value here would
    let the demonstration land and materialize and then refuse to answer, far from here.

    `product_intent_ref` is a placeholder: the demonstration's question is approved in the
    browser after startup, so at activation there is no approved product intent to name. The
    activation therefore goes through contract-service's repository rather than
    `ProductIntentBoundActivationService`, which would check that reference against a real
    approval. That is a limit of running the activation before the journey, not something
    this demonstration can state more honestly.
    """
    contract = publication.contract
    return compose_activated_acquisition_contract(
        # Composed rather than read back, because nothing reads the lifecycle table on this
        # path: `activated_contract_resolver` resolves through `list_contracts`, which is
        # written by `activate_contract` below.
        lifecycle=AcquisitionContractLifecycleState(
            tenant_id=contract.tenant_id,
            contract_digest=digest(contract),
            revision=1,
            lifecycle_state="activated",
            activated_at=activated_at,
            retired_at=None,
        ),
        binding=binding,
        observation=observation,
        declared=AcquisitionDeclaredActivation(
            contract_ref=contract.contract_id,
            process_package_ref=publication.semantic_version.process_package_ref,
            product_intent_ref=ArtifactReference(
                artifact_id="product-intent-orders-daily",
                version=1,
                digest=demo_placeholder_digest("product-intent"),
            ),
            destination_product_ref=contract.destination_product.product_name,
            acknowledgement_consumer_ref=_DESTINATION_REF,
            object_schemas=(_APPROVED_SCHEMA,),
            record_ceiling=_RECORD_CEILING,
            encoded_byte_ceiling=_ENCODED_BYTE_CEILING,
            activated_by=DEMO_ARCHITECT_ID,
            activated_at=activated_at,
        ),
    )


def _require_acquisition_state(stores: DemoStores) -> SQLiteAcquisitionStateRepository:
    """The store that holds the source cursor, or a refusal naming why there is none."""
    state = stores.acquisition_state
    if state is None:
        raise ProvisioningRefused(
            "the demonstration's acquisition state store was not opened, because it was given "
            "no cursor cipher and a source cursor may not be kept unencrypted. There is "
            "nothing to acquire against."
        )
    return state


def _refuse_a_second_acquisition(
    stores: DemoStores,
    *,
    tenant_id: str,
    contract_ref: str,
    contract_digest: str,
    state: SQLiteAcquisitionStateRepository,
) -> None:
    """Refuse an acquisition this state directory has already made, with what to do about it.

    Acknowledging a landed batch advances the source checkpoint to revision 1, and a snapshot
    is admitted only from revision 0 -- so the provider refuses the second attempt with
    `permanent_configuration`, and the state store refuses to replay an acknowledged
    preparation as pending. A second activation is refused too, because an activated
    contract's digest is unique per tenant and this demonstration's is fixed. All three report
    themselves in the vocabulary of the component that refused, which names nothing an
    operator can act on, so the demonstration says what happened instead.

    Reaching here at all means the record of what was landed was lost while the stores it
    points into were kept, so the two are discarded together.
    """
    try:
        state.load_checkpoint(tenant_id, contract_digest, _SOURCE_BINDING_REF)
    except AcquisitionStateNotFoundError:
        pass
    else:
        raise ProvisioningRefused(
            "the demonstration has already acquired and acknowledged its source, so its "
            "checkpoint stands where no snapshot may be taken from, and the record of what "
            "it landed is gone. Discard the state directory and the warehouse together with "
            "`docker compose down -v`."
        )
    if any(
        record.contract_ref == contract_ref
        for record in stores.acquisition_lifecycle.list_contracts(tenant_id)
    ):
        raise ProvisioningRefused(
            "an earlier start activated the demonstration's acquisition contract and did not "
            "finish landing. That contract admits only the source observation it was "
            "activated over, which no later start can reproduce. Discard the state directory "
            "and the warehouse together with `docker compose down -v`."
        )
