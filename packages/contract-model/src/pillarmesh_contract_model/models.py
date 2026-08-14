from __future__ import annotations

import re
from datetime import timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_HANDLE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("identifier contains unsupported characters")
    return value


def _handle(value: str) -> str:
    if not _HANDLE.fullmatch(value):
        raise ValueError("connection handle contains unsupported characters")
    return value


class ProjectionField(ArtifactModel):
    source: str
    destination: str

    @model_validator(mode="after")
    def checked_identifiers(self) -> Self:
        _identifier(self.source)
        _identifier(self.destination)
        return self


FIXED_PROJECTION: tuple[ProjectionField, ...] = (
    ProjectionField(source="order_id", destination="order_id"),
    ProjectionField(source="customer_ref", destination="customer_ref"),
    ProjectionField(source="amount", destination="amount"),
    ProjectionField(source="currency", destination="currency"),
    ProjectionField(source="status", destination="order_status"),
    ProjectionField(source="updated_at", destination="updated_at"),
)


class SourceBinding(ArtifactModel):
    connection_handle: str
    schema_name: str = Field(alias="schema")
    table: str
    primary_key: Literal["order_id"]

    @model_validator(mode="after")
    def checked_values(self) -> Self:
        _handle(self.connection_handle)
        _identifier(self.schema_name)
        _identifier(self.table)
        return self


class DestinationBinding(ArtifactModel):
    connection_handle: str
    database: str
    schema_name: str = Field(alias="schema")
    table: str
    key: Literal["order_id"]

    @model_validator(mode="after")
    def checked_values(self) -> Self:
        _handle(self.connection_handle)
        _identifier(self.database)
        _identifier(self.schema_name)
        _identifier(self.table)
        return self


class IntegrationContract(ArtifactModel):
    schema_version: Literal["1"] = "1"
    contract_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    source: SourceBinding
    destination: DestinationBinding
    projection: tuple[ProjectionField, ...]
    materialization_mode: Literal["snapshot"] = "snapshot"
    commit_behavior: Literal["idempotent_key_upsert"] = "idempotent_key_upsert"
    deletion_behavior: Literal["not_observed"] = "not_observed"
    freshness_seconds: int = Field(gt=0, le=3600)
    data_classification: Literal["synthetic_non_sensitive"] = "synthetic_non_sensitive"
    evidence_retention: Literal["m0_30_days"] = "m0_30_days"
    producer: Literal["pillarmesh-contract-service"] = "pillarmesh-contract-service"

    @model_validator(mode="after")
    def fixed_shape(self) -> Self:
        if self.projection != FIXED_PROJECTION:
            raise ValueError("projection must equal the fixed M0 projection")
        return self

    @property
    def freshness(self) -> timedelta:
        return timedelta(seconds=self.freshness_seconds)
