from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import timedelta
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import Field, field_validator, model_validator

from .acquisition_models import (
    AcquisitionBoundary,
    AcquisitionIntent,
    AcquisitionObjectSchema,
    AcquisitionRecord,
)
from .errors import AcquisitionProviderKind
from .models import ProviderModel, ProviderObservation


class SourceObservationRequest(ProviderModel):
    tenant_id: str = Field(min_length=1)
    source_binding_ref: str = Field(min_length=1)
    object_refs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def requires_canonical_objects(self) -> Self:
        if len(self.object_refs) != len(set(self.object_refs)):
            raise ValueError("object_refs must be unique")
        if self.object_refs != tuple(sorted(self.object_refs)):
            raise ValueError("object_refs must be in canonical order")
        return self


class AcquisitionObjectObservation(ProviderModel):
    schema_version: Literal["1"] = "1"
    logical_object_ref: str = Field(min_length=1)
    provider_observation: ProviderObservation

    @model_validator(mode="after")
    def requires_utc_observation_time(self) -> Self:
        observed_at = self.provider_observation.observed_at
        if observed_at is not None and observed_at.utcoffset() != timedelta(0):
            raise ValueError("observed_at must be UTC")
        return self


class AcquisitionSourceObservation(ProviderModel):
    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    source_binding_ref: str = Field(min_length=1)
    provider_kind: AcquisitionProviderKind
    object_observations: tuple[AcquisitionObjectObservation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def requires_canonical_provider_objects(self) -> Self:
        object_refs = tuple(item.logical_object_ref for item in self.object_observations)
        if len(object_refs) != len(set(object_refs)):
            raise ValueError("object observations must be unique")
        if object_refs != tuple(sorted(object_refs)):
            raise ValueError("object observations must be in canonical order")
        if any(
            item.provider_observation.provider != self.provider_kind
            for item in self.object_observations
        ):
            raise ValueError("object observation provider must match provider_kind")
        return self


class AcquisitionSessionIncomplete(RuntimeError):
    def __init__(self) -> None:
        super().__init__("acquisition_session_incomplete")


class CompletedAcquisition(ProviderModel):
    schema_version: Literal["1"] = "1"
    boundaries: tuple[AcquisitionBoundary, ...] = Field(min_length=1)
    cursor_version: str = Field(min_length=1, strict=True)
    candidate_cursor_payload: bytes = Field(repr=False)

    @field_validator("candidate_cursor_payload", mode="before")
    @classmethod
    def requires_exact_cursor_bytes(cls, value: object) -> bytes:
        if type(value) is not bytes:
            raise ValueError("candidate_cursor_payload must be bytes")
        return value

    @property
    def candidate_cursor_digest(self) -> str:
        return hashlib.sha256(self.candidate_cursor_payload).hexdigest()


class AcquisitionArtifactReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...


@runtime_checkable
class AcquisitionSession(Protocol):
    def __iter__(self) -> Iterator[AcquisitionRecord]: ...

    def complete(self) -> CompletedAcquisition: ...

    def abort(self) -> None: ...


@runtime_checkable
class AcquisitionProvider(Protocol):
    def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation: ...

    def open_acquisition(
        self,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        private_cursor: bytes | None,
    ) -> AcquisitionSession: ...
