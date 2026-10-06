"""What the demonstration's governed acquisition refuses, before it reaches a warehouse.

The acquisition itself needs a cluster and is covered by the live suite. These are its
preconditions: an acquisition that could not store its cursor, and a second acquisition over
a source this state directory has already acknowledged. Both are decided before anything
connects, so both are asked here rather than at the cost of a cluster.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from heinzel_console.demo.cursor_cipher import DemoCursorCipher
from heinzel_console.demo.generation import DemoAcquisition
from heinzel_console.demo.publication import DemoPublication, build_demo_publication
from heinzel_console.demo.stores import DemoStores
from heinzel_console.demo.warehouse import ProvisioningRefused
from heinzel_contract_model import digest
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


def test_an_acquisition_with_nowhere_to_encrypt_its_cursor_is_refused(tmp_path: Path) -> None:
    """No cipher means no state store, and a source cursor may not be kept unencrypted.

    `DemoStores` already declines to open the state repository without a cipher. What this
    asks is that the acquisition says so rather than failing later with an attribute it never
    had, or worse, acquiring into a store that is not there.
    """
    stores = DemoStores(tmp_path / "state")
    try:
        with pytest.raises(ProvisioningRefused, match="no cursor cipher"):
            DemoAcquisition(
                _UNREACHABLE_DSN,
                publication=_publication(stores),
                stores=stores,
                clock=lambda: _NOW,
            )
    finally:
        stores.close()


def test_a_second_acquisition_over_an_acknowledged_source_is_refused_with_what_to_do(
    stores: DemoStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acknowledging a landing moves the checkpoint, and no snapshot may be taken from there.

    The provider admits a snapshot only at checkpoint revision 0 and reports anything else as
    `permanent_configuration`, which names nothing an operator could act on. Reaching
    acquisition with a checkpoint already present means the record of what was landed was lost
    while the stores it points into were kept, so the two are to be discarded together -- and
    the refusal has to say that.

    The checkpoint is served from the real `SourceCheckpointState`, so this cannot pass on a
    shape the repository would never return.
    """
    publication = _publication(stores)
    contract = publication.contract
    acknowledged = SourceCheckpointState(
        tenant_id=contract.tenant_id,
        contract_digest=digest(contract),
        source_binding_ref="source-demo-orders",
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
        stores.acquisition_state,
        "load_checkpoint",
        lambda *_arguments: acknowledged,
    )

    with pytest.raises(ProvisioningRefused) as refusal:
        DemoAcquisition(
            _UNREACHABLE_DSN, publication=publication, stores=stores, clock=lambda: _NOW
        )

    assert "already acquired and acknowledged" in str(refusal.value)
    assert "docker compose down -v" in str(refusal.value)
