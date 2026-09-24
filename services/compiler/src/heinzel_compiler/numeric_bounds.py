from __future__ import annotations

from dataclasses import dataclass

INPUT_DECIMAL_PRECISION = 38
INPUT_DECIMAL_SCALE = 9
OUTPUT_DECIMAL_PRECISION = 57
OUTPUT_DECIMAL_SCALE = 9
MAX_PRODUCT_INPUT_ROWS = (2**63) - 1

_MAXIMUM_SCALED_INPUT = (10**INPUT_DECIMAL_PRECISION) - 1
_OUTPUT_SCALED_LIMIT = 10**OUTPUT_DECIMAL_PRECISION
_SIGNED_INT256_LIMIT = 2**255


@dataclass(frozen=True, slots=True)
class DecimalSumBoundProof:
    input_precision: int
    input_scale: int
    output_precision: int
    output_scale: int
    contributing_row_ceiling: int
    maximum_scaled_input: int
    maximum_scaled_sum: int


def prove_decimal_sum_bound(contributing_row_ceiling: int) -> DecimalSumBoundProof:
    if type(contributing_row_ceiling) is not int or not (
        0 <= contributing_row_ceiling <= MAX_PRODUCT_INPUT_ROWS
    ):
        raise ValueError("contributing row ceiling must fit the signed ledger range")
    maximum_scaled_sum = contributing_row_ceiling * _MAXIMUM_SCALED_INPUT
    if maximum_scaled_sum >= _OUTPUT_SCALED_LIMIT or maximum_scaled_sum >= _SIGNED_INT256_LIMIT:
        raise ValueError("contributing row ceiling cannot prove an exact widened decimal sum")
    return DecimalSumBoundProof(
        input_precision=INPUT_DECIMAL_PRECISION,
        input_scale=INPUT_DECIMAL_SCALE,
        output_precision=OUTPUT_DECIMAL_PRECISION,
        output_scale=OUTPUT_DECIMAL_SCALE,
        contributing_row_ceiling=contributing_row_ceiling,
        maximum_scaled_input=_MAXIMUM_SCALED_INPUT,
        maximum_scaled_sum=maximum_scaled_sum,
    )


__all__ = [
    "INPUT_DECIMAL_PRECISION",
    "INPUT_DECIMAL_SCALE",
    "MAX_PRODUCT_INPUT_ROWS",
    "OUTPUT_DECIMAL_PRECISION",
    "OUTPUT_DECIMAL_SCALE",
    "DecimalSumBoundProof",
    "prove_decimal_sum_bound",
]
