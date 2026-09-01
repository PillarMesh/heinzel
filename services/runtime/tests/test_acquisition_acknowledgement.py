from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from pillarmesh_contract_model import canonical_bytes, digest
from pillarmesh_provider_sdk import (
    AcquisitionAcknowledgement,
    AcquisitionBatchManifest,
    AcquisitionCheckpointReceipt,
    AcquisitionIntent,
    AcquisitionPreparedReceipt,
)
from pillarmesh_provider_sdk.errors import AcquisitionProviderKind
from pillarmesh_runtime import (
    AcquisitionAuthorizationError,
    AcquisitionIntegrityError,
    AcquisitionOwnershipError,
    AcquisitionPreparationResult,
    AcquisitionStaleRevision,
)
from pillarmesh_state import (
    AcquisitionStateConflictError,
    AcquisitionStatePersistenceError,
    PreparedAcquisitionState,
)

if TYPE_CHECKING:
    from services.runtime.tests.test_acquisition import (
        NOW,
        _binding,
        _contract,
        _intent,
        _record,
        _runner,
        _schema,
    )
else:
    try:
        from services.runtime.tests.test_acquisition import (
            NOW,
            _binding,
            _contract,
            _intent,
            _record,
            _runner,
            _schema,
        )
    except ModuleNotFoundError:
        # mutmut copies this suite below the service root, outside the repository namespace.
        from test_acquisition import NOW, _binding, _contract, _intent, _record, _runner, _schema


def _acknowledgement(
    intent: AcquisitionIntent,
    prepared_receipt: AcquisitionPreparedReceipt,
) -> AcquisitionAcknowledgement:
    return AcquisitionAcknowledgement(
        acknowledgement_id="acknowledgement:1",
        tenant_id=intent.tenant_id,
        consumer_ref="strict-consumer",
        contract_digest=intent.contract_digest,
        source_binding_ref=intent.source_binding_ref,
        batch_id=prepared_receipt.batch_id,
        batch_manifest_digest=prepared_receipt.batch_manifest_digest,
        prior_checkpoint_revision=intent.prior_checkpoint_revision,
        candidate_checkpoint_digest=prepared_receipt.candidate_checkpoint_digest,
        consumer_receipt_digest="9" * 64,
        acknowledged_at=NOW,
    )


def test_repeated_unacknowledged_intent_replays_exact_preparation_without_provider_call() -> None:
    runner, observation, _session, provider, _resolutions, _state, _artifacts, evidence, _events = (
        _runner(records=(_record("1"), _record("2")))
    )
    intent = _intent(observation)

    first = runner.prepare(intent)
    replay = runner.prepare(intent)

    assert replay.prepared_receipt == first.prepared_receipt
    assert replay.batch_manifest == first.batch_manifest
    assert provider.calls == 1
    assert [receipt.outcome for receipt in evidence.receipts] == ["prepared", "prepared"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("tenant_id", "tenant-b"),
        ("contract_ref", "contract:other:v1"),
        ("contract_digest", "d" * 64),
        ("source_binding_ref", "source-binding:other"),
        ("source_observation_digest", "e" * 64),
        ("acquisition_mode", "incremental"),
    ),
)
def test_preparation_replay_rejects_each_manifest_authority_difference(
    field: str,
    value: object,
) -> None:
    runner, observation, _session, _provider, _resolutions, state, artifacts, *_rest = _runner()
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    assert preparation.batch_manifest is not None
    changed_manifest = preparation.batch_manifest.model_copy(update={field: value})
    assert isinstance(changed_manifest, AcquisitionBatchManifest)
    manifest_payload = canonical_bytes(changed_manifest)
    manifest_digest = digest(changed_manifest)
    artifacts.payloads[manifest_digest] = manifest_payload
    key = (
        intent.tenant_id,
        intent.contract_digest,
        intent.source_binding_ref,
        intent.prior_checkpoint_revision,
    )
    prepared_state, prepared_receipt = state.prepared_states[key]
    state.prepared_states[key] = (
        prepared_state.model_copy(update={"batch_manifest_digest": manifest_digest}),
        prepared_receipt.model_copy(update={"batch_manifest_digest": manifest_digest}),
    )

    with pytest.raises(AcquisitionIntegrityError, match="prepared_replay_authority_mismatch"):
        runner.prepare(intent)


