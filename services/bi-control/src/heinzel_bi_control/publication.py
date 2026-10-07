from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from threading import Lock
from typing import Protocol

from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_sdk import ProviderError
from heinzel_request_management import GovernedAnswerVerificationError
from pydantic import ValidationError

from .composition import (
    DashboardAnswerAuthorityReader,
    DashboardAuthorityUnavailable,
    DashboardCompositionError,
    DashboardNoValidPlan,
    DashboardStaleRevision,
)
from .models import (
    DashboardAnswerAuthority,
    DashboardProviderReceipt,
    DashboardPublicationAttempt,
    DashboardPublicationAttemptOutcome,
    DashboardPublicationFailureCode,
    DashboardPublicationIntent,
    DashboardPublicationRecord,
    DashboardPublicationState,
    DeclareDashboardPublicationCommand,
    PublishDashboardCommand,
)


def _publication_state(value: object) -> DashboardPublicationState:
    match value:
        case "pending" | "published" | "expired" | "failed":
            return value
        case _:
            raise ValueError("unknown dashboard publication state")


class DashboardPublisher(Protocol):
    def publish(self, command: PublishDashboardCommand) -> DashboardProviderReceipt: ...


class DashboardPublicationConflict(RuntimeError):
    pass


class DashboardPublicationNotPending(RuntimeError):
    pass


#
# A provider failure is retried only where the call plainly did not take effect. An ambiguous
# outcome is excluded on purpose: the dashboard may already exist externally, and re-applying is
# not demonstrably safe, so it settles as a terminal failure for an operator to resolve.
_RETRYABLE_PROVIDER_CLASSIFICATIONS: frozenset[str] = frozenset(
    {"retryable", "throttled", "transient_transport", "transient_unavailable"}
)
_AMBIGUOUS_PROVIDER_CLASSIFICATIONS: frozenset[str] = frozenset({"ambiguous", "ambiguous_outcome"})


def classify_publication_failure(
    error: DashboardCompositionError | ProviderError,
) -> DashboardPublicationFailureCode:
    """Name the failure a publication attempt records, without carrying the error's text."""
    if isinstance(error, DashboardAuthorityUnavailable):
        return "authority_unavailable"
    if isinstance(error, DashboardNoValidPlan):
        return "no_valid_plan"
    if isinstance(error, DashboardStaleRevision):
        return "stale_revision"
    if isinstance(error, DashboardCompositionError):
        return "authority_invalid"
    if error.classification in _RETRYABLE_PROVIDER_CLASSIFICATIONS:
        return "provider_unavailable"
    if error.classification in _AMBIGUOUS_PROVIDER_CLASSIFICATIONS:
        return "provider_ambiguous"
    return "provider_rejected"


