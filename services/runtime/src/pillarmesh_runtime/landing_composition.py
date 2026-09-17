"""Compose LAND for an acquired batch from contract authority and a deployment's routing.

`AcquisitionLandingCoordinator` takes a target resolver and a consumer reference. Until now only
tests supplied them, so nothing in the product decided where an acquired object lands or which
consumer acknowledges it. This composition derives the consumer and the contract revision from
contract-service's activated record, and takes the destination binding and per-object table from
an explicit, validated routing.

Routing is a deployment input, not an authority: no owning service yet binds an acquisition
contract to a warehouse binding or to raw table names. It is stated here as one strict value that
must name the same tenant and contract the run executes under, and must cover every object in the
batch, so a partially routed batch is refused before anything lands.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Self

from pillarmesh_contract_model import ArtifactModel
from pillarmesh_provider_sdk import (
    AcquisitionIntent,
    AcquisitionSegmentManifest,
    RawGenerationTarget,
)
from pydantic import Field, model_validator

from .acquisition import AcquisitionPreparationResult
from .acquisition_composition import ActivatedContractReader, activated_contract_resolver
from .acquisition_errors import AcquisitionContractError
from .acquisition_landing import (
    AcquisitionAcknowledger,
    AcquisitionLanding,
    AcquisitionLandingCoordinator,
    AcquisitionLandingResult,
    AcquisitionSegmentArtifactStore,
)

type _TableRef = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]


class LandingContractAuthority(ArtifactModel):
    """The facts LAND needs from contract-service's activated record for this contract."""

    tenant_id: str = Field(min_length=1)
    contract_ref: str = Field(min_length=1)
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    acknowledgement_consumer_ref: str = Field(min_length=1)


class AcquisitionObjectRoute(ArtifactModel):
    logical_object_ref: str = Field(min_length=1)
    table_ref: _TableRef


class AcquisitionDestinationRouting(ArtifactModel):
    """Where one contract's acquired objects land, stated by the deployment."""

    tenant_id: str = Field(min_length=1)
    contract_ref: str = Field(min_length=1)
    destination_binding_ref: str = Field(min_length=1)
    routes: tuple[AcquisitionObjectRoute, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def routes_name_each_object_once(self) -> Self:
        object_refs = tuple(route.logical_object_ref for route in self.routes)
        if len(object_refs) != len(set(object_refs)):
            raise ValueError("each logical object may be routed only once")
        return self

    def table_ref(self, logical_object_ref: str) -> str | None:
        for route in self.routes:
            if route.logical_object_ref == logical_object_ref:
                return route.table_ref
        return None


type LandingContractResolver = Callable[[str, str], LandingContractAuthority]


class AcquisitionLandingApplication:
    def __init__(
        self,
        *,
        contract_resolver: LandingContractResolver,
        routing: AcquisitionDestinationRouting,
        landing: AcquisitionLanding,
        artifact_store: AcquisitionSegmentArtifactStore,
        acknowledger: AcquisitionAcknowledger,
    ) -> None:
        self._contract_resolver = contract_resolver
        self._routing = routing
        self._landing = landing
        self._artifact_store = artifact_store
        self._acknowledger = acknowledger

    async def land(
        self,
        *,
        trigger_window: str,
        intent: AcquisitionIntent,
        preparation: AcquisitionPreparationResult,
    ) -> AcquisitionLandingResult:
        if not trigger_window:
            raise ValueError("trigger_window must be non-empty")
        authority = self._contract_resolver(intent.tenant_id, intent.contract_ref)
        self._require_routing_authority(intent, authority)
        coordinator = AcquisitionLandingCoordinator(
            artifact_store=self._artifact_store,
            landing=self._landing,
            target_resolver=self._target_resolver(intent, authority, trigger_window),
            acknowledger=self._acknowledger,
            consumer_ref=authority.acknowledgement_consumer_ref,
        )
        return await coordinator.land_and_acknowledge(intent=intent, preparation=preparation)

    def _require_routing_authority(
        self, intent: AcquisitionIntent, authority: LandingContractAuthority
    ) -> None:
        if (
            self._routing.tenant_id != intent.tenant_id
            or self._routing.contract_ref != intent.contract_ref
            or authority.tenant_id != intent.tenant_id
            or authority.contract_ref != intent.contract_ref
        ):
            raise AcquisitionContractError("destination_routing_authority_mismatch")
        if authority.contract_digest != intent.contract_digest:
            # The resolver answers with whatever is activated now. A batch prepared under an
            # earlier contract must not land stamped with a revision it was never validated
            # against: that revision reaches the generation key and the LAND receipt.
            raise AcquisitionContractError("contract_authority_drift")
        if any(self._routing.table_ref(object_ref) is None for object_ref in intent.object_refs):
            raise AcquisitionContractError("destination_routing_incomplete")

    def _target_resolver(
        self,
        intent: AcquisitionIntent,
        authority: LandingContractAuthority,
        trigger_window: str,
    ) -> Callable[[AcquisitionSegmentManifest], RawGenerationTarget]:
        def resolve(manifest: AcquisitionSegmentManifest) -> RawGenerationTarget:
            table_ref = self._routing.table_ref(manifest.logical_object_ref)
            if table_ref is None:
                raise AcquisitionContractError("destination_routing_incomplete")
            return RawGenerationTarget(
                tenant_id=intent.tenant_id,
                contract_ref=intent.contract_ref,
                contract_revision=authority.revision,
                trigger_window=trigger_window,
                destination_binding_ref=self._routing.destination_binding_ref,
                logical_object_ref=manifest.logical_object_ref,
                table_ref=table_ref,
                schema_digest=manifest.record_schema_digest,
            )

        return resolve


def landing_contract_resolver(repository: ActivatedContractReader) -> LandingContractResolver:
    """Read LAND's contract facts from contract-service's current activated record."""
    resolve_record = activated_contract_resolver(repository)

    def resolve(tenant_id: str, contract_ref: str) -> LandingContractAuthority:
        record = resolve_record(tenant_id, contract_ref)
        return LandingContractAuthority(
            tenant_id=record.tenant_id,
            contract_ref=record.contract_ref,
            revision=record.revision,
            contract_digest=record.contract_digest,
            acknowledgement_consumer_ref=record.contract.acknowledgement_consumer_ref,
        )

    return resolve


def compose_acquisition_landing(
    *,
    contract_resolver: LandingContractResolver,
    routing: AcquisitionDestinationRouting,
    landing: AcquisitionLanding,
    artifact_store: AcquisitionSegmentArtifactStore,
    acknowledger: AcquisitionAcknowledger,
) -> AcquisitionLandingApplication:
    return AcquisitionLandingApplication(
        contract_resolver=contract_resolver,
        routing=routing,
        landing=landing,
        artifact_store=artifact_store,
        acknowledger=acknowledger,
    )
