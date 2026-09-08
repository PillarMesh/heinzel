from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pillarmesh_connection_broker import SourceConnectionBinding, SourceConnectionBindingState
from pillarmesh_contract_model import digest
from pillarmesh_provider_sdk import (
    AcquisitionField,
    AcquisitionObjectSchema,
    ColumnObservation,
    ProviderObservation,
)
from pillarmesh_provider_sdk.acquisition_protocols import (
    AcquisitionObjectObservation,
    AcquisitionSourceObservation,
)
from pillarmesh_runtime import (
    AcquisitionContractError,
    AcquisitionDeclaredActivation,
    compose_activated_acquisition_contract,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
TENANT = "tenant-a"
BINDING_REF = "source-binding:orders"
CAPABILITY_DIGEST = "a" * 64
CONTRACT_DIGEST = "b" * 64


class _Lifecycle:
    """Mirrors the three fields contract-service's own lifecycle state asserts."""

    def __init__(self, *, lifecycle_state: str = "activated", tenant_id: str = TENANT) -> None:
        self.tenant_id = tenant_id
        self.contract_digest = CONTRACT_DIGEST
        self.lifecycle_state = lifecycle_state


def _binding(**changes: object) -> SourceConnectionBinding:
    values: dict[str, object] = {
        "binding_id": BINDING_REF,
        "tenant_id": TENANT,
        "provider_kind": "postgresql",
        "connection_handle": "connection-handle-a",
        "account_mode": "not_applicable",
        "lifecycle_state": SourceConnectionBindingState.READY,
        "approved_object_refs": ("orders",),
        "capability_profile_digest": CAPABILITY_DIGEST,
        "source_observation_ref": "source-observation:orders",
        "credential_revision": 2,
        "revision": 3,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return SourceConnectionBinding.model_validate(values)


def _observation(
    *,
    object_refs: tuple[str, ...] = ("orders",),
    capabilities: tuple[str, ...] | None = ("incremental", "snapshot"),
    tenant_id: str = TENANT,
    source_binding_ref: str = BINDING_REF,
) -> AcquisitionSourceObservation:
    return AcquisitionSourceObservation(
        tenant_id=tenant_id,
        source_binding_ref=source_binding_ref,
        provider_kind="postgresql",
        object_observations=tuple(
            AcquisitionObjectObservation(
                logical_object_ref=object_ref,
                provider_observation=ProviderObservation(
                    provider="postgresql",
                    connection_handle="connection-handle-a",
                    object_identity=digest({"object": object_ref}),
                    object_kind="base_table",
                    schema_digest=digest({"schema": object_ref}),
                    columns=(ColumnObservation(name="id", type_name="BIGINT", nullable=False),),
                    key_name="id",
                    key_type="BIGINT",
                    key_nullable=False,
                    key_constraint="primary_key",
                    stable_key_order=True,
                    read_only=True,
                    capabilities=capabilities,
                    observed_at=NOW,
                    snapshot_semantics="snapshot",
                    commit_ledger_object_kind=None,
                    commit_ledger_columns=None,
                    commit_ledger_key_name=None,
                    commit_ledger_key_constraint=None,
                    evidence_safe=True,
                ),
            )
            for object_ref in object_refs
        ),
    )


def _schema(logical_object_ref: str = "orders") -> AcquisitionObjectSchema:
    fields = (AcquisitionField(name="id", value_type="integer", nullable=False),)
    return AcquisitionObjectSchema(
        logical_object_ref=logical_object_ref,
        schema_digest=digest(fields),
        fields=fields,
        record_key_fields=("id",),
        source_updated_at_field=None,
    )


def _declared(**changes: object) -> AcquisitionDeclaredActivation:
    values: dict[str, object] = {
        "contract_ref": "contract:orders:v1",
        "acknowledgement_consumer_ref": "strict-consumer",
        "object_schemas": (_schema(),),
        "record_ceiling": 100,
        "encoded_byte_ceiling": 1_000_000,
    }
    values.update(changes)
    return AcquisitionDeclaredActivation.model_validate(values)


def _compose(**changes: object) -> object:
    values: dict[str, object] = {
        "lifecycle": _Lifecycle(),
        "binding": _binding(),
        "observation": _observation(),
        "declared": _declared(),
    }
    values.update(changes)
    return compose_activated_acquisition_contract(**values)  # type: ignore[arg-type]


def test_a_contract_is_composed_from_what_each_owning_service_asserts() -> None:
    """Ten of the fifteen fields come from a service; none of them is invented here."""
    contract = _compose()

    assert contract.tenant_id == TENANT
    assert contract.contract_digest == CONTRACT_DIGEST
    assert contract.lifecycle_state == "activated"
    assert contract.source_binding_ref == BINDING_REF
    assert contract.source_binding_revision == 3
    assert contract.credential_revision == 2
    assert contract.capability_profile_digest == CAPABILITY_DIGEST
    assert contract.source_observation_ref == "source-observation:orders"
    assert contract.source_observation_digest == digest(_observation())


def test_the_admitted_modes_are_what_every_observed_object_supports() -> None:
    """A mode one object cannot serve is not a mode the contract may admit.

    The provider reports capabilities per object, so the contract's admitted set is
    their intersection, in the canonical order the model requires.
    """
    contract = _compose(
        observation=_observation(object_refs=("orders",), capabilities=("snapshot", "incremental"))
    )

    assert contract.acquisition_modes == ("incremental", "snapshot")


def test_an_object_that_cannot_serve_a_mode_removes_it_from_the_contract() -> None:
    observation = AcquisitionSourceObservation(
        tenant_id=TENANT,
        source_binding_ref=BINDING_REF,
        provider_kind="postgresql",
        object_observations=(
            _observation(
                object_refs=("orders",), capabilities=("snapshot", "incremental")
            ).object_observations[0],
            _observation(object_refs=("payments",), capabilities=("snapshot",)).object_observations[
                0
            ],
        ),
    )

    contract = _compose(
        binding=_binding(approved_object_refs=("orders", "payments")),
        observation=observation,
        declared=_declared(object_schemas=(_schema("orders"), _schema("payments"))),
    )

    assert contract.acquisition_modes == ("snapshot",)


def test_a_retired_contract_composes_as_inactive() -> None:
    """The runtime's vocabulary is activated/inactive; contract-service says retired."""
    contract = _compose(lifecycle=_Lifecycle(lifecycle_state="retired"))

    assert contract.lifecycle_state == "inactive"


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"lifecycle": _Lifecycle(tenant_id="tenant-somebody-else")}, "tenant"),
        ({"observation": _observation(tenant_id="tenant-somebody-else")}, "tenant"),
        ({"observation": _observation(source_binding_ref="source-binding:other")}, "binding"),
        (
            {
                "binding": _binding(
                    lifecycle_state=SourceConnectionBindingState.DRAFT,
                    capability_profile_digest=None,
                    source_observation_ref=None,
                )
            },
            "not_ready",
        ),
        ({"observation": _observation(capabilities=None)}, "modes"),
        ({"observation": _observation(capabilities=("teleport",))}, "modes"),
    ),
)
def test_sources_that_disagree_refuse_to_compose(changes: dict[str, object], reason: str) -> None:
    """Composition asserts nothing it cannot source, so a disagreement is refused.

    Every one of these would otherwise produce a contract that reads as authority
    while resting on values no service agreed on.
    """
    with pytest.raises(AcquisitionContractError, match=reason):
        _compose(**changes)