def test_concurrent_exact_preparation_returns_the_durable_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, observation, _session, provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)
    original_prepare = state.prepare_exact

    def persist_winner_then_report_conflict(
        receipt: AcquisitionPreparedReceipt,
        *,
        contract_digest: str,
        source_binding_ref: str,
        source_binding_revision: int,
        credential_revision: int,
        binding_authority_epoch: int,
        contract_authority_epoch: int,
        acknowledgement_consumer_ref: str,
        provider_kind: AcquisitionProviderKind,
        candidate_cursor_plaintext: bytes,
    ) -> PreparedAcquisitionState:
        winner = receipt.model_copy(update={"prepared_receipt_id": "prepared:winner"})
        original_prepare(
            winner,
            contract_digest=contract_digest,
            source_binding_ref=source_binding_ref,
            source_binding_revision=source_binding_revision,
            credential_revision=credential_revision,
            binding_authority_epoch=binding_authority_epoch,
            contract_authority_epoch=contract_authority_epoch,
            acknowledgement_consumer_ref=acknowledgement_consumer_ref,
            provider_kind=provider_kind,
            candidate_cursor_plaintext=candidate_cursor_plaintext,
        )
        raise AcquisitionStateConflictError("competing preparation")

    monkeypatch.setattr(state, "prepare_exact", persist_winner_then_report_conflict)

    result = runner.prepare(intent)

    assert result.prepared_receipt is not None
    assert result.prepared_receipt.prepared_receipt_id == "prepared:winner"
    assert provider.calls == 1


def test_concurrent_different_preparation_is_an_integrity_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, observation, _session, provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)
    original_prepare = state.prepare_exact

    def persist_competing_batch_then_report_conflict(
        receipt: AcquisitionPreparedReceipt,
        *,
        contract_digest: str,
        source_binding_ref: str,
        source_binding_revision: int,
        credential_revision: int,
        binding_authority_epoch: int,
        contract_authority_epoch: int,
        acknowledgement_consumer_ref: str,
        provider_kind: AcquisitionProviderKind,
        candidate_cursor_plaintext: bytes,
    ) -> PreparedAcquisitionState:
        original_prepare(
            receipt.model_copy(update={"batch_id": "f" * 64}),
            contract_digest=contract_digest,
            source_binding_ref=source_binding_ref,
            source_binding_revision=source_binding_revision,
            credential_revision=credential_revision,
            binding_authority_epoch=binding_authority_epoch,
            contract_authority_epoch=contract_authority_epoch,
            acknowledgement_consumer_ref=acknowledgement_consumer_ref,
            provider_kind=provider_kind,
            candidate_cursor_plaintext=candidate_cursor_plaintext,
        )
        raise AcquisitionStateConflictError("competing preparation")

    monkeypatch.setattr(state, "prepare_exact", persist_competing_batch_then_report_conflict)

    with pytest.raises(AcquisitionIntegrityError, match="prepared_replay_authority_mismatch"):
        runner.prepare(intent)

    assert provider.calls == 1


