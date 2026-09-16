from __future__ import annotations

import pytest
from pillarmesh_compiler.numeric_bounds import (
    MAX_PRODUCT_INPUT_ROWS,
    prove_decimal_sum_bound,
)


def test_decimal_sum_bound_proves_the_signed_ledger_ceiling() -> None:
    proof = prove_decimal_sum_bound(MAX_PRODUCT_INPUT_ROWS)

    assert proof.input_precision == 38
    assert proof.input_scale == 9
    assert proof.output_precision == 57
    assert proof.output_scale == 9
    assert proof.contributing_row_ceiling == (2**63) - 1
    assert proof.maximum_scaled_sum < 10**proof.output_precision
    assert proof.maximum_scaled_sum < 2**255


@pytest.mark.parametrize("contributing_row_ceiling", (0, 1, (2**63) - 1))
def test_decimal_sum_bound_accepts_every_ledger_boundary(
    contributing_row_ceiling: int,
) -> None:
    proof = prove_decimal_sum_bound(contributing_row_ceiling)

    assert proof.contributing_row_ceiling == contributing_row_ceiling


@pytest.mark.parametrize("contributing_row_ceiling", (-1, 2**63, True))
def test_decimal_sum_bound_rejects_values_outside_the_authority_range(
    contributing_row_ceiling: int,
) -> None:
    with pytest.raises(ValueError, match="contributing row ceiling"):
        prove_decimal_sum_bound(contributing_row_ceiling)
