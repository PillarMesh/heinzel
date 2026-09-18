"""Execute a state-owned run's stages under its lease, one durable boundary at a time.

State owns run identity, attempts, leases and epochs. This executor claims an attempt, checks
before every stage that the claim is still the live attempt of an active run, and completes the
attempt at the last boundary it proved. It keeps no progress of its own: each stage must be
replay-stable against its own durable store, so a successor attempt resumes by replaying the
stages that already committed -- which return their stored receipts without repeating effects --
and continuing from the first that did not.

Fencing is at stage boundaries. A worker that loses its lease mid-stage may still finish that
stage's effect; the stage's own idempotency and compare-and-set make that indistinguishable
from the successor's replay, and the stale worker can neither start a later stage nor record a
terminal outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from pillarmesh_provider_sdk import ProviderError
from pillarmesh_state import RunAttemptClaim, RunAttemptLeaseExtension, RunService
from pillarmesh_state.run_models import RunAttemptCompletion

from .acquisition_errors import AcquisitionThrottledError, AcquisitionTransientError

type RunFailureClassification = Literal["transient", "permanent"]

_NO_BOUNDARY = "none"
# RunService refusals that mean this worker is no longer the live attempt. Anything else it raises,
# such as a completion that conflicts with a recorded outcome, is an integrity problem and is not
# reported as ordinary lease loss.
_FENCING_REFUSALS = frozenset(
    (
        "stale run epoch",
        "run lease expired",
        "run is cancelled",
        "run attempt is already complete",
    )
)
_TRANSIENT_PROVIDER_CLASSIFICATIONS = frozenset(
    (
        "retryable",
        "throttled",
        "ambiguous",
        "transient_transport",
        "transient_unavailable",
        "ambiguous_outcome",
    )
)


class RunLease:
    """A stage's handle on its attempt: the claim it runs under, and a way to renew it.

    A stage that may outlive its lease renews it while working, so the attempt is not fenced
    mid-stage. Renewal is refused once the attempt is no longer the live one, which reaches the
    stage as `RunLeaseLostError` rather than as a silent continuation.
    """

    def __init__(
        self,
        runs: RunService,
        *,
        tenant_id: str,
        claim: RunAttemptClaim,
        lease_seconds: int,
    ) -> None:
        self._runs = runs
        self._tenant_id = tenant_id
        self._claim = claim
        self._lease_seconds = lease_seconds

    @property
    def claim(self) -> RunAttemptClaim:
        return self._claim

    def extend(self, lease_seconds: int | None = None) -> RunAttemptLeaseExtension:
        try:
            return self._runs.extend_lease(
                tenant_id=self._tenant_id,
                claim=self._claim,
                lease_seconds=self._lease_seconds if lease_seconds is None else lease_seconds,
            )
        except ValueError as error:
            _raise_if_fenced(error, None)
            raise
        except Exception as error:
            raise RunLeaseRenewalUnavailableError("run lease renewal is unavailable") from error


@dataclass(frozen=True)
class RunStage:
    """One replay-stable stage that returns the reference of the durable boundary it proved."""

    boundary: str
    execute: Callable[[RunLease], str]


@dataclass(frozen=True)
class LeasedRunOutcome:
    claim: RunAttemptClaim
    boundaries: tuple[tuple[str, str], ...]
    completion: RunAttemptCompletion


class RunLeaseLostError(RuntimeError):
    """The attempt is no longer current; the worker stopped without recording an outcome."""


class RunLeaseRenewalUnavailableError(RuntimeError):
    """The lease could not be renewed for a reason that is not fencing.

    The attempt may well still be live, so this is retryable: a state-service outage must not
    terminate a healthy run.
    """


class RunStageFailedError(RuntimeError):
    """A stage failed and the attempt recorded it.

    The message names only the stage, classification and boundary, so provider text never reaches
    it; the stage's exception is chained as the cause for diagnosis.
    """

    def __init__(self, boundary: str, completion: RunAttemptCompletion, error_type: str) -> None:
        super().__init__(
            f"run stage {boundary} failed ({completion.failure_classification}, {error_type}); "
            f"last durable boundary {completion.durable_boundary_ref}"
        )
        self.boundary = boundary
        self.completion = completion
        self.error_type = error_type


def classify_run_stage_failure(error: Exception) -> RunFailureClassification:
    """Transient only when a retry is known to be safe; anything unrecognized needs an operator."""
    if isinstance(
        error,
        (AcquisitionTransientError, AcquisitionThrottledError, RunLeaseRenewalUnavailableError),
    ):
        return "transient"
    if (
        isinstance(error, ProviderError)
        and error.classification in _TRANSIENT_PROVIDER_CLASSIFICATIONS
    ):
        return "transient"
    return "permanent"


class LeasedRunExecutor:
    def __init__(
        self,
        runs: RunService,
        *,
        worker_id: str,
        lease_seconds: int,
        classify: Callable[[Exception], RunFailureClassification] = classify_run_stage_failure,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._runs = runs
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._classify = classify

    def execute(
        self,
        *,
        tenant_id: str,
        run_id: str,
        stages: Sequence[RunStage],
    ) -> LeasedRunOutcome:
        names = tuple(stage.boundary for stage in stages)
        if not names or any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("run stages must have unique, non-empty boundary names")
        claim = self._runs.claim(
            tenant_id=tenant_id,
            run_id=run_id,
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        proven: list[tuple[str, str]] = []
        for stage in stages:
            self._require_current(tenant_id, claim)
            try:
                reference = stage.execute(
                    RunLease(
                        self._runs,
                        tenant_id=tenant_id,
                        claim=claim,
                        lease_seconds=self._lease_seconds,
                    )
                )
            except Exception as error:
                classification = self._classify(error)
                self._require_current(tenant_id, claim, cause=error)
                completion = self._complete(
                    tenant_id,
                    claim,
                    outcome="failed",
                    boundary=_boundary_ref(proven),
                    classification=classification,
                    cause=error,
                )
                raise RunStageFailedError(
                    stage.boundary, completion, type(error).__name__
                ) from error
            if not reference:
                raise ValueError(f"run stage {stage.boundary} returned no boundary reference")
            proven.append((stage.boundary, reference))
        self._require_current(tenant_id, claim)
        completion = self._complete(
            tenant_id, claim, outcome="succeeded", boundary=_boundary_ref(proven)
        )
        return LeasedRunOutcome(claim=claim, boundaries=tuple(proven), completion=completion)

    def _require_current(
        self, tenant_id: str, claim: RunAttemptClaim, *, cause: Exception | None = None
    ) -> None:
        try:
            self._runs.require_current_attempt(tenant_id=tenant_id, claim=claim)
        except ValueError as error:
            _raise_if_fenced(error, cause)
            raise

    def _complete(
        self,
        tenant_id: str,
        claim: RunAttemptClaim,
        *,
        outcome: Literal["succeeded", "failed"],
        boundary: str,
        classification: RunFailureClassification | None = None,
        cause: Exception | None = None,
    ) -> RunAttemptCompletion:
        try:
            return self._runs.complete(
                tenant_id=tenant_id,
                run_id=claim.run_id,
                attempt_number=claim.attempt_number,
                epoch=claim.epoch,
                worker_id=claim.worker_id,
                outcome=outcome,
                durable_boundary_ref=boundary,
                failure_classification=classification,
            )
        except ValueError as error:
            _raise_if_fenced(error, cause)
            raise


def _raise_if_fenced(error: ValueError, cause: Exception | None) -> None:
    """Translate a fencing refusal, keeping a stage failure found alongside it as the cause."""
    if str(error) in _FENCING_REFUSALS:
        raise RunLeaseLostError(str(error)) from cause


def _boundary_ref(proven: Sequence[tuple[str, str]]) -> str:
    if not proven:
        return _NO_BOUNDARY
    boundary, reference = proven[-1]
    return f"{boundary}:{reference}"