@pytest.mark.parametrize(
    "difference",
    (
        "missing_winner",
        "missing_receipt",
        "missing_manifest",
        "cursor_version",
        "batch_id",
        "candidate_checkpoint_digest",
        "segment_manifests",
        "total_record_count",
        "total_encoded_bytes",
    ),
)
def test_competing_preparation_must_match_each_durable_winner_field(
    monkeypatch: pytest.MonkeyPatch,
    difference: str,
) -> None:
    baseline_runner, baseline_observation, *_baseline_rest = _runner()
    baseline = baseline_runner.prepare(_intent(baseline_observation))
    assert baseline.prepared_receipt is not None
    assert baseline.batch_manifest is not None

    winner: AcquisitionPreparationResult | None = baseline
    if difference == "missing_winner":
        winner = None
    elif difference == "missing_receipt":
        winner = baseline.model_copy(update={"prepared_receipt": None})
    elif difference == "missing_manifest":
        winner = baseline.model_copy(update={"batch_manifest": None})
    elif difference == "cursor_version":
        winner = baseline.model_copy(
            update={
                "prepared_receipt": baseline.prepared_receipt.model_copy(
                    update={"cursor_version": "postgresql-compound-v2"}
                )
            }
        )
    elif difference == "batch_id":
        changed_batch_id = "f" * 64
        winner = baseline.model_copy(
            update={
                "prepared_receipt": baseline.prepared_receipt.model_copy(
                    update={"batch_id": changed_batch_id}
                ),
                "batch_manifest": baseline.batch_manifest.model_copy(
                    update={"batch_id": changed_batch_id}
                ),
            }
        )
    elif difference == "candidate_checkpoint_digest":
        changed_digest = "f" * 64
        winner = baseline.model_copy(
            update={
                "prepared_receipt": baseline.prepared_receipt.model_copy(
                    update={"candidate_checkpoint_digest": changed_digest}
                ),
                "batch_manifest": baseline.batch_manifest.model_copy(
                    update={"candidate_checkpoint_digest": changed_digest}
                ),
            }
        )
    elif difference == "segment_manifests":
        segment = baseline.batch_manifest.segment_manifests[0].model_copy(
            update={"record_count": baseline.batch_manifest.segment_manifests[0].record_count + 1}
        )
        winner = baseline.model_copy(
            update={
                "batch_manifest": baseline.batch_manifest.model_copy(
                    update={"segment_manifests": (segment,)}
                )
            }
        )
    elif difference == "total_record_count":
        winner = baseline.model_copy(
            update={
                "batch_manifest": baseline.batch_manifest.model_copy(
                    update={"total_record_count": baseline.batch_manifest.total_record_count + 1}
                )
            }
        )
    else:
        winner = baseline.model_copy(
            update={
                "batch_manifest": baseline.batch_manifest.model_copy(
                    update={"total_encoded_bytes": baseline.batch_manifest.total_encoded_bytes + 1}
                )
            }
        )

    runner, observation, _session, provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)

    def report_conflict(*args: object, **kwargs: object) -> PreparedAcquisitionState:
        del args, kwargs
        raise AcquisitionStateConflictError("competing preparation")

    replay_calls = 0

    def load_winner(candidate: AcquisitionIntent) -> AcquisitionPreparationResult | None:
        nonlocal replay_calls
        assert candidate == intent
        replay_calls += 1
        return None if replay_calls == 1 else winner

    monkeypatch.setattr(state, "prepare_exact", report_conflict)
    monkeypatch.setattr(runner, "_load_preparation_replay", load_winner)

    with pytest.raises(AcquisitionIntegrityError, match="prepared_state_write_failed"):
        runner.prepare(intent)

    assert provider.calls == 1


def test_exact_acknowledgement_advances_one_revision_and_emits_public_evidence() -> None:
    runner, observation, _session, provider, _resolutions, state, _artifacts, evidence, _events = (
        _runner()
    )
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None
    acknowledgement = _acknowledgement(intent, prepared_receipt)

    receipt = runner.acknowledge(
        intent,
        acknowledgement,
    )

    assert receipt.previous_revision == 0
    assert receipt.committed_revision == 1
    assert provider.calls == 1
    assert evidence.receipts[-1].outcome == "acknowledged"
    assert evidence.receipts[-1].prepared_receipt_ref == prepared_receipt.prepared_receipt_id
    assert evidence.receipts[-1].checkpoint_receipt_ref == receipt.checkpoint_receipt_id
    assert evidence.receipts[-1].resulting_checkpoint_revision == 1
    public_payload = evidence.receipts[-1].model_dump_json()
    assert prepared_receipt.batch_id not in public_payload
    assert prepared_receipt.batch_manifest_digest not in public_payload
    assert prepared_receipt.candidate_checkpoint_digest not in public_payload

    replay = runner.acknowledge(
        intent,
        acknowledgement,
    )

    assert replay == receipt
    assert len(state.acknowledgements) == 1
    assert evidence.receipts[-1].outcome == "acknowledged"


