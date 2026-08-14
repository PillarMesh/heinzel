from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import (
    CommitReceipt,
    DriftProbe,
    OrderRow,
    ProviderObservation,
    SegmentManifest,
    SourceBoundary,
    VisibilityProof,
)


@runtime_checkable
class SourceProvider(Protocol):
    def observe(self) -> ProviderObservation: ...

    def drift_probe(self) -> DriftProbe: ...

    def read_snapshot(self) -> tuple[SourceBoundary, Iterable[OrderRow]]: ...


@runtime_checkable
class DestinationProvider(Protocol):
    def observe(self) -> ProviderObservation: ...

    def stage(self, segment: Path, manifest: SegmentManifest) -> None: ...

    def commit_or_resolve(self, manifest: SegmentManifest) -> CommitReceipt: ...

    def verify_visibility(
        self, manifest: SegmentManifest, acceptance_key: int
    ) -> VisibilityProof: ...
