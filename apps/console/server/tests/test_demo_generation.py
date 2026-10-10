"""What the demonstration's governed acquisition refuses, before it reaches a warehouse.

The acquisition itself needs a cluster and is covered by the live suite. These are its
preconditions, and they divide the way the code does: what activating a contract refuses, and
what composing an acquisition under an activated one refuses. Every one of them is decided
before anything connects, so all of them are asked here rather than at the cost of a cluster.
The DSN names a port nothing serves, which is what makes that claim checkable.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from heinzel_connection_broker import SourceConnectionBinding, SourceConnectionBindingState
from heinzel_console.demo.cursor_cipher import DemoCursorCipher
from heinzel_console.demo.generation import (
    DEMO_LOGICAL_OBJECT,
    DEMO_SOURCE_CONNECTION_HANDLE,
    DemoAcquisition,
    DemoSourceBindingReader,
    ensure_activated_demo_contract,
)
from heinzel_console.demo.publication import DemoPublication, build_demo_publication
from heinzel_console.demo.stores import DemoStores
from heinzel_console.demo.warehouse import ProvisioningRefused
from heinzel_contract_model import digest
from heinzel_provider_sdk import (
    AcquisitionObjectObservation,
    AcquisitionSourceObservation,
    ColumnObservation,
    ProviderObservation,
)
from heinzel_state import SourceCheckpointState

_NOW = datetime(2026, 9, 13, tzinfo=UTC)
# A DSN naming a port nothing serves, so any step that connected before refusing would raise
# `psycopg.OperationalError` here instead of the refusal these tests are about.
_UNREACHABLE_DSN = "postgresql://acquisition_runtime@127.0.0.1:1/heinzel"


@pytest.fixture(name="stores")
def _stores(tmp_path: Path) -> Iterator[DemoStores]:
    stores = DemoStores(tmp_path / "state", cursor_cipher_factory=DemoCursorCipher)
    try:
        yield stores
    finally:
        stores.close()


def _publication(stores: DemoStores) -> DemoPublication:
    return build_demo_publication(stores, clock=lambda: _NOW)


_BINDING_REF = "src-0123456789abcdef01234567"


def _binding(tenant_id: str) -> SourceConnectionBinding:
    """A ready binding shaped as the broker commits one, with probed authority on it.

    Built here rather than driven through `SourceBindingService`, because what these two tests
    are about happens before anything the broker owns: they need a binding the acquisition
    accepts so that the refusal under test is the one that fires.
    """
    return SourceConnectionBinding(
        binding_id=_BINDING_REF,
        tenant_id=tenant_id,
        provider_kind="postgresql",
        connection_handle=DEMO_SOURCE_CONNECTION_HANDLE,
        account_mode="not_applicable",
        lifecycle_state=SourceConnectionBindingState.READY,
        approved_object_refs=(DEMO_LOGICAL_OBJECT,),
        capability_profile_digest="a" * 64,
        source_observation_ref="source-observation:postgresql:" + "b" * 64,
        credential_revision=1,
        revision=3,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _Bindings:
    """The broker's register, as the acquisition runtime reads it."""

    def __init__(self, binding: SourceConnectionBinding) -> None:
        self._binding = binding

    def load(self, tenant_id: str, binding_id: str) -> SourceConnectionBinding:
        if (tenant_id, binding_id) != (self._binding.tenant_id, self._binding.binding_id):
            raise KeyError("no such source binding")
        return self._binding


def _bindings(binding: SourceConnectionBinding) -> DemoSourceBindingReader:
    """The double, seen as `DemoAcquisition` takes it, so the type checker answers for it."""
    return _Bindings(binding)


def test_an_acquisition_with_nowhere_to_encrypt_its_cursor_is_refused(tmp_path: Path) -> None:
    """No cipher means no state store, and a source cursor may not be kept unencrypted.

    `DemoStores` already declines to open the state repository without a cipher. What this
    asks is that the acquisition says so rather than failing later with an attribute it never
    had, or worse, acquiring into a store that is not there.
    """
    stores = DemoStores(tmp_path / "state")
    try:
        with pytest.raises(ProvisioningRefused, match="no cursor cipher"):
            publication = _publication(stores)
            binding = _binding(publication.contract.tenant_id)
            DemoAcquisition(
                _UNREACHABLE_DSN,
                binding=binding,
                bindings=_bindings(binding),
                publication=publication,
                stores=stores,
                clock=lambda: _NOW,
            )
    finally:
        stores.close()


