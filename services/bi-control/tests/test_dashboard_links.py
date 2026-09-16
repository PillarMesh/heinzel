from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pillarmesh_bi_control import (
    DashboardAccessAuthorityError,
    DashboardAccessAuthorization,
    DashboardControlService,
    DashboardDesiredState,
    DashboardLinkDenied,
    DashboardLinkIssueCommand,
    DashboardLinkService,
    DashboardProductGenerationReference,
    SQLiteDashboardRepository,
)
from pillarmesh_contract_model import ArtifactReference, digest
from pillarmesh_provider_sdk.bi import BiApplyResult, BiDashboardDefinition
from pillarmesh_request_management import DashboardAnswerAuthority

NOW = datetime(2026, 9, 14, 18, tzinfo=UTC)
PRODUCT_REF = ArtifactReference(artifact_id="product:orders", version=2, digest="a" * 64)
PURPOSE = "Quarterly revenue planning"
PURPOSE_DIGEST = digest(PURPOSE)


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class _Provider:
    provider_kind: Literal["superset"] = "superset"

    def apply(self, definition: BiDashboardDefinition) -> BiApplyResult:
        return BiApplyResult(
            stable_external_key=definition.stable_external_key,
            desired_digest=definition.desired_digest,
            lifecycle_state=definition.lifecycle_state,
            external_url="https://superset.private.example/dashboard/provider-dashboard-42",
            provider_version="4.1.1",
        )


class _AccessAuthority:
    def __init__(self, authorization: DashboardAccessAuthorization) -> None:
        self.current: DashboardAccessAuthorization | None = authorization
        self.calls = 0
        self.on_authorize: Callable[[], None] | None = None
        self.fail = False

    def authorize(
        self,
        *,
        tenant_id: str,
        authorization_id: str,
        principal_ref: str,
        purpose: str,
        dashboard_id: str,
        dashboard_version: int,
        data_product_version_ref: ArtifactReference,
    ) -> DashboardAccessAuthorization | None:
        self.calls += 1
        if self.fail:
            raise DashboardAccessAuthorityError("access-control unavailable")
        if self.on_authorize is not None:
            self.on_authorize()
        if self.current is None:
            return None
        if (
            self.current.tenant_id != tenant_id
            or self.current.authorization_id != authorization_id
            or self.current.principal_ref != principal_ref
            or self.current.purpose_digest != digest(purpose)
            or self.current.dashboard_id != dashboard_id
            or self.current.dashboard_version != dashboard_version
            or self.current.data_product_version_ref != data_product_version_ref
        ):
            return None
        return self.current


def _desired(
    *,
    lifecycle_state: str = "active",
    revision: int = 1,
    prior_desired_digest: str | None = None,
) -> DashboardDesiredState:
    metric_ref = ArtifactReference(artifact_id="metric:revenue", version=2, digest="d" * 64)
    return DashboardDesiredState.model_validate(
        {
            "tenant_id": "tenant-a",
            "dashboard_id": "revenue",
            "version": 3,
            "revision": revision,
            "prior_desired_digest": prior_desired_digest,
            "title": "Revenue overview",
            "contract_digest": "e" * 64,
            "contract_key_id": "dashboard-key-1",
            "contract_signature": "test-signature",
            "source_answer": DashboardAnswerAuthority(
                tenant_id="tenant-a",
                request_id="request-1",
                request_revision=6,
                answer_id="answer-1",
                title="Revenue overview",
                execution_receipt_ref="execution-1",
                result_ref="result-1",
                result_digest="1" * 64,
                product_generation_refs=(
                    DashboardProductGenerationReference(
                        product_ref=PRODUCT_REF,
                        generation=7,
                    ),
                ),
                metric_version_refs=(metric_ref,),
                as_of=NOW,
                freshness_disposition="current",
                delivered_at=NOW,
            ),
            "dataset_product_ref": PRODUCT_REF,
            "dataset_generation": 7,
            "consumption_object_ref": ArtifactReference(
                artifact_id="consumption:orders", version=7, digest="b" * 64
            ),
            "materialization_receipt_ref": ArtifactReference(
                artifact_id="materialization:orders", version=7, digest="c" * 64
            ),
            "product_publication_ref": ArtifactReference(
                artifact_id="publication:orders", version=7, digest="6" * 64
            ),
            "dataset_namespace": "analytics",
            "dataset_relation_name": "revenue_current",
            "warehouse_binding_id": "warehouse-1",
            "warehouse_binding_revision": 2,
            "warehouse_binding_digest": "2" * 64,
            "connection_secret_ref": "secret://tenant-a/superset-database",
            "metric_refs": (metric_ref,),
            "dimension_refs": (),
            "filter_refs": (),
            "visual_intents": ("bar",),
            "lifecycle_state": lifecycle_state,
        }
    )


