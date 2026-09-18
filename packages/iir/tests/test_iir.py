from copy import deepcopy

from heinzel_contract_model import FIXED_PROJECTION, IntegrationContract
from heinzel_iir import lower_contract


def contract_data() -> dict[str, object]:
    return {
        "contract_id": "contract-001",
        "version": 1,
        "source": {
            "connection_handle": "pg-one",
            "schema": "m0_source",
            "table": "orders",
            "primary_key": "order_id",
        },
        "destination": {
            "connection_handle": "sf-one",
            "database": "HEINZEL_M0",
            "schema": "PUBLIC",
            "table": "ORDERS",
            "key": "order_id",
        },
        "projection": [item.model_dump() for item in FIXED_PROJECTION],
        "freshness_seconds": 300,
    }


def test_iir_semantic_identity_excludes_connection_handles() -> None:
    left = contract_data()
    right = deepcopy(left)
    right["source"]["connection_handle"] = "pg-two"  # type: ignore[index]
    right["destination"]["connection_handle"] = "sf-two"  # type: ignore[index]

    left_iir = lower_contract(IntegrationContract.model_validate(left))
    right_iir = lower_contract(IntegrationContract.model_validate(right))

    assert left_iir.semantic_digest == right_iir.semantic_digest
    assert "connection_handle" not in left_iir.model_dump_json()
