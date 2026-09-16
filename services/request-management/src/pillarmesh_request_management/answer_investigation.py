from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from pillarmesh_contract_model import ArtifactModel, canonical_bytes, digest
from pydantic import Field, ValidationError, field_validator

from .answer_models import AnswerIntentValidation
from .answer_validation_repository import AnswerValidationConflict, SQLiteAnswerValidationRepository
from .models import InboxRequest, RequestState
from .repository import SQLiteRequestRepository, StaleRevisionError

_INVESTIGATION_ACTOR = "system:answer-investigation"


class AnswerInvestigationDenied(ValueError):
    pass


class ConfirmAnswerInvestigation(ArtifactModel):
    tenant_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    expected_revision: int = Field(gt=0)
    requester_id: str = Field(min_length=1)
    validation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class AnswerInvestigationConfirmation(ArtifactModel):
    schema_version: Literal["1"] = "1"
    confirmation_id: str = Field(pattern=r"^investigation-[0-9a-f]{64}$")
    command: ConfirmAnswerInvestigation
    validation_request_revision: int = Field(gt=0)
    confirmed_revision: int = Field(gt=0)
    confirmed_at: datetime

    @field_validator("confirmed_at")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("confirmation timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)


class AnswerInvestigationReader(Protocol):
    def confirmed_revision(
        self,
        *,
        tenant_id: str,
        request_id: str,
        requester_id: str,
        validation: AnswerIntentValidation,
    ) -> int | None: ...


class SQLiteAnswerInvestigationAuthority:
    def __init__(self, connection: sqlite3.Connection, *, clock: Callable[[], datetime]) -> None:
        self._connection = connection
        self._clock = clock
        self._requests = SQLiteRequestRepository(connection)
        self._validations = SQLiteAnswerValidationRepository(connection)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS answer_investigation_confirmations ("
            "tenant_id TEXT NOT NULL, confirmation_id TEXT NOT NULL, request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, validation_digest TEXT NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, confirmation_id), "
            "UNIQUE (tenant_id, request_id, request_revision))"
        )
        connection.commit()

    def confirm(self, command: ConfirmAnswerInvestigation) -> AnswerInvestigationConfirmation:
        command = ConfirmAnswerInvestigation.model_validate(command.model_dump(), strict=True)
        confirmation_id = "investigation-" + digest(command)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            recorded = self.read(tenant_id=command.tenant_id, confirmation_id=confirmation_id)
            if recorded is not None:
                if recorded.command != command:
                    raise AnswerInvestigationDenied("investigation confirmation identity conflicts")
                self._connection.commit()
                return recorded
            request = self._requests.load_owned_request(command.tenant_id, command.request_id)
            if request.requester_id != command.requester_id:
                raise AnswerInvestigationDenied("investigation confirmation is not authorized")
            if (
                request.revision != command.expected_revision
                or request.state is not RequestState.SUBMITTED
            ):
                raise AnswerInvestigationDenied("investigation confirmation is stale")
            result = self._validations.read_result(
                tenant_id=command.tenant_id,
                request_id=command.request_id,
                validation_digest=command.validation_digest,
            )
            if result is None or result.validation.outcome != "admitted":
                raise AnswerInvestigationDenied("investigation validation is unavailable")
            if result.restatement_confirmation_required:
                raise AnswerInvestigationDenied("investigation validation requires confirmation")
            validation = result.validation
            if not (
                validation.request_revision == command.expected_revision
                and result.intent.question_digest == digest(request.payload)
                and request.payload.request_type == "stakeholder_question"
            ):
                raise AnswerInvestigationDenied("investigation question has changed")
            confirmed_at = self._clock()
            if confirmed_at < max(validation.created_at, request.updated_at):
                raise AnswerInvestigationDenied("investigation confirmation predates its authority")
            confirmation = AnswerInvestigationConfirmation(
                confirmation_id=confirmation_id,
                command=command,
                validation_request_revision=validation.request_revision,
                confirmed_revision=command.expected_revision + 1,
                confirmed_at=confirmed_at,
            )
            self._connection.execute(
                "INSERT INTO answer_investigation_confirmations "
                "(tenant_id, confirmation_id, request_id, request_revision, "
                "validation_digest, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    command.tenant_id,
                    confirmation_id,
                    command.request_id,
                    command.expected_revision,
                    command.validation_digest,
                    canonical_bytes(confirmation),
                ),
            )
            self._requests.transition_in_transaction(
                tenant_id=command.tenant_id,
                request_id=command.request_id,
                expected_revision=command.expected_revision,
                actor_id=_INVESTIGATION_ACTOR,
                to_state=RequestState.INVESTIGATING,
                created_at=confirmed_at,
            )
            self._connection.commit()
            return confirmation
        except AnswerInvestigationDenied:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
        except (
            KeyError,
            sqlite3.Error,
            ValidationError,
            AnswerValidationConflict,
            StaleRevisionError,
        ):
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise AnswerInvestigationDenied("investigation confirmation is unavailable") from None
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise

    def read(
        self, *, tenant_id: str, confirmation_id: str
    ) -> AnswerInvestigationConfirmation | None:
        try:
            return self._read(tenant_id=tenant_id, confirmation_id=confirmation_id)
        except AnswerInvestigationDenied:
            raise
        except (sqlite3.Error, ValidationError, AnswerValidationConflict):
            raise AnswerInvestigationDenied("stored investigation authority is invalid") from None

    def _read(
        self, *, tenant_id: str, confirmation_id: str
    ) -> AnswerInvestigationConfirmation | None:
        row = self._connection.execute(
            "SELECT request_id, request_revision, validation_digest, payload "
            "FROM answer_investigation_confirmations "
            "WHERE tenant_id = ? AND confirmation_id = ?",
            (tenant_id, confirmation_id),
        ).fetchone()
        if row is None:
            return None
        if not isinstance(row[3], bytes):
            raise AnswerInvestigationDenied("stored investigation authority is invalid")
        confirmation = AnswerInvestigationConfirmation.model_validate_json(row[3], strict=True)
        command = confirmation.command
        if not (
            command.tenant_id == tenant_id
            and confirmation.confirmation_id == confirmation_id
            and confirmation_id == "investigation-" + digest(command)
            and command.request_id == row[0]
            and command.expected_revision == row[1]
            and command.validation_digest == row[2]
            and confirmation.validation_request_revision == command.expected_revision
            and confirmation.confirmed_revision == command.expected_revision + 1
        ):
            raise AnswerInvestigationDenied("stored investigation authority is invalid")
        result = self._validations.read_result(
            tenant_id=tenant_id,
            request_id=command.request_id,
            validation_digest=command.validation_digest,
        )
        if result is None or not (
            result.validation.outcome == "admitted"
            and not result.restatement_confirmation_required
            and result.validation.request_revision == confirmation.validation_request_revision
            and confirmation.confirmed_at >= result.validation.created_at
        ):
            raise AnswerInvestigationDenied("stored investigation validation is invalid")
        before = self._read_revision(
            tenant_id=tenant_id,
            request_id=command.request_id,
            revision=command.expected_revision,
        )
        after = self._read_revision(
            tenant_id=tenant_id,
            request_id=command.request_id,
            revision=confirmation.confirmed_revision,
        )
        transitions = tuple(
            event
            for event in self._requests.list_transition_history(tenant_id, command.request_id)
            if event.request_revision == confirmation.confirmed_revision
        )
        if len(transitions) != 1 or not (
            before.state is RequestState.SUBMITTED
            and before.requester_id == command.requester_id
            and before.payload.request_type == "stakeholder_question"
            and result.intent.question_digest == digest(before.payload)
            and before.updated_at <= confirmation.confirmed_at
            and after
            == before.model_copy(
                update={
                    "state": RequestState.INVESTIGATING,
                    "revision": confirmation.confirmed_revision,
                    "updated_at": confirmation.confirmed_at,
                }
            )
            and transitions[0].request_id == command.request_id
            and transitions[0].actor_id == _INVESTIGATION_ACTOR
            and transitions[0].from_state is RequestState.SUBMITTED
            and transitions[0].to_state is RequestState.INVESTIGATING
            and transitions[0].created_at == confirmation.confirmed_at
        ):
            raise AnswerInvestigationDenied("stored investigation continuity is invalid")
        return confirmation

    def confirmed_revision(
        self,
        *,
        tenant_id: str,
        request_id: str,
        requester_id: str,
        validation: AnswerIntentValidation,
    ) -> int | None:
        command = ConfirmAnswerInvestigation(
            tenant_id=tenant_id,
            request_id=request_id,
            expected_revision=validation.request_revision,
            requester_id=requester_id,
            validation_digest=digest(validation),
        )
        confirmation = self.read(
            tenant_id=tenant_id,
            confirmation_id="investigation-" + digest(command),
        )
        if confirmation is not None and (
            confirmation.command.request_id == request_id == validation.request_id
            and tenant_id == validation.tenant_id
            and confirmation.command.requester_id == requester_id
            and confirmation.validation_request_revision == validation.request_revision
        ):
            return confirmation.confirmed_revision
        return None

    def _read_revision(self, *, tenant_id: str, request_id: str, revision: int) -> InboxRequest:
        row = self._connection.execute(
            "SELECT payload FROM request_revisions "
            "WHERE tenant_id = ? AND request_id = ? AND revision = ?",
            (tenant_id, request_id, revision),
        ).fetchone()
        if row is None or not isinstance(row[0], bytes):
            raise AnswerInvestigationDenied("stored investigation continuity is unavailable")
        request = InboxRequest.model_validate_json(row[0], strict=True)
        if not (
            request.tenant_id == tenant_id
            and request.request_id == request_id
            and request.revision == revision
        ):
            raise AnswerInvestigationDenied("stored investigation continuity is invalid")
        return request
