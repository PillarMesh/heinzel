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
from typing import Protocol

from heinzel_connection_broker import SourceConnectionBinding, SourceConnectionBindingState
from heinzel_contract_model import ArtifactReference, ManagedIntegrationContract, digest
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
from heinzel_provider_sdk.acquisition_models import AcquisitionMode
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
    "DEMO_SOURCE_CONNECTION_HANDLE",
    "DemoAcquisition",
    "DemoSourceBindingReader",
    "LandedDemoAcquisition",
    "LandedDemoGeneration",
    "PreparedDemoAcquisition",
    "demo_source_acquisition_settings",
    "ensure_activated_demo_contract",
]

# The logical object the contract's mapping names, so the landed relation and the semantic
# version describe the same thing.
DEMO_LOGICAL_OBJECT = "customer_orders"

DEMO_RAW_TABLE = "raw_customer_orders"

# The handle the provider connects by. The binding itself is no longer named here: it is the
# one the connection broker registered under this handle, so its identifier is derived from the
# tenant, the provider and the handle by the broker rather than chosen by the demonstration.
DEMO_SOURCE_CONNECTION_HANDLE = "demo-source"

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
    trigger_window: str


@dataclass(frozen=True, slots=True)
class LandedDemoAcquisition:
    """What one acquisition landed, and the acknowledgement that closed it.

    `receipt` and `generation_key` are `None` where the batch carried no records. An
    incremental run of a source nothing has written to since the last one prepares a batch of
    nothing, which has no segment to land and so describes no generation. The acknowledgement
    commits either way, which is how the checkpoint records that the source was read and had
    nothing new -- and it is why this is not a failure.

    `checkpoint_receipt_id` is that acknowledgement. The runtime writes its own
    `acknowledged` evidence receipt naming it and does not hand it back, so a caller that
    wants that receipt reads the evidence store for the one this identifies.
    """

    receipt: LandReceipt | None
    generation_key: str | None
    checkpoint_receipt_id: str


@dataclass(frozen=True, slots=True)
class LandedDemoGeneration:
    """A landed acquisition with the watermark its records were measured at.

    The watermark is measured from the prepared records, so it exists only where a caller
    measured them. The demonstration's first acquisition does, because the freshness
    observation it composes afterwards needs one; a commanded run does not, because a batch
    that carried no records has no watermark to measure and is still a run that happened.
    """

    receipt: LandReceipt
    generation_key: str
    watermark_at: datetime
    checkpoint_receipt_id: str


class DemoSourceBindingReader(Protocol):
    """The one read the acquisition runtime makes of a connection broker.

    `SQLiteSourceBindingRepository` satisfies it directly, which is the point: the binding the
    acquisition runs under is read out of the broker's own register, at the revision the broker
    committed it at, rather than assembled by this demonstration and served from memory.
    """

    def load(self, tenant_id: str, binding_id: str) -> SourceConnectionBinding: ...


