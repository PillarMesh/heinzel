from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pillarmesh_contract_model import ArtifactModel
from pillarmesh_iir.product_models import LiteralScalar, ScalarType
from pydantic import ValidationInfo, field_validator


class SqlParameter(ArtifactModel):
    name: str
    value_type: ScalarType
    value: LiteralScalar

    @field_validator("value", mode="before")
    @classmethod
    def restore_json_scalar_type(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "json" and isinstance(value, str):
            if info.data.get("value_type") == "decimal":
                try:
                    return Decimal(value)
                except InvalidOperation:
                    raise ValueError("decimal parameter is invalid") from None
            if info.data.get("value_type") == "timestamp":
                return datetime.fromisoformat(value)
        return value


class SqlEmission(ArtifactModel):
    engine: Literal["postgresql", "clickhouse"]
    statement: str
    parameters: tuple[SqlParameter, ...]