def test_contradictory_acknowledgement_replay_is_integrity_not_stale() -> None:
    runner, observation, _session, _provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None
    acknowledgement = _acknowledgement(intent, prepared_receipt)
    receipt = runner.acknowledge(intent, acknowledgement)

    with pytest.raises(AcquisitionIntegrityError, match="contradictory_acknowledgement"):
        runner.acknowledge(
            intent,
            acknowledgement.model_copy(update={"consumer_receipt_digest": "8" * 64}),
        )

    assert receipt.committed_revision == 1
    assert len(state.acknowledgements) == 1


def test_contradictory_acknowledgement_fails_before_state_mutation() -> None:
    runner, observation, _session, _provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None
    acknowledgement = AcquisitionAcknowledgement(
        acknowledgement_id="acknowledgement:wrong-batch",
        tenant_id=intent.tenant_id,
        consumer_ref="strict-consumer",
        contract_digest=intent.contract_digest,
        source_binding_ref=intent.source_binding_ref,
        batch_id="8" * 64,
        batch_manifest_digest=prepared_receipt.batch_manifest_digest,
        prior_checkpoint_revision=intent.prior_checkpoint_revision,
        candidate_checkpoint_digest=prepared_receipt.candidate_checkpoint_digest,
        consumer_receipt_digest="9" * 64,
        acknowledged_at=NOW,
    )

    with pytest.raises(AcquisitionIntegrityError, match="acknowledgement_authority"):
        runner.acknowledge(
            intent,
            acknowledgement,
        )

    assert state.acknowledgements == []


def test_cross_tenant_acknowledgement_fails_as_ownership_before_state_mutation() -> None:
    runner, observation, _session, _provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None
    acknowledgement = AcquisitionAcknowledgement(
        acknowledgement_id="acknowledgement:wrong-tenant",
        tenant_id="tenant-b",
        consumer_ref="strict-consumer",
        contract_digest=intent.contract_digest,
        source_binding_ref=intent.source_binding_ref,
        batch_id=prepared_receipt.batch_id,
        batch_manifest_digest=prepared_receipt.batch_manifest_digest,
        prior_checkpoint_revision=intent.prior_checkpoint_revision,
        candidate_checkpoint_digest=prepared_receipt.candidate_checkpoint_digest,
        consumer_receipt_digest="9" * 64,
        acknowledged_at=NOW,
    )

    with pytest.raises(AcquisitionOwnershipError, match="tenant"):
        runner.acknowledge(
            intent,
            acknowledgement,
        )

    assert state.acknowledgements == []


@pytest.mark.parametrize(
    ("artifact", "field", "value"),
    (
        ("state", "tenant_id", "tenant-b"),
        ("state", "intent_key", "f" * 64),
        ("state", "contract_digest", "d" * 64),
        ("state", "source_binding_ref", "source-binding:other"),
        ("state", "prior_checkpoint_revision", 1),
        ("receipt", "tenant_id", "tenant-b"),
        ("receipt", "intent_key", "f" * 64),
        ("receipt", "batch_id", "e" * 64),
        ("receipt", "batch_manifest_digest", "d" * 64),
        ("receipt", "prior_checkpoint_revision", 1),
        ("receipt", "candidate_checkpoint_digest", "a" * 64),
        ("receipt", "cursor_version", "postgresql-compound-v2"),
        ("receipt", "prepared_at", NOW + timedelta(seconds=1)),
    ),
)
def test_acknowledgement_revalidates_every_persisted_preparation_authority_field(
    artifact: str,
    field: str,
    value: object,
) -> None:
    runner, observation, _session, _provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None
    key = (
        intent.tenant_id,
        intent.contract_digest,
        intent.source_binding_ref,
        intent.prior_checkpoint_revision,
    )
    prepared_state, stored_receipt = state.prepared_states[key]
    if artifact == "state":
        prepared_state = prepared_state.model_copy(update={field: value})
    else:
        stored_receipt = stored_receipt.model_copy(update={field: value})
    state.prepared_states[key] = (prepared_state, stored_receipt)

    with pytest.raises(AcquisitionIntegrityError, match="prepared_acquisition_authority"):
        runner.acknowledge(
            intent,
            _acknowledgement(intent, prepared_receipt),
        )

    assert state.acknowledgements == []


