from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from .run_models import (
    RunAttemptClaim,
    RunAttemptCompletion,
    RunCancellation,
    RunIntent,
    RunRecord,
    RunRetryRequest,
)
from .run_repository import SQLiteRunRepository


class RunService:
    def __init__(self, repository: SQLiteRunRepository, *, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    def materialize(self, intent: RunIntent) -> RunRecord:
        existing = self._repository.load_by_intent(intent.tenant_id, intent.intent_digest)
        if existing is not None:
            return existing
        run = RunRecord(
            run_id=f"run-{intent.intent_digest[:32]}",
            intent=intent,
            intent_digest=intent.intent_digest,
            created_at=self._clock(),
        )
        return self._repository.insert_run(run)

    def list_runs(self, tenant_id: str) -> tuple[RunRecord, ...]:
        return self._repository.list_runs(tenant_id)

    def list_retry_requests(self, tenant_id: str, run_id: str) -> tuple[RunRetryRequest, ...]:
        self._repository.load_owned(tenant_id, run_id)
        return self._repository.list_retry_requests(run_id)

    def claim(
        self,
        *,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> RunAttemptClaim:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._clock()
        with self._repository.transaction():
            self._repository.load_owned(tenant_id, run_id)
            if self._repository.load_cancellation(run_id) is not None:
                raise ValueError("run is cancelled")
            latest = self._repository.latest_claim(run_id)
            if latest is not None:
                completion = self._repository.load_completion(run_id, latest.attempt_number)
                if completion is not None:
                    if (
                        completion.outcome == "succeeded"
                        or completion.failure_classification == "permanent"
                    ):
                        raise ValueError("run is already terminal")
                elif latest.lease_expires_at > now:
                    raise ValueError("run is already leased")
            claim = RunAttemptClaim(
                run_id=run_id,
                attempt_number=1 if latest is None else latest.attempt_number + 1,
                epoch=1 if latest is None else latest.epoch + 1,
                worker_id=worker_id,
                claimed_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            self._repository.insert_claim(claim)
        return claim

    def require_current_attempt(self, *, tenant_id: str, claim: RunAttemptClaim) -> None:
        """Refuse unless the claim is still the live, unfinished attempt of an active run.

        A worker checks this before each effectful stage, so a worker whose lease expired or was
        superseded stops at the next stage boundary instead of carrying on beside its successor.
        """
        now = self._clock()
        with self._repository.transaction():
            self._repository.load_owned(tenant_id, claim.run_id)
            if self._repository.load_cancellation(claim.run_id) is not None:
                raise ValueError("run is cancelled")
            latest = self._repository.latest_claim(claim.run_id)
            if latest != claim:
                raise ValueError("stale run epoch")
            if self._repository.load_completion(claim.run_id, claim.attempt_number) is not None:
                raise ValueError("run attempt is already complete")
            if latest.lease_expires_at <= now:
                raise ValueError("run lease expired")

    def complete(
        self,
        *,
        tenant_id: str,
        run_id: str,
        attempt_number: int,
        epoch: int,
        worker_id: str,
        outcome: Literal["succeeded", "failed"],
        durable_boundary_ref: str,
        failure_classification: Literal["transient", "permanent"] | None = None,
    ) -> RunAttemptCompletion:
        now = self._clock()
        candidate = RunAttemptCompletion(
            run_id=run_id,
            attempt_number=attempt_number,
            epoch=epoch,
            worker_id=worker_id,
            outcome=outcome,
            failure_classification=failure_classification,
            durable_boundary_ref=durable_boundary_ref,
            completed_at=now,
        )
        with self._repository.transaction():
            self._repository.load_owned(tenant_id, run_id)
            if self._repository.load_cancellation(run_id) is not None:
                raise ValueError("run is cancelled")
            existing = self._repository.load_completion(run_id, attempt_number)
            if existing is not None:
                authority = (
                    existing.run_id,
                    existing.attempt_number,
                    existing.epoch,
                    existing.worker_id,
                    existing.outcome,
                    existing.failure_classification,
                    existing.durable_boundary_ref,
                )
                requested = (
                    run_id,
                    attempt_number,
                    epoch,
                    worker_id,
                    outcome,
                    failure_classification,
                    durable_boundary_ref,
                )
                if authority != requested:
                    raise ValueError("run completion conflicts with recorded outcome")
                return existing
            latest = self._repository.latest_claim(run_id)
            if (
                latest is None
                or latest.attempt_number != attempt_number
                or latest.epoch != epoch
                or latest.worker_id != worker_id
            ):
                raise ValueError("stale run epoch")
            if latest.lease_expires_at <= now:
                raise ValueError("run lease expired")
            self._repository.insert_completion(candidate)
        return candidate

    def cancel(
        self,
        *,
        tenant_id: str,
        run_id: str,
        cancelled_by: str,
        reason: str,
    ) -> RunCancellation:
        candidate = RunCancellation(
            run_id=run_id,
            cancelled_by=cancelled_by,
            reason=reason,
            cancelled_at=self._clock(),
        )
        with self._repository.transaction():
            self._repository.load_owned(tenant_id, run_id)
            existing = self._repository.load_cancellation(run_id)
            if existing is not None:
                if (existing.cancelled_by, existing.reason) != (cancelled_by, reason):
                    raise ValueError("run cancellation conflicts with recorded cancellation")
                return existing
            latest = self._repository.latest_claim(run_id)
            if (
                latest is not None
                and self._repository.load_completion(run_id, latest.attempt_number) is not None
            ):
                raise ValueError("run is already terminal")
            self._repository.insert_cancellation(candidate)
        return candidate

    def request_retry(
        self,
        *,
        tenant_id: str,
        run_id: str,
        failed_attempt_number: int,
        command_id: str,
        incident_id: str,
        incident_revision: int,
        requested_by: str,
        reason: str,
    ) -> RunRetryRequest:
        with self._repository.transaction():
            self._repository.load_owned(tenant_id, run_id)
            replay = self._repository.load_retry_request_by_command(run_id, command_id)
            if replay is not None:
                requested_authority = (
                    command_id,
                    run_id,
                    failed_attempt_number,
                    requested_by,
                    reason,
                    incident_id,
                    incident_revision,
                )
                if _retry_request_command_authority(replay) != requested_authority:
                    raise ValueError("retry command conflicts with recorded request")
                return replay
            if self._repository.load_cancellation(run_id) is not None:
                raise ValueError("run is cancelled")
            existing = self._repository.load_retry_request_for_attempt(
                run_id, failed_attempt_number
            )
            if existing is not None:
                raise ValueError("failed attempt already has a retry request")
            latest = self._repository.latest_claim(run_id)
            if latest is None or latest.attempt_number != failed_attempt_number:
                raise ValueError("retry does not target the latest failed attempt")
            completion = self._repository.load_completion(run_id, failed_attempt_number)
            if (
                completion is None
                or completion.outcome != "failed"
                or completion.failure_classification != "transient"
            ):
                raise ValueError("retry requires a transient failure")
            candidate = RunRetryRequest(
                command_id=command_id,
                run_id=run_id,
                failed_attempt_number=failed_attempt_number,
                failed_epoch=latest.epoch,
                requested_by=requested_by,
                reason=reason,
                incident_id=incident_id,
                incident_revision=incident_revision,
                requested_at=self._clock(),
            )
            self._repository.insert_retry_request(candidate)
        return candidate

    def cancel_unstarted(
        self,
        *,
        tenant_id: str,
        run_id: str,
        cancelled_by: str,
        reason: str,
    ) -> RunCancellation:
        candidate = RunCancellation(
            run_id=run_id,
            cancelled_by=cancelled_by,
            reason=reason,
            cancelled_at=self._clock(),
        )
        with self._repository.transaction():
            self._repository.load_owned(tenant_id, run_id)
            existing = self._repository.load_cancellation(run_id)
            if existing is not None:
                if (existing.cancelled_by, existing.reason) != (cancelled_by, reason):
                    raise ValueError("run cancellation conflicts with recorded cancellation")
                return existing
            if self._repository.latest_claim(run_id) is not None:
                raise ValueError("run has already started")
            self._repository.insert_cancellation(candidate)
        return candidate


def _retry_request_command_authority(request: RunRetryRequest) -> tuple[object, ...]:
    return (
        request.command_id,
        request.run_id,
        request.failed_attempt_number,
        request.requested_by,
        request.reason,
        request.incident_id,
        request.incident_revision,
    )