class DemoAcquisition:
    """The demonstration's governed acquisition, composed over its own stores.

    A handle to the capability rather than one run of it: constructing this reads nothing
    from the source and writes nothing, so a start that acquired long ago can still compose
    one and run another acquisition under the contract an earlier start activated. That is
    what `ensure_activated_demo_contract` is separate for -- activating is a single act and
    belongs to whoever performs it once, while preparing and landing is repeatable and
    belongs to whoever commands a run.

    The contract has to be activated already, and the reading it was activated over has to
    be held, because `resolve_observation` answers the runner from the store. Both are
    checked here rather than left to the runner, whose refusal names a composition.
    """

    def __init__(
        self,
        acquisition_dsn: str,
        *,
        binding: SourceConnectionBinding,
        bindings: DemoSourceBindingReader,
        publication: DemoPublication,
        stores: DemoStores,
        clock: Callable[[], datetime],
    ) -> None:
        contract = publication.contract
        source_observation_ref = _admissible_source_binding(binding, contract=contract)
        state = _require_acquisition_state(stores)
        if not _contract_is_activated(
            stores, tenant_id=contract.tenant_id, contract_ref=contract.contract_id
        ):
            raise ProvisioningRefused(
                "the demonstration's acquisition contract is not activated, so there is "
                "nothing to acquire under. Activating it is a separate act; see "
                "`ensure_activated_demo_contract`."
            )
        _held_observation(
            stores,
            tenant_id=contract.tenant_id,
            source_observation_ref=source_observation_ref,
        )
        self._stores = stores
        self._tenant_id = contract.tenant_id
        self._contract_ref = contract.contract_id
        self._contract_revision = contract.version
        provider = _acquisition_provider(acquisition_dsn)

        def resolve_observation(tenant_id: str, reference: str) -> AcquisitionSourceObservation:
            # The store is the only route to a reading, for every run and every start.
            # Nothing here resolves from what some process happened to observe, so a start
            # that stored a reading proves on its own next run that the store returns it.
            stored = stores.source_observations.read(tenant_id=tenant_id, observation_ref=reference)
            if tenant_id != self._tenant_id or stored is None:
                raise KeyError("the demonstration has no such source observation")
            return stored

        def resolve_provider(resolved: SourceConnectionBinding) -> PostgreSQLAcquisitionProvider:
            if resolved.binding_id != binding.binding_id:
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

    def prepare_run(
        self,
        *,
        acquisition_mode: AcquisitionMode = "snapshot",
        trigger_window: str = _TRIGGER_WINDOW,
    ) -> AcquisitionRunPreparation:
        """Prepare one batch under the activated contract, whatever it comes to.

        `prepare_now` rather than `run_now`, because landing binds its targets to the intent
        the batch was admitted under and `run_now` discards it.

        A preparation that carries no batch manifest comes back as it is rather than as a
        refusal, because for a commanded run that is an outcome and not a failure: the
        runtime governs a run to `No Valid Plan` or to a resynchronization, and the receipt
        says which. A batch of no records is a different and also ordinary outcome -- it has
        a manifest and no segment -- and `land_run` takes it. `prepare` is where a caller
        that must have records demands them.
        """
        return self._application.prepare_now(
            tenant_id=self._tenant_id,
            contract_ref=self._contract_ref,
            trigger_window=trigger_window,
            acquisition_mode=acquisition_mode,
        )

    def measure(
        self, preparation: AcquisitionRunPreparation, *, trigger_window: str = _TRIGGER_WINDOW
    ) -> PreparedDemoAcquisition:
        """Measure a prepared batch for landing, or refuse a preparation that has none."""
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
            trigger_window=trigger_window,
        )

    def prepare(
        self,
        *,
        acquisition_mode: AcquisitionMode = "snapshot",
        trigger_window: str = _TRIGGER_WINDOW,
    ) -> PreparedDemoAcquisition:
        """Prepare one batch and insist that there is one to land."""
        return self.measure(
            self.prepare_run(acquisition_mode=acquisition_mode, trigger_window=trigger_window),
            trigger_window=trigger_window,
        )

    async def land(
        self, prepared: PreparedDemoAcquisition, *, landing_dsn: str
    ) -> LandedDemoGeneration:
        """Land a measured batch, keeping the watermark its records were measured at.

        A measured batch has records, because `measure` refuses one with no manifest and the
        watermark is derived from the records themselves -- so unlike `land_run` this insists
        on the one generation the demonstration's product is built from.
        """
        landed = await self.land_run(
            prepared.preparation,
            trigger_window=prepared.trigger_window,
            landing_dsn=landing_dsn,
        )
        if landed.receipt is None or landed.generation_key is None:
            raise ProvisioningRefused(
                "the demonstration's acquisition landed no segment, so no generation "
                "describes what it read. Discard the state directory and the warehouse "
                "together with `docker compose down -v`."
            )
        return LandedDemoGeneration(
            receipt=landed.receipt,
            generation_key=landed.generation_key,
            watermark_at=prepared.watermark_at,
            checkpoint_receipt_id=landed.checkpoint_receipt_id,
        )

    async def land_run(
        self,
        preparation: AcquisitionRunPreparation,
        *,
        trigger_window: str,
        landing_dsn: str,
    ) -> LandedDemoAcquisition:
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
            target_resolver=lambda manifest: self._landing_target(
                manifest, trigger_window=trigger_window
            ),
            acknowledger=self._acknowledger,
            consumer_ref=_DESTINATION_REF,
        )
        landed = await coordinator.land_and_acknowledge(
            intent=preparation.intent,
            preparation=preparation.result,
        )
        if len(landed.landings) > 1:
            # The contract names one logical object, so one segment is the most a batch of it
            # can carry. More would mean a generation nothing here can name.
            raise ProvisioningRefused(
                "the demonstration landed more than its one segment, so no single generation "
                "describes it. Discard the state directory and the warehouse together with "
                "`docker compose down -v`."
            )
        landing = landed.landings[0] if landed.landings else None
        return LandedDemoAcquisition(
            receipt=None if landing is None else landing.receipt,
            generation_key=None if landing is None else landing.generation_key,
            checkpoint_receipt_id=landed.checkpoint_receipt.checkpoint_receipt_id,
        )

    def _landing_target(
        self, manifest: AcquisitionSegmentManifest, *, trigger_window: str
    ) -> RawGenerationTarget:
        """Where one prepared segment lands.

        The coordinator re-derives the target for every segment and refuses one that does not
        agree with the intent, so this names the intent's own identities rather than a second
        copy of them. The window is one of those identities: the generation key is derived
        from it, so two runs of the same window would land as the same generation.
        """
        return RawGenerationTarget(
            tenant_id=self._tenant_id,
            contract_ref=self._contract_ref,
            contract_revision=self._contract_revision,
            trigger_window=trigger_window,
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


def ensure_activated_demo_contract(
    acquisition_dsn: str,
    *,
    binding: SourceConnectionBinding,
    publication: DemoPublication,
    stores: DemoStores,
    clock: Callable[[], datetime],
) -> None:
    """Activate the contract the demonstration acquires under, once, and keep what it read.

    Separate from `DemoAcquisition` because the two have different lifetimes. Activating
    observes the source and writes an activation, and may happen only once for a contract.
    Acquiring under it happens as often as a run is commanded. Holding both in a constructor
    made the object the first half of one acquisition rather than a handle to the capability,
    and so made a second acquisition unreachable.

    Called again over an activation this already performed, it does nothing: an activated
    contract whose reading is still held is what it was asked to produce.
    """
    contract = publication.contract
    tenant_id = contract.tenant_id
    source_observation_ref = _admissible_source_binding(binding, contract=contract)
    state = _require_acquisition_state(stores)
    if (
        _resumable_activation(
            stores,
            tenant_id=tenant_id,
            contract_ref=contract.contract_id,
            contract_digest=digest(contract),
            source_binding_ref=binding.binding_id,
            source_observation_ref=source_observation_ref,
            state=state,
        )
        is not None
    ):
        return
    # Observed under the broker's binding identifier, because the activation refuses an
    # observation of any other binding -- and because the receipt an acquisition under this
    # contract leaves behind then names the binding an architect can read in the console.
    observation = _acquisition_provider(acquisition_dsn).observe_source(
        SourceObservationRequest(
            tenant_id=tenant_id,
            source_binding_ref=binding.binding_id,
            object_refs=(DEMO_LOGICAL_OBJECT,),
        )
    )
    activated_at = clock()
    # Stored before the contract that pins its digest is activated. The other order would
    # leave a start that died between the two with an activated contract whose observation
    # nothing holds, which is the state this store exists to prevent.
    stores.source_observations.store(
        observation_ref=source_observation_ref, observation=observation
    )
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
            tenant_id=tenant_id,
            process_package_ref=activated.process_package_ref,
            product_intent_ref=activated.product_intent_ref,
            destination_product_ref=activated.destination_product_ref,
            approved_by=activated.activated_by,
            approved_at=activated_at,
        ),
        source_validation=ValidatedSourceBinding(
            tenant_id=tenant_id,
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
    state.activate_contract_authority(tenant_id, activated.contract_digest)


def _admissible_source_binding(
    binding: SourceConnectionBinding, *, contract: ManagedIntegrationContract
) -> str:
    """The probed observation reference of a binding this demonstration may acquire under.

    The reference is what the probe published when it validated the binding. An activation
    pins it as the authority the acquisition runs under, and pins `digest(observation)`
    beside it as what was actually read -- two different facts, which is why the observation
    resolver answers for the probe's reference with the reading the activation was over.
    """
    if binding.tenant_id != contract.tenant_id:
        raise ProvisioningRefused(
            "the demonstration's source binding belongs to another tenant than its contract"
        )
    if binding.lifecycle_state is not SourceConnectionBindingState.READY:
        # Reachable only through a caller that did not register before acquiring. Named
        # rather than left to `compose_activated_acquisition_contract`, whose refusal is a
        # reason code about a composition and says nothing about what to do.
        raise ProvisioningRefused(
            "the demonstration's source binding is not ready, so there is no validated "
            "capability authority to acquire under. Register the source first."
        )
    source_observation_ref = binding.source_observation_ref
    if source_observation_ref is None:
        raise ProvisioningRefused(
            "the demonstration's source binding is ready and carries no probed "
            "observation, which the broker's own model does not permit"
        )
    return source_observation_ref


def _contract_is_activated(stores: DemoStores, *, tenant_id: str, contract_ref: str) -> bool:
    return any(
        record.contract_ref == contract_ref
        for record in stores.acquisition_lifecycle.list_contracts(tenant_id)
    )


def _held_observation(
    stores: DemoStores, *, tenant_id: str, source_observation_ref: str
) -> AcquisitionSourceObservation:
    """The reading an activated contract was activated over, or a refusal saying it is gone.

    An activated contract pins `digest(observation)` and admits no other reading, so a
    contract whose reading is not held can never be satisfied again. That is a state
    directory written before this store existed, or one it was deleted from.
    """
    held = stores.source_observations.read(
        tenant_id=tenant_id, observation_ref=source_observation_ref
    )
    if held is None:
        raise ProvisioningRefused(
            "the demonstration's acquisition contract is activated and the source "
            "observation it was activated over is not held. That contract admits no other, "
            "so no later start can satisfy it. Discard the state directory and the "
            "warehouse together with `docker compose down -v`."
        )
    return held


def demo_source_acquisition_settings(
    connection_handle: str, dsn: SecretStr
) -> PostgreSQLAcquisitionSettings:
    """The declaration this demonstration acquires its one source object under.

    Taken as `(handle, dsn)` rather than read from anywhere, because that is the shape a
    capability authority composes settings in: the broker resolves a reference pair into a
    connection detail and asks the deployment what it declared for that handle. The acquisition
    provider and the source capability probe are built from this one function, so a binding the
    probe admits is a binding the acquisition can read -- if they declared different objects, a
    probe could pass on a source the acquisition is then refused.
    """
    return PostgreSQLAcquisitionSettings(
        dsn=dsn,
        connection_handle=connection_handle,
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
    )


def _acquisition_provider(acquisition_dsn: str) -> PostgreSQLAcquisitionProvider:
    return PostgreSQLAcquisitionProvider(
        demo_source_acquisition_settings(DEMO_SOURCE_CONNECTION_HANDLE, SecretStr(acquisition_dsn)),
        private_boundary_reference_factory=lambda tenant, reference: (
            f"private://{tenant}/{reference}"
        ),
        # The demonstration writes no private boundary payload: it has nowhere governed to
        # put one, and a destination that silently discarded it would be worse than none.
        private_boundary_writer=lambda _tenant, _reference, _payload: None,
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


def _resumable_activation(
    stores: DemoStores,
    *,
    tenant_id: str,
    contract_ref: str,
    contract_digest: str,
    source_binding_ref: str,
    source_observation_ref: str,
    state: SQLiteAcquisitionStateRepository,
) -> AcquisitionSourceObservation | None:
    """Whether this contract still needs activating, is already activated, or is past saving.

    `None` means nothing of this contract is on record, so observing and activating is the
    thing to do. An observation means it was activated already and the reading it was
    activated over is still held, so there is nothing to do.

    Resuming an activation used to be impossible and the refusal said so. An activated
    contract pins `digest(observation)`, the provider stamps a wall clock into every reading,
    and nothing held the reading -- so the contract admitted only an observation that no later
    process could reproduce, and the only way out was discarding the state directory. Now the
    reading is kept, and the window between activating and landing is a window a start can
    come back into. That window is not rare: the materialization between them runs dbt.

    One situation refuses: an advanced source checkpoint. Acknowledging a landed batch moves
    it off revision 0, and a snapshot is admitted only from revision 0, so the first
    acquisition under this contract has already been taken and acknowledged. Reaching here
    means the record of what it landed was lost while the stores it points into were kept, so
    the two go together. A commanded run after that one does not come through here at all:
    it acquires under this activation rather than seeking another.

    It reports itself in the vocabulary of whichever component refuses next, which names
    nothing an operator can act on, so this says what happened instead.
    """
    try:
        state.load_checkpoint(tenant_id, contract_digest, source_binding_ref)
    except AcquisitionStateNotFoundError:
        pass
    else:
        raise ProvisioningRefused(
            "the demonstration has already acquired and acknowledged its source, so its "
            "checkpoint stands where no snapshot may be taken from, and the record of what "
            "it landed is gone. Discard the state directory and the warehouse together with "
            "`docker compose down -v`."
        )
    if not _contract_is_activated(stores, tenant_id=tenant_id, contract_ref=contract_ref):
        return None
    return _held_observation(
        stores, tenant_id=tenant_id, source_observation_ref=source_observation_ref
    )
