from datetime import timedelta

import pytest
from heinzel_contract_model import (
    FIXED_PROJECTION,
    DestinationBinding,
    IntegrationContract,
    ProjectionField,
    SourceBinding,
)
from pydantic import ValidationError


def contract_data() -> dict[str, object]:
    return {
        "contract_id": "contract-001",
        "version": 1,
        "source": {
            "connection_handle": "pg-snapshot",
            "schema": "snapshot_source",
            "table": "orders",
            "primary_key": "order_id",
        },
        "destination": {
            "connection_handle": "snowflake-snapshot",
            "database": "HEINZEL_SNAPSHOT",
            "schema": "PUBLIC",
            "table": "ORDERS",
            "key": "order_id",
        },
        "projection": [field.model_dump() for field in FIXED_PROJECTION],
        "freshness_seconds": 300,
    }


def test_contract_accepts_only_fixed_snapshot_shape() -> None:
    contract = IntegrationContract.model_validate(contract_data())

    assert contract.materialization_mode == "snapshot"
    assert contract.commit_behavior == "idempotent_key_upsert"
    assert contract.deletion_behavior == "not_observed"
    assert contract.projection == FIXED_PROJECTION
    assert contract.freshness == timedelta(minutes=5)


def test_contract_rejects_additional_projection() -> None:
    data = contract_data()
    projection = list(data["projection"])  # type: ignore[arg-type]
    projection.append(ProjectionField(source="extra", destination="extra").model_dump())
    data["projection"] = projection

    with pytest.raises(ValidationError, match="fixed snapshot projection"):
        IntegrationContract.model_validate(data)


def test_contract_rejects_unknown_fields() -> None:
    data = contract_data()
    data["raw_password"] = "must-not-be-accepted"

    with pytest.raises(ValidationError, match="Extra inputs"):
        IntegrationContract.model_validate(data)


def test_bindings_reject_unchecked_identifiers() -> None:
    with pytest.raises(ValidationError, match="identifier"):
        SourceBinding(
            connection_handle="pg-snapshot",
            schema="public; drop schema public",
            table="orders",
            primary_key="order_id",
        )

    with pytest.raises(ValidationError, match="identifier"):
        DestinationBinding(
            connection_handle="sf-snapshot",
            database="HEINZEL_SNAPSHOT",
            schema="PUBLIC",
            table='ORDERS"; DROP TABLE ORDERS',
            key="order_id",
        )