def test_a_declared_object_the_binding_never_approved_is_refused() -> None:
    """Approval lives on the binding; a declaration cannot widen it."""
    with pytest.raises(AcquisitionContractError, match="approved"):
        _compose(
            observation=_observation(object_refs=("payments",)),
            declared=_declared(object_schemas=(_schema("payments"),)),
        )


def test_declared_schemas_must_describe_exactly_the_observed_objects() -> None:
    """A schema for an object nobody observed describes nothing the source has."""
    with pytest.raises(AcquisitionContractError, match="object"):
        _compose(declared=_declared(object_schemas=(_schema("orders"), _schema("payments"))))


def test_the_declared_half_is_named_as_declared_rather_than_asserted() -> None:
    """Five fields have no owning publisher, and the type says so.

    `object_schemas` is the substantive one: `open_session` takes it as an input and
    no provider returns it, so nothing in the estate can assert what a source's
    schema is. Composing it silently would invent the authority this layer exists to
    avoid inventing.
    """
    declared = _declared()

    assert set(type(declared).model_fields) == {
        "contract_ref",
        "acknowledgement_consumer_ref",
        "object_schemas",
        "record_ceiling",
        "encoded_byte_ceiling",
    }


def test_an_unmodelled_capability_is_refused_even_beside_a_recognised_one() -> None:
    """Filtering the unknown one out would drop a disagreement instead of reporting it.

    A provider naming a mode this runtime does not model has said something the
    composition cannot honour. Keeping the modes it does recognise would compose a
    contract that silently admits less than the source offered, and record no reason
    why -- the same flattening that turning a transient failure into a denial does.
    """
    with pytest.raises(AcquisitionContractError, match="not_recognised"):
        _compose(observation=_observation(capabilities=("snapshot", "teleport")))