@pytest.mark.parametrize(
    "changed_authority",
    ("acknowledgement_consumer_ref", "source_binding_revision", "credential_revision"),
)
def test_pending_acknowledgement_rejects_each_changed_live_authority(
    changed_authority: str,
) -> None:
    runner, observation, _session, _provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None
    contract_changes: dict[str, object] = {}
    binding_changes: dict[str, object] = {}
    if changed_authority == "acknowledgement_consumer_ref":
        contract_changes[changed_authority] = "replacement-consumer"
    elif changed_authority == "source_binding_revision":
        contract_changes[changed_authority] = 4
        binding_changes["revision"] = 4
    else:
        contract_changes[changed_authority] = 2
        binding_changes[changed_authority] = 2
    changed_contract = _contract(_schema(), observation, **contract_changes)
    changed_binding = _binding().model_copy(update=binding_changes)
    runner._contract_resolver = lambda tenant_id, contract_ref: changed_contract
    runner._binding_resolver = lambda tenant_id, binding_ref: changed_binding

    with pytest.raises(AcquisitionStaleRevision, match="acknowledgement_authority_changed"):
        runner.acknowledge(intent, _acknowledgement(intent, prepared_receipt))

    assert state.acknowledgements == []


@pytest.mark.parametrize(
    ("field", "value"),
    (("tenant_id", "tenant-b"), ("intent_key", "f" * 64)),
)
def test_acknowledgement_rejects_correlated_preparation_authority_corruption(
    field: str,
    value: object,
) -> None:
    runner, observation, _session, _provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None
    key = (
        intent.tenant_id,
        intent.contract_digest,
        intent.source_binding_ref,
        intent.prior_checkpoint_revision,
    )
    prepared_state, stored_receipt = state.prepared_states[key]
    state.prepared_states[key] = (
        prepared_state.model_copy(update={field: value}),
        stored_receipt.model_copy(update={field: value}),
    )

    with pytest.raises(AcquisitionIntegrityError, match="prepared_acquisition_authority"):
        runner.acknowledge(
            intent,
            _acknowledgement(intent, prepared_receipt),
        )

    assert state.acknowledgements == []


def test_acknowledgement_requires_the_exact_authorized_consumer() -> None:
    runner, observation, _session, _provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None
    acknowledgement = _acknowledgement(intent, prepared_receipt).model_copy(
        update={"consumer_ref": "different-consumer"}
    )

    with pytest.raises(AcquisitionAuthorizationError, match="consumer_not_authorized"):
        runner.acknowledge(
            intent,
            acknowledgement,
        )

    assert state.acknowledgements == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("contract_digest", "d" * 64),
        ("source_binding_ref", "source-binding:other"),
        ("batch_id", "e" * 64),
        ("batch_manifest_digest", "d" * 64),
        ("prior_checkpoint_revision", 1),
        ("candidate_checkpoint_digest", "a" * 64),
    ),
)
def test_acknowledgement_rejects_each_contradictory_batch_authority_field(
    field: str,
    value: object,
) -> None:
    runner, observation, _session, _provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None
    acknowledgement = _acknowledgement(intent, prepared_receipt).model_copy(update={field: value})

    with pytest.raises(AcquisitionIntegrityError, match="acknowledgement_authority"):
        runner.acknowledge(
            intent,
            acknowledgement,
        )

    assert state.acknowledgements == []


