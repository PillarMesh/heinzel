from datetime import UTC, datetime
from decimal import Decimal

import pytest
from heinzel_contract_model import canonical_bytes, digest


def test_canonical_bytes_normalize_domain_scalars() -> None:
    value = {
        "z": Decimal("12.30"),
        "at": datetime(2026, 8, 13, 12, 34, 56, 123456, tzinfo=UTC),
        "a": "value",
    }

    assert canonical_bytes(value) == (
        b'{"a":"value","at":"2026-08-13T12:34:56.123456Z","z":"12.30"}'
    )
    assert digest(value) == "a711f696ef4903c8b2b69fd5530e402d2643ccc192f03a9e1428a4488c55fc2a"


def test_canonical_bytes_reject_float() -> None:
    with pytest.raises(TypeError, match="floating-point"):
        canonical_bytes({"amount": 12.3})


def test_canonical_bytes_reject_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_bytes({"at": datetime(2026, 8, 13, 12, 0)})