class SQLiteDashboardPublicationRepository:
    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = Lock()
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_publication_intents ("
            "tenant_id TEXT NOT NULL, intent_id TEXT NOT NULL, state TEXT NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, intent_id))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_publication_attempts ("
            "tenant_id TEXT NOT NULL, intent_id TEXT NOT NULL, attempt INTEGER NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, intent_id, attempt))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def declare(self, intent: DashboardPublicationIntent) -> DashboardPublicationRecord:
        payload = canonical_bytes(intent)
        key = (intent.tenant_id, intent.intent_id)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT payload FROM dashboard_publication_intents "
                    "WHERE tenant_id = ? AND intent_id = ?",
                    key,
                ).fetchone()
                if existing is not None:
                    #
                    # Replay is judged on the declaring command, not on the intent derived from it:
                    # the intent carries its own declaration time and the deadline read at that
                    # moment, so re-declaring could never match byte for byte. Returning the stored
                    # record is also what keeps a re-declaration from extending the first window.
                    stored = DashboardPublicationIntent.model_validate_json(
                        existing[0], strict=True
                    )
                    if stored.command_digest != intent.command_digest:
                        raise DashboardPublicationConflict(
                            "conflicting dashboard publication intent replay"
                        )
                    record = self._read_locked(intent.tenant_id, intent.intent_id)
                    self._connection.commit()
                    return record
                self._connection.execute(
                    "INSERT INTO dashboard_publication_intents VALUES (?, ?, ?, ?)",
                    (*key, "pending", payload),
                )
                self._connection.commit()
                return DashboardPublicationRecord(intent=intent, state="pending", attempts=())
            except BaseException:
                with suppress(sqlite3.Error):
                    self._connection.rollback()
                raise

    def load(self, tenant_id: str, intent_id: str) -> DashboardPublicationRecord:
        with self._lock:
            return self._read_locked(tenant_id, intent_id)

    def record_attempt(
        self,
        attempt: DashboardPublicationAttempt,
        *,
        tenant_id: str,
        state: DashboardPublicationState,
    ) -> DashboardPublicationRecord:
        payload = canonical_bytes(attempt)
        key = (tenant_id, attempt.intent_id, attempt.attempt)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                current = self._read_locked(tenant_id, attempt.intent_id)
                if current.state != "pending":
                    raise DashboardPublicationNotPending(
                        "dashboard publication has already settled"
                    )
                if attempt.intent_digest != current.intent.intent_digest:
                    raise DashboardPublicationConflict(
                        "dashboard publication attempt does not belong to its intent"
                    )
                if attempt.attempt != len(current.attempts) + 1:
                    raise DashboardPublicationConflict(
                        "dashboard publication attempt is not contiguous"
                    )
                # Validates the whole record before either row is written, so a state the invariants
                # reject can never be committed and then read back as settled.
                settled = DashboardPublicationRecord(
                    intent=current.intent,
                    state=state,
                    attempts=(*current.attempts, attempt),
                )
                self._connection.execute(
                    "INSERT INTO dashboard_publication_attempts VALUES (?, ?, ?, ?)",
                    (*key, payload),
                )
                self._connection.execute(
                    "UPDATE dashboard_publication_intents SET state = ? "
                    "WHERE tenant_id = ? AND intent_id = ?",
                    (state, tenant_id, attempt.intent_id),
                )
                self._connection.commit()
                return settled
            except BaseException:
                with suppress(sqlite3.Error):
                    self._connection.rollback()
                raise

    def list_pending(self, tenant_id: str) -> tuple[DashboardPublicationIntent, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM dashboard_publication_intents "
                "WHERE tenant_id = ? AND state = 'pending' ORDER BY intent_id",
                (tenant_id,),
            ).fetchall()
        intents = tuple(
            DashboardPublicationIntent.model_validate_json(row[0], strict=True) for row in rows
        )
        if any(intent.tenant_id != tenant_id for intent in intents):
            raise ValueError("dashboard publication index does not match its payload")
        return intents

    def _read_locked(self, tenant_id: str, intent_id: str) -> DashboardPublicationRecord:
        row = self._connection.execute(
            "SELECT state, payload FROM dashboard_publication_intents "
            "WHERE tenant_id = ? AND intent_id = ?",
            (tenant_id, intent_id),
        ).fetchone()
        if row is None:
            raise LookupError((tenant_id, intent_id))
        intent = DashboardPublicationIntent.model_validate_json(row[1], strict=True)
        if intent.tenant_id != tenant_id or intent.intent_id != intent_id:
            raise ValueError("dashboard publication index does not match its payload")
        attempts = tuple(
            DashboardPublicationAttempt.model_validate_json(attempt_row[0], strict=True)
            for attempt_row in self._connection.execute(
                "SELECT payload FROM dashboard_publication_attempts "
                "WHERE tenant_id = ? AND intent_id = ? ORDER BY attempt",
                (tenant_id, intent_id),
            ).fetchall()
        )
        return DashboardPublicationRecord(
            intent=intent, state=_publication_state(row[0]), attempts=attempts
        )


