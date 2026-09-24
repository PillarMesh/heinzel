from __future__ import annotations

from datetime import UTC, datetime

import pytest
from heinzel_state import (
    PreparedAcquisitionState,
    PreparedAcquisitionStateStatus,
    SourceCheckpointState,
)
from pydantic import ValidationError

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def checkpoint() -> SourceCheckpointState:
    return SourceCheckpointState(
        tenant_id="tenant-a",
        contract_digest="1" * 64,
        source_binding_ref="source-binding-a",
        provider_kind="postgresql",
        cursor_version="postgresql-compound-v1",
        revision=0,
        encrypted_cursor_payload=b"ciphertext-canary",
        cursor_digest="2" * 64,
        last_batch_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def prepared() -> PreparedAcquisitionState:
    return PreparedAcquisitionState(
        tenant_id="tenant-a",
        intent_key="3" * 64,
        contract_digest="1" * 64,
        source_binding_ref="source-binding-a",
        source_binding_revision=3,
        credential_revision=1,
        binding_authority_epoch=0,
        contract_authority_epoch=0,
        acknowledgement_consumer_ref="warehouse-consumer-a",
        provider_kind="postgresql",
        prior_checkpoint_revision=0,
        batch_id="4" * 64,
        batch_manifest_digest="5" * 64,
        candidate_cursor_ciphertext=b"candidate-ciphertext-canary",
        candidate_checkpoint_digest="6" * 64,
        cursor_version="postgresql-compound-v1",
        state=PreparedAcquisitionStateStatus.PREPARED,
        acknowledgement_digest=None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_checkpoint_accepts_initial_revision_zero_and_hides_ciphertext_from_repr() -> None:
    state = checkpoint()

    assert state.revision == 0
    assert "ciphertext-canary" not in repr(state)


def test_private_state_models_are_frozen_and_reject_unknown_fields() -> None:
    state = prepared()

    with pytest.raises(ValidationError, match="frozen"):
        state.state = PreparedAcquisitionStateStatus.ACKNOWLEDGED
    with pytest.raises(ValidationError, match="extra"):
        PreparedAcquisitionState.model_validate({**state.model_dump(), "unexpected": True})


@pytest.mark.parametrize(
    ("state", "acknowledgement_digest"),
    (
        (PreparedAcquisitionStateStatus.PREPARED, "7" * 64),
        (PreparedAcquisitionStateStatus.ACKNOWLEDGED, None),
    ),
)
def test_prepared_state_requires_acknowledgement_authority_only_after_commit(
    state: PreparedAcquisitionStateStatus,
    acknowledgement_digest: str | None,
) -> None:
    with pytest.raises(ValidationError, match="acknowledgement_digest"):
        PreparedAcquisitionState.model_validate(
            {
                **prepared().model_dump(),
                "state": state,
                "acknowledgement_digest": acknowledgement_digest,
            }
        )


def test_private_state_requires_timezone_aware_utc_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        SourceCheckpointState.model_validate(
            {**checkpoint().model_dump(), "updated_at": NOW.replace(tzinfo=None)}
        )
