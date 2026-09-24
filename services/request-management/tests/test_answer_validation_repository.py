from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from heinzel_contract_model import ArtifactReference, canonical_bytes, digest
from heinzel_request_management import (
    AnswerIntentValidation,
    AnswerQuestionIntent,
    AnswerQuestionValidationResult,
    BoundFilter,
)
from heinzel_request_management.answer_models import AnswerProductGenerationReference
from heinzel_request_management.answer_validation_repository import (
    AnswerValidationConflict,
    SQLiteAnswerValidationRepository,
)

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def _reference(identifier: str, character: str) -> ArtifactReference:
    return ArtifactReference(artifact_id=identifier, version=1, digest=character * 64)


def _result() -> AnswerQuestionValidationResult:
    intent = AnswerQuestionIntent(
        intent_id="intent-1",
        tenant_id="tenant-a",
        request_id="request-1",
        request_revision=3,
        question_digest="1" * 64,
        intent_kind="metric_value",
        metric_refs=("revenue",),
        dimension_refs=("region",),
        filters=(),
        time_window=None,
        ordering=(),
        row_limit=10,
        interpreter="form",
        interpreter_ref="answer-form-v1",
        created_at=NOW,
    )
    validation = AnswerIntentValidation(
        validation_id="validation-1",
        tenant_id="tenant-a",
        request_id="request-1",
        request_revision=3,
        intent_digest=digest(intent),
        semantic_version_digest="2" * 64,
        policy_id="policy-1",
        policy_revision=2,
        policy_digest="3" * 64,
        entitlement_snapshot_digest="4" * 64,
        bound_metric_versions=(_reference("metric:revenue", "5"),),
        bound_dimensions=(_reference("dimension:region", "6"),),
        bound_filters=(
            BoundFilter(
                dimension_ref=_reference("dimension:region", "6"),
                operator="equals",
                values=("east",),
            ),
        ),
        restatement="Revenue by region for east",
        product_generation_refs=(
            AnswerProductGenerationReference(
                product_ref=_reference("product:revenue", "7"), generation=4
            ),
        ),
        outcome="admitted",
        reason_codes=(),
        created_at=NOW,
    )
    return AnswerQuestionValidationResult(
        intent=intent,
        validation=validation,
        restatement_confirmation_required=False,
    )


def test_exact_replay_returns_the_same_durable_validation() -> None:
    repository = SQLiteAnswerValidationRepository(sqlite3.connect(":memory:"))
    result = _result()

    first = repository.save(result)
    replay = repository.save(result)

    assert replay == first
    assert (
        repository.read_validation(
            tenant_id="tenant-a",
            request_id="request-1",
            validation_digest=digest(result.validation),
        )
        == result.validation
    )


def test_cross_tenant_read_is_non_enumerating() -> None:
    repository = SQLiteAnswerValidationRepository(sqlite3.connect(":memory:"))
    result = repository.save(_result())

    assert result.validation.validation_id == "validation-1"
    assert (
        repository.read_validation(
            tenant_id="tenant-b",
            request_id="request-1",
            validation_digest=digest(result.validation),
        )
        is None
    )


def test_tampered_index_binding_is_rejected() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteAnswerValidationRepository(connection)
    result = repository.save(_result())
    connection.execute(
        "UPDATE answer_intent_validations SET validation_id = ? WHERE validation_digest = ?",
        ("validation-tampered", digest(result.validation)),
    )
    connection.commit()

    with pytest.raises(AnswerValidationConflict, match="stored validation authority"):
        repository.read_validation(
            tenant_id="tenant-a",
            request_id="request-1",
            validation_digest=digest(result.validation),
        )


def test_same_validation_identity_with_changed_payload_is_rejected() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteAnswerValidationRepository(connection)
    result = repository.save(_result())
    changed = result.model_copy(update={"restatement_confirmation_required": True})

    with pytest.raises(AnswerValidationConflict, match="identity is already bound"):
        repository.save(changed)

    row = connection.execute("SELECT payload FROM answer_intent_validations").fetchone()
    assert row is not None
    assert bytes(row[0]) == canonical_bytes(result)


def test_legacy_validation_payload_is_rejected_as_a_typed_authority_conflict() -> None:
    connection = sqlite3.connect(":memory:")
    repository = SQLiteAnswerValidationRepository(connection)
    payload = _result().model_dump(mode="python")
    validation = payload["validation"]
    assert isinstance(validation, dict)
    validation["schema_version"] = "1"
    validation["product_generation_refs"] = (
        _reference("product:revenue-generation", "7").model_dump(mode="python"),
    )
    validation_digest = digest(validation)
    connection.execute(
        "INSERT INTO answer_intent_validations "
        "(tenant_id, request_id, validation_id, validation_digest, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "tenant-a",
            "request-1",
            "validation-1",
            validation_digest,
            canonical_bytes(payload),
        ),
    )
    connection.commit()

    with pytest.raises(AnswerValidationConflict, match="stored validation authority"):
        repository.read_validation(
            tenant_id="tenant-a",
            request_id="request-1",
            validation_digest=validation_digest,
        )
