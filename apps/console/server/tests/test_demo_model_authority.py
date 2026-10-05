"""The signed model a restart must still be able to verify against.

`PostgreSQLAnswerGenerationAuthority` returns no output magnitude checks when it is given no
signed model, and answers anyway. So a demonstration that lost its model on restart would come
back enforcing nothing about its own output magnitudes, and look exactly the same doing it.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_console.demo.model_authority import (
    DemoSignedModelStore,
    SignedModelAuthorityError,
)
from heinzel_contract_model import digest
from heinzel_dbt_adapter import (
    CompiledDbtModel,
    DbtColumnTest,
    DbtDecimalMagnitudeCheck,
    SignedCompiledDbtModel,
    compiled_dbt_model_signing_bytes,
)


class _Identity(TypedDict):
    """The generation every case here stores and reads, spread into both calls.

    A `TypedDict` rather than a plain literal, whose values would be inferred as `object` and
    could be spread into any signature at all. The store's own parameters are what these are.
    """

    tenant_id: str
    product_id: str
    product_revision: int
    generation: int


_IDENTITY: _Identity = {
    "tenant_id": "tenant-demo",
    "product_id": "orders_daily",
    "product_revision": 1,
    "generation": 1,
}


def _signed(
    key: Ed25519PrivateKey, *, model_name: str = "orders_daily_g1"
) -> SignedCompiledDbtModel:
    model = CompiledDbtModel(
        model_name=model_name,
        contract_digest="a" * 64,
        provider="postgresql",
        input_generation_digests=("b" * 64,),
        target_schema="contract_demo",
        output_columns=("ordered_on", "total_order_value"),
        output_magnitude_checks=(DbtDecimalMagnitudeCheck(column_name="total_order_value"),),
        quality_tests=(DbtColumnTest(column_name="ordered_on", kind="not_null"),),
        compiled_sql="SELECT 1",
    )
    return SignedCompiledDbtModel(
        model=model,
        model_digest=digest(model),
        key_id="compiler-demo-1",
        signature=base64.b64encode(key.sign(compiled_dbt_model_signing_bytes(model))).decode(
            "ascii"
        ),
    )


@pytest.fixture(name="path")
def _path(tmp_path: Path) -> Iterator[str]:
    yield str(tmp_path / "signed-models.sqlite3")


def test_a_stored_model_is_readable_by_a_later_process(path: str) -> None:
    """The process that signs the model is not the one that answers over it."""
    key = Ed25519PrivateKey.generate()
    signed = _signed(key)
    writing = DemoSignedModelStore(path)
    try:
        writing.store(**_IDENTITY, signed_model=signed, public_key=key.public_key())
    finally:
        writing.close()

    reading = DemoSignedModelStore(path)
    try:
        authority = reading.read(**_IDENTITY)
    finally:
        reading.close()

    assert authority is not None
    assert authority.signed_model == signed
    # The key verifies the model it was stored beside, which is the whole point of keeping it.
    authority.public_key.verify(
        base64.b64decode(authority.signed_model.signature),
        compiled_dbt_model_signing_bytes(authority.signed_model.model),
    )


def test_a_generation_with_no_model_reads_as_none(path: str) -> None:
    store = DemoSignedModelStore(path)
    try:
        assert store.read(**_IDENTITY) is None
    finally:
        store.close()


def test_storing_the_same_model_again_is_harmless(path: str) -> None:
    """A restart that re-ran provisioning must not fail on its own earlier record."""
    key = Ed25519PrivateKey.generate()
    signed = _signed(key)
    store = DemoSignedModelStore(path)
    try:
        store.store(**_IDENTITY, signed_model=signed, public_key=key.public_key())
        store.store(**_IDENTITY, signed_model=signed, public_key=key.public_key())
        assert store.read(**_IDENTITY) is not None
    finally:
        store.close()


def test_a_second_model_for_one_generation_is_refused(path: str) -> None:
    """A generation's compiled model is part of what it is, so a different one is a conflict."""
    key = Ed25519PrivateKey.generate()
    store = DemoSignedModelStore(path)
    try:
        store.store(**_IDENTITY, signed_model=_signed(key), public_key=key.public_key())
        with pytest.raises(SignedModelAuthorityError, match="already recorded"):
            store.store(
                **_IDENTITY,
                signed_model=_signed(key, model_name="orders_daily_g1_other"),
                public_key=key.public_key(),
            )
    finally:
        store.close()


def test_a_second_key_for_one_generation_is_refused(path: str) -> None:
    """Accepting it would let a later key verify a model an earlier key signed."""
    key = Ed25519PrivateKey.generate()
    signed = _signed(key)
    store = DemoSignedModelStore(path)
    try:
        store.store(**_IDENTITY, signed_model=signed, public_key=key.public_key())
        with pytest.raises(SignedModelAuthorityError, match="already recorded"):
            store.store(
                **_IDENTITY,
                signed_model=signed,
                public_key=Ed25519PrivateKey.generate().public_key(),
            )
    finally:
        store.close()
