from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, get_args

from pillarmesh_connection_broker import SourceConnectionBinding, SourceConnectionBindingState
from pillarmesh_contract_model import ArtifactModel, ArtifactReference, digest
from pillarmesh_provider_sdk import AcquisitionObjectSchema
from pillarmesh_provider_sdk.acquisition_models import AcquisitionMode
from pillarmesh_provider_sdk.acquisition_protocols import AcquisitionSourceObservation
from pydantic import Field

from .acquisition import ActivatedAcquisitionContract
from .acquisition_errors import AcquisitionContractError

_ADMITTED_MODES: frozenset[AcquisitionMode] = frozenset(get_args(AcquisitionMode.__value__))


class ContractActivationLifecycle(Protocol):
    """What contract-service asserts about an acquisition contract's activation.

    Declared structurally so the runtime does not depend on contract-service to
    compose a contract; `AcquisitionContractLifecycleState` satisfies it as it
    stands.
    """

    @property
    def tenant_id(self) -> str: ...

    @property
    def contract_digest(self) -> str: ...

    @property
    def lifecycle_state(self) -> str: ...


class AcquisitionDeclaredActivation(ArtifactModel):
    """Explicit non-source inputs for the legacy contract composition helper.

    Contract service now persists the resulting v2 artifact and is its authority.
    This helper still requires every field that cannot be derived from connection-
    broker and provider observations instead of inventing values.

    `object_schemas` is the substantive one. `AcquisitionSession.open` takes schemas
    as an input and no provider returns them, and the observation publishes only
    `ColumnObservation` -- a name, a raw source type and nullability -- which cannot
    reconstruct the closed `AcquisitionValueType` vocabulary an
    `AcquisitionObjectSchema` field requires. The ceilings are policy, and the
    references are identity the activating authority chooses.
    """

    contract_ref: str = Field(min_length=1)
    process_package_ref: ArtifactReference
    product_intent_ref: ArtifactReference
    destination_product_ref: str = Field(min_length=1)
    acknowledgement_consumer_ref: str = Field(min_length=1)
    object_schemas: tuple[AcquisitionObjectSchema, ...] = Field(min_length=1)
    record_ceiling: int = Field(gt=0)
    encoded_byte_ceiling: int = Field(gt=0)
    activated_by: str = Field(min_length=1)
    activated_at: datetime


def compose_activated_acquisition_contract(
    *,
    lifecycle: ContractActivationLifecycle,
    binding: SourceConnectionBinding,
    observation: AcquisitionSourceObservation,
    declared: AcquisitionDeclaredActivation,
) -> ActivatedAcquisitionContract:
    """Assemble a v2 candidate for contract service to validate and persist.

    Runtime execution reads the persisted contract-service record. This helper is
    retained for existing activation callers and requires its inputs to agree
    before contract service accepts the candidate.
    """
    if lifecycle.tenant_id != binding.tenant_id or observation.tenant_id != binding.tenant_id:
        raise AcquisitionContractError("composition_tenant_mismatch")
    if observation.source_binding_ref != binding.binding_id:
        raise AcquisitionContractError("composition_binding_mismatch")
    # A binding carries validated capability authority only while it is ready: the
    # broker's own model refuses to hold that authority in any other state, and
    # refuses to hold half of it. Requiring ready is therefore the whole check, and
    # the two narrowing guards below are unreachable through that model rather than
    # redundant -- they exist because this composition should not depend on a remote
    # validator for its own type safety.
    if binding.lifecycle_state is not SourceConnectionBindingState.READY:
        raise AcquisitionContractError("composition_binding_not_ready")
    capability_profile_digest = binding.capability_profile_digest
    source_observation_ref = binding.source_observation_ref
    if capability_profile_digest is None or source_observation_ref is None:
        raise AcquisitionContractError("composition_binding_authority_absent")

    observed_refs = tuple(item.logical_object_ref for item in observation.object_observations)
    if not set(observed_refs).issubset(binding.approved_object_refs):
        raise AcquisitionContractError("composition_object_not_approved")
    declared_refs = tuple(schema.logical_object_ref for schema in declared.object_schemas)
    if sorted(declared_refs) != sorted(observed_refs):
        raise AcquisitionContractError("composition_object_schema_mismatch")

    return ActivatedAcquisitionContract(
        tenant_id=binding.tenant_id,
        contract_ref=declared.contract_ref,
        contract_digest=lifecycle.contract_digest,
        process_package_ref=declared.process_package_ref,
        product_intent_ref=declared.product_intent_ref,
        destination_product_ref=declared.destination_product_ref,
        source_binding_ref=binding.binding_id,
        source_binding_revision=binding.revision,
        credential_revision=binding.credential_revision,
        acknowledgement_consumer_ref=declared.acknowledgement_consumer_ref,
        capability_profile_digest=capability_profile_digest,
        source_observation_ref=source_observation_ref,
        source_observation_digest=digest(observation),
        lifecycle_state=_lifecycle_state(lifecycle.lifecycle_state),
        acquisition_modes=_admitted_modes(observation),
        object_schemas=tuple(
            sorted(declared.object_schemas, key=lambda schema: schema.logical_object_ref)
        ),
        record_ceiling=declared.record_ceiling,
        encoded_byte_ceiling=declared.encoded_byte_ceiling,
        activated_by=declared.activated_by,
        activated_at=declared.activated_at,
    )


def _lifecycle_state(value: str) -> Literal["activated", "inactive"]:
    # contract-service says activated or retired; the runtime admits or does not.
    # Anything else is a vocabulary this composition has no mapping for.
    if value == "activated":
        return "activated"
    if value == "retired":
        return "inactive"
    raise AcquisitionContractError("composition_lifecycle_state_not_admitted")


def _admitted_modes(observation: AcquisitionSourceObservation) -> tuple[AcquisitionMode, ...]:
    """The modes every observed object can serve, in canonical order.

    Capabilities are reported per object and typed only as `tuple[str, ...] | None`,
    so a mode one object cannot serve is not one the contract may admit, and a
    capability outside the closed vocabulary is refused rather than dropped -- a
    provider naming something this runtime does not model is a disagreement, not a
    silent no-op.
    """
    admitted: set[AcquisitionMode] | None = None
    for item in observation.object_observations:
        capabilities = item.provider_observation.capabilities
        if not capabilities:
            raise AcquisitionContractError("composition_admitted_modes_absent")
        reported = set(capabilities)
        if reported - set(_ADMITTED_MODES):
            raise AcquisitionContractError("composition_admitted_modes_not_recognised")
        # Narrowed through the closed vocabulary rather than cast: every capability
        # kept here was matched against a declared mode on the line above.
        recognised = {mode for mode in _ADMITTED_MODES if mode in reported}
        admitted = recognised if admitted is None else admitted & recognised
    if not admitted:
        raise AcquisitionContractError("composition_admitted_modes_absent")
    return tuple(sorted(admitted))
