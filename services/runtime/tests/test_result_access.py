from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from heinzel_provider_sdk import (
    AccessEffectCommand,
    AccessEffectProviderError,
    run_access_provider_conformance,
)
from heinzel_runtime import (
    AnswerResultAccessAuthorityUnavailable,
    AnswerResultAccessEffectProvider,
    AnswerResultAccessTarget,
)

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def _command(**updates: object) -> AccessEffectCommand:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "grant_id": "grant-1",
        "grant_revision": 1,
        "surface": "result",
        "action": "apply",
        "idempotency_key": "effect-apply-1",
        "principal_ref": "principal:requester-a",
        "provider_resource_ref": "answer-result-1",
        "fields": ("region", "revenue"),
        "permissions": ("download", "view"),
        "effective_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "scope_digest": "a" * 64,
    }
    values.update(updates)
    return AccessEffectCommand.model_validate(values)


def _target(**updates: object) -> AnswerResultAccessTarget:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "grant_id": "grant-1",
        "grant_revision": 1,
        "principal_ref": "principal:requester-a",
        "result_ref": "answer-result-1",
        "fields": ("region", "revenue"),
        "permissions": ("download", "view"),
        "effective_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "scope_digest": "a" * 64,
    }
    values.update(updates)
    return AnswerResultAccessTarget.model_validate(values)


class _Authority:
    def __init__(self, target: AnswerResultAccessTarget | None = None) -> None:
        self.target = _target() if target is None else target

    def resolve(self, command: AccessEffectCommand) -> AnswerResultAccessTarget | None:
        return self.target


class _UnavailableAuthority(_Authority):
    def resolve(self, command: AccessEffectCommand) -> AnswerResultAccessTarget | None:
        raise AnswerResultAccessAuthorityUnavailable("private authority detail")


class _CommitFailureConnection(sqlite3.Connection):
    fail_commit = False

    def commit(self) -> None:
        if self.fail_commit:
            raise sqlite3.OperationalError("private storage detail")
        super().commit()


def _provider(
    *,
    authority: _Authority | None = None,
    connection: sqlite3.Connection | None = None,
) -> AnswerResultAccessEffectProvider:
    return AnswerResultAccessEffectProvider(
        connection or sqlite3.connect(":memory:"),
        targets=authority or _Authority(),
        clock=lambda: NOW + timedelta(minutes=1),
    )


def test_apply_and_revoke_change_only_the_exact_result_authority() -> None:
    provider = _provider()

    applied = provider.enact(_command())

    assert applied.surface == "result"
    assert provider.allows(
        tenant_id="tenant-a",
        principal_ref="principal:requester-a",
        result_ref="answer-result-1",
        permission="download",
    )
    assert not provider.allows(
        tenant_id="tenant-a",
        principal_ref="principal:requester-b",
        result_ref="answer-result-1",
        permission="download",
    )

    provider.enact(_command(action="revoke", idempotency_key="effect-revoke-1"))

    assert not provider.allows(
        tenant_id="tenant-a",
        principal_ref="principal:requester-a",
        result_ref="answer-result-1",
        permission="download",
    )


def test_later_grant_revision_revokes_the_original_applied_result_authority() -> None:
    authority = _Authority()
    provider = _provider(authority=authority)
    provider.enact(_command())
    authority.target = _target(grant_revision=2)

    provider.enact(
        _command(
            grant_revision=2,
            action="revoke",
            idempotency_key="effect-revoke-revision-2",
        )
    )

    assert not provider.allows(
        tenant_id="tenant-a",
        principal_ref="principal:requester-a",
        result_ref="answer-result-1",
        permission="view",
    )


@pytest.mark.parametrize(
    ("target_update", "command_update"),
    (
        ({"tenant_id": "tenant-b"}, {}),
        ({"grant_id": "grant-2"}, {}),
        ({"grant_revision": 2}, {}),
        ({"principal_ref": "principal:other"}, {}),
        ({"result_ref": "answer-result-other"}, {}),
        ({"fields": ("region",)}, {}),
        ({"permissions": ("view",)}, {}),
        ({"effective_at": NOW + timedelta(seconds=1)}, {}),
        ({"expires_at": NOW + timedelta(hours=2)}, {}),
        ({"scope_digest": "b" * 64}, {}),
        ({}, {"surface": "warehouse"}),
    ),
)
def test_authority_mismatch_is_permanent_without_recording_access(
    target_update: dict[str, object], command_update: dict[str, object]
) -> None:
    provider = _provider(authority=_Authority(_target(**target_update)))

    with pytest.raises(AccessEffectProviderError) as captured:
        provider.enact(_command(**command_update))

    assert captured.value.outcome == "permanent_failure"
    assert not provider.allows(
        tenant_id="tenant-a",
        principal_ref="principal:requester-a",
        result_ref="answer-result-1",
        permission="view",
    )


def test_unavailable_authority_is_transient_and_sanitized() -> None:
    provider = _provider(authority=_UnavailableAuthority())

    with pytest.raises(AccessEffectProviderError) as captured:
        provider.enact(_command())

    assert captured.value.outcome == "transient_failure"
    assert "private authority detail" not in str(captured.value)


def test_commit_failure_after_effect_is_ambiguous_and_sanitized() -> None:
    connection = sqlite3.connect(":memory:", factory=_CommitFailureConnection)
    provider = _provider(connection=connection)
    connection.fail_commit = True

    with pytest.raises(AccessEffectProviderError) as captured:
        provider.enact(_command())

    assert captured.value.outcome == "ambiguous_outcome"
    assert "private storage detail" not in str(captured.value)


def test_result_access_provider_conforms_to_exact_replay_contract() -> None:
    run_access_provider_conformance(_provider, lambda **updates: _command(**updates))
