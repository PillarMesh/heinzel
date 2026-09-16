from __future__ import annotations

import sqlite3
from contextlib import suppress
from typing import Protocol

from pillarmesh_contract_model import canonical_bytes, digest
from pydantic import ValidationError

from .answer_models import (
    AnswerIntentValidation,
    AnswerQuestionValidationResult,
)


class AnswerValidationConflict(ValueError):
    pass


class AnswerValidationRepository(Protocol):
    def save(self, result: AnswerQuestionValidationResult) -> AnswerQuestionValidationResult: ...

    def read_result(
        self, *, tenant_id: str, request_id: str, validation_digest: str
    ) -> AnswerQuestionValidationResult | None: ...

    def read_validation(
        self, *, tenant_id: str, request_id: str, validation_digest: str
    ) -> AnswerIntentValidation | None: ...


class SQLiteAnswerValidationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS answer_intent_validations ("
            "tenant_id TEXT NOT NULL, request_id TEXT NOT NULL, validation_id TEXT NOT NULL, "
            "validation_digest TEXT NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, validation_id), "
            "UNIQUE (tenant_id, request_id, validation_digest))"
        )
        self._connection.commit()

    def save(self, result: AnswerQuestionValidationResult) -> AnswerQuestionValidationResult:
        validated = AnswerQuestionValidationResult.model_validate(
            result.model_dump(mode="python"), strict=True
        )
        validation = validated.validation
        intent = validated.intent
        if (
            validation.tenant_id != intent.tenant_id
            or validation.request_id != intent.request_id
            or validation.request_revision != intent.request_revision
            or validation.intent_digest != digest(intent)
        ):
            raise AnswerValidationConflict("validation does not bind its owning intent")
        validation_digest = digest(validation)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            rows = self._connection.execute(
                "SELECT tenant_id, request_id, validation_id, validation_digest, payload "
                "FROM answer_intent_validations WHERE tenant_id = ? AND "
                "(validation_id = ? OR (request_id = ? AND validation_digest = ?))",
                (
                    validation.tenant_id,
                    validation.validation_id,
                    validation.request_id,
                    validation_digest,
                ),
            ).fetchall()
            if rows:
                if len(rows) != 1:
                    raise AnswerValidationConflict("validation identity is already bound")
                recorded = self._validate_row(
                    rows[0],
                    tenant_id=validation.tenant_id,
                    request_id=validation.request_id,
                    validation_digest=validation_digest,
                )
                if recorded != validated:
                    raise AnswerValidationConflict("validation identity is already bound")
                self._connection.commit()
                return recorded
            self._connection.execute(
                "INSERT INTO answer_intent_validations "
                "(tenant_id, request_id, validation_id, validation_digest, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    validation.tenant_id,
                    validation.request_id,
                    validation.validation_id,
                    validation_digest,
                    canonical_bytes(validated),
                ),
            )
            self._connection.commit()
            return validated
        except sqlite3.IntegrityError as error:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise AnswerValidationConflict("validation identity is already bound") from error
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise

    def read_validation(
        self, *, tenant_id: str, request_id: str, validation_digest: str
    ) -> AnswerIntentValidation | None:
        result = self.read_result(
            tenant_id=tenant_id,
            request_id=request_id,
            validation_digest=validation_digest,
        )
        return None if result is None else result.validation

    def read_result(
        self, *, tenant_id: str, request_id: str, validation_digest: str
    ) -> AnswerQuestionValidationResult | None:
        row = self._connection.execute(
            "SELECT tenant_id, request_id, validation_id, validation_digest, payload "
            "FROM answer_intent_validations "
            "WHERE tenant_id = ? AND request_id = ? AND validation_digest = ?",
            (tenant_id, request_id, validation_digest),
        ).fetchone()
        if row is None:
            return None
        return self._validate_row(
            row,
            tenant_id=tenant_id,
            request_id=request_id,
            validation_digest=validation_digest,
        )

    @staticmethod
    def _validate_row(
        row: tuple[object, ...],
        *,
        tenant_id: str,
        request_id: str,
        validation_digest: str,
    ) -> AnswerQuestionValidationResult:
        payload = row[4]
        if not isinstance(payload, bytes):
            raise AnswerValidationConflict("stored validation authority does not match its key")
        try:
            result = AnswerQuestionValidationResult.model_validate_json(payload, strict=True)
        except ValidationError as error:
            raise AnswerValidationConflict(
                "stored validation authority does not match its key"
            ) from error
        validation = result.validation
        intent = result.intent
        if not (
            row[0] == tenant_id == validation.tenant_id == intent.tenant_id
            and row[1] == request_id == validation.request_id == intent.request_id
            and row[2] == validation.validation_id
            and row[3] == validation_digest == digest(validation)
            and validation.request_revision == intent.request_revision
            and validation.intent_digest == digest(intent)
        ):
            raise AnswerValidationConflict("stored validation authority does not match its key")
        return result