class DashboardPublicationWorkflow:
    """Drive a declared publication to a settled outcome within its source snapshot's window."""

    def __init__(
        self,
        *,
        repository: SQLiteDashboardPublicationRepository,
        composition: DashboardPublisher,
        answers: DashboardAnswerAuthorityReader,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._composition = composition
        self._answers = answers
        self._clock = clock

    def declare(self, command: DeclareDashboardPublicationCommand) -> DashboardPublicationRecord:
        """Open a publication window bounded by the source snapshot the answer was read from."""
        answer = self._read_answer(command)
        return self._repository.declare(
            DashboardPublicationIntent(
                intent_id=command.intent_id,
                tenant_id=command.tenant_id,
                dashboard_id=command.dashboard_id,
                dashboard_version=command.dashboard_version,
                request_id=command.request_id,
                answer_id=command.answer_id,
                expected_revision=command.expected_revision,
                command_digest=digest(command),
                result_expires_at=answer.result_expires_at,
                declared_at=self._clock(),
            )
        )

    def _read_answer(self, command: DeclareDashboardPublicationCommand) -> DashboardAnswerAuthority:
        try:
            raw = self._answers.read_exact(
                tenant_id=command.tenant_id,
                request_id=command.request_id,
                answer_id=command.answer_id,
            )
        except GovernedAnswerVerificationError as error:
            raise DashboardCompositionError("dashboard answer authority is invalid") from error
        if raw is None:
            raise DashboardAuthorityUnavailable("dashboard answer authority is unavailable")
        try:
            answer = DashboardAnswerAuthority.model_validate(
                raw.model_dump(mode="python"), strict=True
            )
        except (AttributeError, ValidationError):
            raise DashboardCompositionError("dashboard answer authority is invalid") from None
        if (
            answer.tenant_id != command.tenant_id
            or answer.request_id != command.request_id
            or answer.answer_id != command.answer_id
        ):
            raise DashboardCompositionError("dashboard answer identity does not match command")
        return answer

    def advance(self, *, tenant_id: str, intent_id: str) -> DashboardPublicationRecord:
        record = self._repository.load(tenant_id, intent_id)
        if record.state != "pending":
            return record
        intent = record.intent
        attempt_number = len(record.attempts) + 1
        observed_at = self._clock()
        if observed_at >= intent.result_expires_at:
            #
            # Past the deadline the provider is never reached. The source snapshot can no longer be
            # read back, so a dashboard compiled from it could not produce its own evidence.
            return self._settle(
                intent=intent,
                attempt=attempt_number,
                outcome="expired",
                observed_at=observed_at,
                state="expired",
            )
        try:
            receipt = self._composition.publish(intent.publish_command())
        except (DashboardCompositionError, ProviderError) as error:
            #
            # The failure belongs to the dashboard, not to the answer that was delivered. It is
            # recorded against the intent and returned to the caller; only a failure whose cause can
            # clear leaves the intent pending for another attempt. Any other exception propagates
            # with the intent still pending rather than being classified as something it is not.
            failure_code = classify_publication_failure(error)
            attempt = DashboardPublicationAttempt(
                intent_id=intent.intent_id,
                intent_digest=intent.intent_digest,
                attempt=attempt_number,
                outcome="failed",
                failure_code=failure_code,
                observed_at=observed_at,
            )
            return self._repository.record_attempt(
                attempt,
                tenant_id=intent.tenant_id,
                state="pending" if attempt.retryable else "failed",
            )
        return self._settle(
            intent=intent,
            attempt=attempt_number,
            outcome="published",
            observed_at=observed_at,
            state="published",
            dashboard_revision=receipt.revision,
            desired_digest=receipt.desired_digest,
        )

    def drain(self, tenant_id: str) -> tuple[DashboardPublicationRecord, ...]:
        #
        # An unexpected failure on one intent stops the drain rather than being stepped over: the
        # intents it has not reached stay pending and are advanced by the next drain, which is safe
        # because every outcome here is idempotent. Continuing would mean deciding that an error
        # nothing classified is harmless.
        return tuple(
            self.advance(tenant_id=tenant_id, intent_id=intent.intent_id)
            for intent in self._repository.list_pending(tenant_id)
        )

    def read(self, *, tenant_id: str, intent_id: str) -> DashboardPublicationRecord:
        return self._repository.load(tenant_id, intent_id)

    def _settle(
        self,
        *,
        intent: DashboardPublicationIntent,
        attempt: int,
        outcome: DashboardPublicationAttemptOutcome,
        observed_at: datetime,
        state: DashboardPublicationState,
        dashboard_revision: int | None = None,
        desired_digest: str | None = None,
    ) -> DashboardPublicationRecord:
        return self._repository.record_attempt(
            DashboardPublicationAttempt(
                intent_id=intent.intent_id,
                intent_digest=intent.intent_digest,
                attempt=attempt,
                outcome=outcome,
                dashboard_revision=dashboard_revision,
                desired_digest=desired_digest,
                observed_at=observed_at,
            ),
            tenant_id=intent.tenant_id,
            state=state,
        )