@pytest.mark.parametrize(
    "updates",
    (
        {"tenant_id": "tenant-b"},
        {"contract_digest": "d" * 64},
        {"source_binding_ref": "source-binding:other"},
        {"previous_revision": 1, "committed_revision": 2},
        {"cursor_digest": "a" * 64},
        {"batch_id": "e" * 64},
        {"acknowledgement_id": "acknowledgement:other"},
        {"committed_at": NOW + timedelta(seconds=1)},
    ),
)
def test_acknowledgement_rejects_each_untrusted_checkpoint_receipt_authority_field(
    updates: dict[str, object],
) -> None:
    runner, observation, _session, _provider, _resolutions, state, *_rest = _runner()
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None
    acknowledgement = _acknowledgement(intent, prepared_receipt)
    checkpoint_receipt = AcquisitionCheckpointReceipt(
        checkpoint_receipt_id="checkpoint-receipt:untrusted",
        tenant_id=acknowledgement.tenant_id,
        contract_digest=acknowledgement.contract_digest,
        source_binding_ref=acknowledgement.source_binding_ref,
        previous_revision=acknowledgement.prior_checkpoint_revision,
        committed_revision=acknowledgement.prior_checkpoint_revision + 1,
        cursor_digest=acknowledgement.candidate_checkpoint_digest,
        batch_id=acknowledgement.batch_id,
        acknowledgement_id=acknowledgement.acknowledgement_id,
        committed_at=acknowledgement.acknowledged_at,
    ).model_copy(update=updates)
    state.checkpoint_receipt = checkpoint_receipt

    with pytest.raises(AcquisitionIntegrityError, match="checkpoint_receipt_authority"):
        runner.acknowledge(
            intent,
            acknowledgement,
        )


def test_missing_preparation_maps_to_stale_failure_evidence() -> None:
    runner, observation, _session, _provider, _resolutions, state, _artifacts, evidence, _events = (
        _runner()
    )
    intent = _intent(observation)
    acknowledgement = AcquisitionAcknowledgement(
        acknowledgement_id="acknowledgement:missing",
        tenant_id=intent.tenant_id,
        consumer_ref="strict-consumer",
        contract_digest=intent.contract_digest,
        source_binding_ref=intent.source_binding_ref,
        batch_id="2" * 64,
        batch_manifest_digest="3" * 64,
        prior_checkpoint_revision=intent.prior_checkpoint_revision,
        candidate_checkpoint_digest="4" * 64,
        consumer_receipt_digest="9" * 64,
        acknowledged_at=NOW,
    )

    with pytest.raises(AcquisitionStaleRevision, match="prepared_acquisition_not_found"):
        runner.acknowledge(
            intent,
            acknowledgement,
        )

    assert state.acknowledgements == []
    assert evidence.receipts[-1].outcome == "failed"
    assert evidence.receipts[-1].reason_codes == ("stale_checkpoint",)


@pytest.mark.parametrize(
    ("state_error", "expected_error", "reason_code"),
    (
        (
            AcquisitionStateConflictError("stale acknowledgement"),
            AcquisitionIntegrityError,
            "integrity_failure",
        ),
        (
            AcquisitionStatePersistenceError(operation="acknowledge"),
            AcquisitionIntegrityError,
            "integrity_failure",
        ),
    ),
)
def test_state_acknowledgement_failures_are_sanitized_and_classified(
    monkeypatch: pytest.MonkeyPatch,
    state_error: Exception,
    expected_error: type[Exception],
    reason_code: str,
) -> None:
    runner, observation, _session, _provider, _resolutions, state, _artifacts, evidence, _events = (
        _runner()
    )
    intent = _intent(observation)
    preparation = runner.prepare(intent)
    prepared_receipt = preparation.prepared_receipt
    assert prepared_receipt is not None

    def fail_acknowledgement(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise state_error

    monkeypatch.setattr(state, "acknowledge_exact", fail_acknowledgement)

    with pytest.raises(expected_error):
        runner.acknowledge(
            intent,
            _acknowledgement(intent, prepared_receipt),
        )

    assert evidence.receipts[-1].outcome == "failed"
    assert evidence.receipts[-1].reason_codes == (reason_code,)