def _acknowledged_checkpoint(
    stores: DemoStores, monkeypatch: pytest.MonkeyPatch, *, publication: DemoPublication
) -> None:
    """Report this source's checkpoint as advanced, as a landed and acknowledged batch leaves it.

    Served from the real `SourceCheckpointState`, so this cannot pass on a shape the
    repository would never return.
    """
    acknowledged = SourceCheckpointState(
        tenant_id=publication.contract.tenant_id,
        contract_digest=digest(publication.contract),
        source_binding_ref=_BINDING_REF,
        provider_kind="postgresql",
        cursor_version="1",
        revision=1,
        encrypted_cursor_payload=b"encrypted-cursor",
        cursor_digest="c" * 64,
        last_batch_id="b" * 64,
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert stores.acquisition_state is not None
    monkeypatch.setattr(
        stores.acquisition_state, "load_checkpoint", lambda *_arguments: acknowledged
    )


def test_activating_a_contract_whose_source_was_already_acquired_is_refused_with_what_to_do(
    stores: DemoStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acknowledging a landing moves the checkpoint, and no snapshot may be taken from there.

    So reaching activation with a checkpoint already present means the record of what the
    first acquisition landed was lost while the stores it points into were kept, and the two
    are to be discarded together. The provider would report `permanent_configuration`, which
    names nothing an operator could act on, so this refuses first and says what happened.

    This is about activating, not about acquiring: a run commanded after the first one
    acquires under the activation that is already there and never comes through here.
    """
    publication = _publication(stores)
    _acknowledged_checkpoint(stores, monkeypatch, publication=publication)

    with pytest.raises(ProvisioningRefused) as refusal:
        ensure_activated_demo_contract(
            _UNREACHABLE_DSN,
            binding=_binding(publication.contract.tenant_id),
            publication=publication,
            stores=stores,
            clock=lambda: _NOW,
        )

    assert "already acquired and acknowledged" in str(refusal.value)
    assert "docker compose down -v" in str(refusal.value)


def test_an_acquisition_composes_over_a_source_whose_checkpoint_has_already_advanced(
    stores: DemoStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second acquisition is what an advanced checkpoint is for, not what it forbids.

    This used to refuse: the whole object activated as it constructed, so composing one over
    an acknowledged source meant a snapshot nobody could take. Activating is separate now, so
    an advanced checkpoint is just a source that has been read once -- which is the state
    every commanded run starts from.

    The DSN names a port nothing serves, so composing successfully is the assertion: it
    reached no source and wrote no activation.
    """
    publication = _publication(stores)
    contract = publication.contract
    binding = _binding(contract.tenant_id)
    assert binding.source_observation_ref is not None
    stores.source_observations.store(
        observation_ref=binding.source_observation_ref,
        observation=_observation(contract.tenant_id, binding.binding_id),
    )
    _already_activated(stores, monkeypatch, contract_ref=contract.contract_id)
    _acknowledged_checkpoint(stores, monkeypatch, publication=publication)

    acquisition = DemoAcquisition(
        _UNREACHABLE_DSN,
        binding=binding,
        bindings=_bindings(binding),
        publication=publication,
        stores=stores,
        clock=lambda: _NOW,
    )

    assert acquisition is not None


def test_an_acquisition_over_a_contract_nothing_activated_is_refused_by_name(
    stores: DemoStores,
) -> None:
    """Composing one is not activating one, and the refusal says where activating happens."""
    publication = _publication(stores)
    binding = _binding(publication.contract.tenant_id)

    with pytest.raises(ProvisioningRefused, match="not activated"):
        DemoAcquisition(
            _UNREACHABLE_DSN,
            binding=binding,
            bindings=_bindings(binding),
            publication=publication,
            stores=stores,
            clock=lambda: _NOW,
        )


def test_an_acquisition_under_an_unvalidated_binding_is_refused_by_name(
    stores: DemoStores,
) -> None:
    """A binding that is not ready carries no capability authority, so there is nothing to run
    under.

    The composition further in refuses it too, as `composition_binding_not_ready` -- a reason
    code about an assembly, which says nothing about what an operator should do. This refusal
    is the one worth reading, and it comes before anything connects.
    """
    publication = _publication(stores)
    draft = _binding(publication.contract.tenant_id).model_copy(
        update={
            "lifecycle_state": SourceConnectionBindingState.DRAFT,
            "capability_profile_digest": None,
            "source_observation_ref": None,
            "revision": 1,
        }
    )

    with pytest.raises(ProvisioningRefused, match="not ready"):
        DemoAcquisition(
            _UNREACHABLE_DSN,
            binding=draft,
            bindings=_bindings(draft),
            publication=publication,
            stores=stores,
            clock=lambda: _NOW,
        )


def test_an_acquisition_under_another_tenants_binding_is_refused(stores: DemoStores) -> None:
    """One tenant's contract may not be acquired under another tenant's source.

    Checked here rather than left to the composition for the same reason as above, and because
    this one is a boundary: the tenant on the binding is the tenant whose source is read.
    """
    publication = _publication(stores)
    foreign = _binding("tenant-somebody-else")

    with pytest.raises(ProvisioningRefused, match="another tenant"):
        DemoAcquisition(
            _UNREACHABLE_DSN,
            binding=foreign,
            bindings=_bindings(foreign),
            publication=publication,
            stores=stores,
            clock=lambda: _NOW,
        )


def _observation(tenant_id: str, binding_id: str) -> AcquisitionSourceObservation:
    """A provider reading of the demonstration's one source object.

    Shaped as `observe_source` returns one, because the resumption under test hands exactly
    this back to the runner and the activated contract pins its digest.
    """
    return AcquisitionSourceObservation(
        tenant_id=tenant_id,
        source_binding_ref=binding_id,
        provider_kind="postgresql",
        object_observations=(
            AcquisitionObjectObservation(
                logical_object_ref=DEMO_LOGICAL_OBJECT,
                provider_observation=ProviderObservation(
                    provider="postgresql",
                    connection_handle=DEMO_SOURCE_CONNECTION_HANDLE,
                    object_identity="source_data.customer_orders",
                    object_kind="base_table",
                    schema_digest="d" * 64,
                    columns=(
                        ColumnObservation(name="order_id", type_name="BIGINT", nullable=False),
                    ),
                    key_name="order_id",
                    key_type="BIGINT",
                    key_nullable=False,
                    key_constraint="primary_key",
                    stable_key_order=True,
                    read_only=True,
                    capabilities=("snapshot",),
                    observed_at=_NOW,
                    snapshot_semantics="snapshot",
                    commit_ledger_object_kind=None,
                    commit_ledger_columns=None,
                    commit_ledger_key_name=None,
                    commit_ledger_key_constraint=None,
                    evidence_safe=True,
                ),
            ),
        ),
    )


def _already_activated(
    stores: DemoStores, monkeypatch: pytest.MonkeyPatch, *, contract_ref: str
) -> None:
    """Report this contract as activated, as a start that died before landing would leave it.

    The lifecycle read is substituted rather than a real activation driven, because reaching a
    real one takes a source to observe: what is under test is the branch taken on the way in,
    before anything connects.
    """
    record = SimpleNamespace(contract_ref=contract_ref)
    monkeypatch.setattr(stores.acquisition_lifecycle, "list_contracts", lambda _tenant: (record,))


def test_a_start_resumes_the_contract_an_earlier_one_activated(
    stores: DemoStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window between activating and landing is one a start can come back into.

    It could not be before: the activation pins `digest(observation)`, the provider stamps a
    wall clock into every reading, and nothing held the reading -- so the contract admitted
    only an observation no later process could reproduce. The window is not rare, because a
    dbt run sits in it.

    The DSN names a port nothing serves, so observing the source again would raise
    `psycopg.OperationalError` here. Constructing successfully is therefore the assertion: it
    resumed rather than observed.
    """
    publication = _publication(stores)
    contract = publication.contract
    binding = _binding(contract.tenant_id)
    assert binding.source_observation_ref is not None
    stores.source_observations.store(
        observation_ref=binding.source_observation_ref,
        observation=_observation(contract.tenant_id, binding.binding_id),
    )
    _already_activated(stores, monkeypatch, contract_ref=contract.contract_id)

    acquisition = DemoAcquisition(
        _UNREACHABLE_DSN,
        binding=binding,
        bindings=_bindings(binding),
        publication=publication,
        stores=stores,
        clock=lambda: _NOW,
    )

    assert acquisition is not None
    # And it activated nothing further: the record it found is the only one there is.
    assert [
        record.contract_ref
        for record in stores.acquisition_lifecycle.list_contracts(contract.tenant_id)
    ] == [contract.contract_id]


def test_an_activated_contract_whose_observation_is_lost_is_refused_with_what_to_do(
    stores: DemoStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state directory written before the observation was kept, or deleted from.

    Observing again would produce a reading the activated contract does not admit, so this
    refuses rather than trying -- and says which two things to discard, because the warehouse
    holds what the contract was activated against.
    """
    publication = _publication(stores)
    contract = publication.contract
    _already_activated(stores, monkeypatch, contract_ref=contract.contract_id)

    with pytest.raises(ProvisioningRefused) as refusal:
        DemoAcquisition(
            _UNREACHABLE_DSN,
            binding=_binding(contract.tenant_id),
            bindings=_bindings(_binding(contract.tenant_id)),
            publication=publication,
            stores=stores,
            clock=lambda: _NOW,
        )

    assert "is not held" in str(refusal.value)
    assert "docker compose down -v" in str(refusal.value)