def _authorization(**updates: object) -> DashboardAccessAuthorization:
    values: dict[str, object] = {
        "authorization_id": "grant-42",
        "revision": 4,
        "state": "active",
        "tenant_id": "tenant-a",
        "access_request_id": "access-request-1",
        "dashboard_id": "revenue",
        "dashboard_version": 3,
        "data_product_version_ref": PRODUCT_REF,
        "principal_ref": "principal:analyst@example.test",
        "purpose_digest": PURPOSE_DIGEST,
        "effective_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=1),
        "verified_at": NOW,
    }
    values.update(updates)
    return DashboardAccessAuthorization.model_validate(values)


def _command(**updates: object) -> DashboardLinkIssueCommand:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "dashboard_id": "revenue",
        "dashboard_version": 3,
        "principal_ref": "principal:analyst@example.test",
        "purpose": PURPOSE,
        "purpose_digest": PURPOSE_DIGEST,
        "session_digest": "1" * 64,
        "idempotency_key": "open-revenue-dashboard",
    }
    values.update(updates)
    return DashboardLinkIssueCommand.model_validate(values)


def _service(
    repository: SQLiteDashboardRepository,
    authority: _AccessAuthority,
    clock: _Clock,
) -> DashboardLinkService:
    return DashboardLinkService(
        repository=repository,
        access_authority=authority,
        clock=clock,
        reference_factory=lambda: "Z" * 43,
        lifetime=timedelta(minutes=5),
    )


def _published_repository() -> SQLiteDashboardRepository:
    repository = SQLiteDashboardRepository(":memory:")
    DashboardControlService(repository, _Provider(), clock=lambda: NOW).apply(_desired())
    return repository


def test_issue_and_lookup_bind_an_opaque_link_to_current_authorities() -> None:
    repository = _published_repository()
    authorization = _authorization()
    authority = _AccessAuthority(authorization)
    clock = _Clock()
    service = _service(repository, authority, clock)

    link = service.issue(_command(), authorization)

    assert link.model_dump(mode="json") == {
        "schema_version": "1",
        "reference": f"dashboard-link:{'Z' * 43}",
        "expires_at": "2026-09-14T18:05:00Z",
    }
    public_payload = link.model_dump_json()
    assert all(
        value not in public_payload
        for value in ("superset", "provider-dashboard-42", "revenue", "grant-42", "request-1")
    )

    target = service.resolve(
        tenant_id="tenant-a",
        reference=link.reference,
        session_digest="1" * 64,
    )

    assert target.external_url == (
        "https://superset.private.example/dashboard/provider-dashboard-42"
    )
    assert authority.calls == 2


def test_exact_issue_replay_returns_the_persisted_link() -> None:
    repository = _published_repository()
    authorization = _authorization()
    authority = _AccessAuthority(authorization)
    clock = _Clock()
    generated = iter(("A" * 43, "B" * 43))
    service = DashboardLinkService(
        repository=repository,
        access_authority=authority,
        clock=clock,
        reference_factory=lambda: next(generated),
        lifetime=timedelta(minutes=5),
    )

    first = service.issue(_command(), authorization)
    replay = service.issue(_command(), authorization)

    assert replay == first


def test_recheck_observation_time_does_not_make_unchanged_authority_stale() -> None:
    repository = _published_repository()
    supplied = _authorization(verified_at=NOW - timedelta(minutes=1))
    authority = _AccessAuthority(_authorization(verified_at=NOW))

    link = _service(repository, authority, _Clock()).issue(_command(), supplied)

    assert link.reference.startswith("dashboard-link:")


def test_link_session_survives_repository_reopen(tmp_path: Path) -> None:
    database_path = str(tmp_path / "bi-control.db")
    repository = SQLiteDashboardRepository(database_path)
    DashboardControlService(repository, _Provider(), clock=lambda: NOW).apply(_desired())
    authorization = _authorization()
    authority = _AccessAuthority(authorization)
    clock = _Clock()
    link = _service(repository, authority, clock).issue(_command(), authorization)
    repository.close()

    reopened = SQLiteDashboardRepository(database_path)
    target = _service(reopened, authority, clock).resolve(
        tenant_id="tenant-a",
        reference=link.reference,
        session_digest="1" * 64,
    )

    assert target.external_url.endswith("/provider-dashboard-42")


