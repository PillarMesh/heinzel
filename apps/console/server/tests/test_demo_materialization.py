"""The demonstration's product is one the compiler can express.

These need no warehouse: they ask the compiler whether the demonstration's intent is
admissible under its restricted shape, and what statement it emits. That is the question
most likely to be broken by an edit to the metric or the source, and the cheapest to ask.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_console.demo.materialization import (
    DEMO_GROUP_COLUMN,
    DEMO_MEASURE_COLUMN,
    compose_demo_physical_plan,
    demo_generation_source,
    demo_product_intent,
    sign_demo_model,
)
from heinzel_console.demo.publication import build_demo_publication
from heinzel_console.demo.stores import DemoStores
from heinzel_contract_model import ManagedIntegrationContract, digest
from heinzel_dbt_adapter import compiled_dbt_model_signing_bytes
from heinzel_execution_graph import ProductPhysicalPlan
from heinzel_iir import AggregateOperation

_GENERATION_ID = "d" * 64
_RECEIPT_DIGEST = "a" * 64


@pytest.fixture(name="contract")
def _contract(tmp_path: Path) -> Iterator[ManagedIntegrationContract]:
    stores = DemoStores(tmp_path / "state")
    try:
        yield build_demo_publication(
            stores, clock=lambda: datetime(2026, 9, 12, tzinfo=UTC)
        ).contract
    finally:
        stores.close()


def _plan(contract: ManagedIntegrationContract) -> ProductPhysicalPlan:
    return compose_demo_physical_plan(
        contract=contract,
        source=demo_generation_source(
            generation_id=_GENERATION_ID,
            landing_receipt_digest=_RECEIPT_DIGEST,
            observed_schema_digest="3" * 64,
        ),
        target_schema="demo_target",
        model_name="orders_daily_g1",
        expected_output_schema_digest="c" * 64,
        provider_observation_digest="9" * 64,
    )


def test_the_demonstration_product_is_admissible_under_the_restricted_shape(
    contract: ManagedIntegrationContract,
) -> None:
    """A metric the compiler cannot express would leave the seeded question unanswerable."""
    plan = _plan(contract)

    assert plan.output_columns == (DEMO_GROUP_COLUMN, DEMO_MEASURE_COLUMN)
    assert plan.legality_rule_id == "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"


def test_the_emitted_statement_is_scoped_to_one_generation(
    contract: ManagedIntegrationContract,
) -> None:
    """The generation is in the statement, so another generation is another statement."""
    plan = _plan(contract)

    assert _GENERATION_ID in plan.emitted_statement
    assert "raw_customer_orders" in plan.emitted_statement
    assert "sum" in plan.emitted_statement.lower()


def test_the_product_sums_rather_than_counts(contract: ManagedIntegrationContract) -> None:
    """`AggregateMeasure.function` admits `sum` alone; the metric follows that, not taste."""
    intent = demo_product_intent()
    aggregate = intent.operations[1]

    # Narrowed rather than asserted on directly: the operation union has four other members,
    # and a product whose second step stopped being the aggregate should fail here loudly.
    assert isinstance(aggregate, AggregateOperation)
    assert [measure.function for measure in aggregate.measures] == ["sum"]
    assert aggregate.measures[0].output_name == DEMO_MEASURE_COLUMN


def test_the_signed_model_carries_the_compilers_own_statement(
    contract: ManagedIntegrationContract,
) -> None:
    """A model signed over anything else would materialize a product the plan does not describe."""
    plan = _plan(contract)
    key = Ed25519PrivateKey.generate()

    signed = sign_demo_model(
        key,
        plan=plan,
        contract=contract,
        input_generation_digest=_RECEIPT_DIGEST,
        target_schema="demo_target",
        model_name="orders_daily_g1",
    )

    assert signed.model.compiled_sql == plan.emitted_statement
    assert signed.model.output_columns == plan.output_columns
    assert signed.model_digest == digest(signed.model)

    # The signature verifies over the model's own signing bytes.
    key.public_key().verify(
        base64.b64decode(signed.signature),
        compiled_dbt_model_signing_bytes(signed.model),
    )