@pytest.mark.parametrize(
    ("authorization_update", "command_update"),
    [
        ({"state": "revoked"}, {}),
        ({"expires_at": NOW}, {}),
        ({"effective_at": NOW + timedelta(minutes=1)}, {}),
        ({"tenant_id": "tenant-b"}, {}),
        ({"dashboard_id": "margin"}, {}),
        ({"dashboard_version": 4}, {}),
        (
            {
                "data_product_version_ref": ArtifactReference(
                    artifact_id="product:other", version=1, digest="9" * 64
                )
            },
            {},
        ),
        ({"principal_ref": "principal:other@example.test"}, {}),
        ({"purpose_digest": "8" * 64}, {}),
        ({}, {"tenant_id": "tenant-b"}),
        ({}, {"principal_ref": "principal:other@example.test"}),
        (
            {},
            {"purpose": "Another purpose", "purpose_digest": digest("Another purpose")},
        ),
    ],
)
def test_issue_denies_inactive_expired_future_or_mismatched_authority(
    authorization_update: dict[str, object], command_update: dict[str, object]
) -> None:
    repository = _published_repository()
    authorization = _authorization(**authorization_update)
    authority = _AccessAuthority(authorization)

    with pytest.raises(DashboardLinkDenied):
        _service(repository, authority, _Clock()).issue(_command(**command_update), authorization)


def test_issue_denies_an_archived_or_unreceipted_publication() -> None:
    authorization = _authorization()
    authority = _AccessAuthority(authorization)
    unpublished = SQLiteDashboardRepository(":memory:")
    unpublished.record_desired(_desired())

    with pytest.raises(DashboardLinkDenied):
        _service(unpublished, authority, _Clock()).issue(_command(), authorization)

    archived = SQLiteDashboardRepository(":memory:")
    DashboardControlService(archived, _Provider(), clock=lambda: NOW).apply(
        _desired(lifecycle_state="archived")
    )
    with pytest.raises(DashboardLinkDenied):
        _service(archived, authority, _Clock()).issue(_command(), authorization)


def test_issue_denies_an_unavailable_access_authority() -> None:
    repository = _published_repository()
    authorization = _authorization()
    authority = _AccessAuthority(authorization)
    authority.fail = True

    with pytest.raises(DashboardLinkDenied):
        _service(repository, authority, _Clock()).issue(_command(), authorization)


def test_lookup_denies_wrong_session_expiry_and_changed_access_authority() -> None:
    repository = _published_repository()
    authorization = _authorization()
    authority = _AccessAuthority(authorization)
    clock = _Clock()
    service = _service(repository, authority, clock)
    link = service.issue(_command(), authorization)

    with pytest.raises(DashboardLinkDenied):
        service.resolve(
            tenant_id="tenant-a",
            reference=link.reference,
            session_digest="2" * 64,
        )

    authority.current = authorization.model_copy(update={"revision": 5})
    with pytest.raises(DashboardLinkDenied):
        service.resolve(
            tenant_id="tenant-a",
            reference=link.reference,
            session_digest="1" * 64,
        )

    authority.current = authorization.model_copy(update={"state": "revoked"})
    with pytest.raises(DashboardLinkDenied):
        service.resolve(
            tenant_id="tenant-a",
            reference=link.reference,
            session_digest="1" * 64,
        )

    authority.current = authorization
    clock.now = NOW + timedelta(minutes=5)
    with pytest.raises(DashboardLinkDenied):
        service.resolve(
            tenant_id="tenant-a",
            reference=link.reference,
            session_digest="1" * 64,
        )


def test_lookup_denies_a_publication_that_is_no_longer_current() -> None:
    repository = _published_repository()
    authorization = _authorization()
    authority = _AccessAuthority(authorization)
    service = _service(repository, authority, _Clock())
    link = service.issue(_command(), authorization)
    current = repository.load_current_desired("tenant-a", "revenue", 3)
    assert current is not None
    DashboardControlService(repository, _Provider(), clock=lambda: NOW).apply(
        _desired(revision=2, prior_desired_digest=current.desired_digest)
    )

    with pytest.raises(DashboardLinkDenied):
        service.resolve(
            tenant_id="tenant-a",
            reference=link.reference,
            session_digest="1" * 64,
        )


def test_issue_denies_a_publication_that_changes_during_authorization() -> None:
    repository = _published_repository()
    authorization = _authorization()
    authority = _AccessAuthority(authorization)
    current = repository.load_current_desired("tenant-a", "revenue", 3)
    assert current is not None

    def advance_publication() -> None:
        authority.on_authorize = None
        DashboardControlService(repository, _Provider(), clock=lambda: NOW).apply(
            _desired(revision=2, prior_desired_digest=current.desired_digest)
        )

    authority.on_authorize = advance_publication

    with pytest.raises(DashboardLinkDenied):
        _service(repository, authority, _Clock()).issue(_command(), authorization)


def test_conflicting_idempotency_replay_is_denied() -> None:
    repository = _published_repository()
    authorization = _authorization()
    authority = _AccessAuthority(authorization)
    service = _service(repository, authority, _Clock())
    service.issue(_command(), authorization)

    with pytest.raises(DashboardLinkDenied):
        service.issue(_command(session_digest="2" * 64), authorization)
