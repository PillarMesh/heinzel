from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol

from heinzel_contract_model import ArtifactReference, canonical_bytes, digest
from heinzel_provider_sdk import ProviderError
from heinzel_provider_sdk.bi import (
    BiDashboardDefinition,
    BiDimensionProjection,
    BiMetricProjection,
    BiProvider,
)

from .models import (
    DashboardAccessAuthorization,
    DashboardDesiredState,
    DashboardLink,
    DashboardLinkIssueCommand,
    DashboardLinkSession,
    DashboardPrivateTarget,
    DashboardProviderReceipt,
    DashboardPublication,
)


class DashboardLinkDenied(PermissionError):
    pass


class DashboardAccessAuthorityError(RuntimeError):
    pass


class DashboardAccessAuthority(Protocol):
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
    ) -> DashboardAccessAuthorization | None: ...


class SQLiteDashboardRepository:
    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = Lock()
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_desired_states ("
            "tenant_id TEXT NOT NULL, dashboard_id TEXT NOT NULL, version INTEGER NOT NULL, "
            "revision INTEGER NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, dashboard_id, version, revision))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_link_sessions ("
            "tenant_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, "
            "reference TEXT NOT NULL UNIQUE, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, idempotency_key))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_current_desired ("
            "tenant_id TEXT NOT NULL, dashboard_id TEXT NOT NULL, version INTEGER NOT NULL, "
            "revision INTEGER NOT NULL, "
            "PRIMARY KEY (tenant_id, dashboard_id, version))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_provider_receipts ("
            "tenant_id TEXT NOT NULL, dashboard_id TEXT NOT NULL, version INTEGER NOT NULL, "
            "revision INTEGER NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, dashboard_id, version, revision))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def record_desired(self, desired: DashboardDesiredState) -> DashboardDesiredState:
        payload = canonical_bytes(desired)
        key = (desired.tenant_id, desired.dashboard_id, desired.version, desired.revision)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT payload FROM dashboard_desired_states WHERE tenant_id = ? "
                    "AND dashboard_id = ? AND version = ? AND revision = ?",
                    key,
                ).fetchone()
                if existing is not None:
                    if bytes(existing[0]) != payload:
                        raise ValueError("conflicting dashboard desired state replay")
                    self._connection.commit()
                    return DashboardDesiredState.model_validate_json(existing[0])
                current = self._connection.execute(
                    "SELECT revision, payload FROM dashboard_desired_states "
                    "WHERE tenant_id = ? AND dashboard_id = ? AND version = ? "
                    "ORDER BY revision DESC LIMIT 1",
                    key[:3],
                ).fetchone()
                if current is None:
                    if desired.revision != 1:
                        raise ValueError("dashboard desired revision is not contiguous")
                else:
                    previous = DashboardDesiredState.model_validate_json(current[1])
                    if (
                        desired.revision != int(current[0]) + 1
                        or desired.prior_desired_digest != previous.desired_digest
                    ):
                        raise ValueError("dashboard desired revision is stale")
                self._connection.execute(
                    "INSERT INTO dashboard_desired_states VALUES (?, ?, ?, ?, ?)",
                    (*key, payload),
                )
                self._connection.execute(
                    "INSERT INTO dashboard_current_desired VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(tenant_id, dashboard_id, version) DO UPDATE "
                    "SET revision = excluded.revision",
                    key,
                )
                self._connection.commit()
                return desired
            except BaseException:
                self._connection.rollback()
                raise

    def record_receipt(self, receipt: DashboardProviderReceipt) -> DashboardProviderReceipt:
        payload = canonical_bytes(receipt)
        key = (receipt.tenant_id, receipt.dashboard_id, receipt.version, receipt.revision)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                desired = self._connection.execute(
                    "SELECT payload FROM dashboard_desired_states WHERE tenant_id = ? "
                    "AND dashboard_id = ? AND version = ? AND revision = ?",
                    key,
                ).fetchone()
                if desired is None:
                    raise ValueError("dashboard receipt has no desired state")
                desired_state = DashboardDesiredState.model_validate_json(desired[0])
                if desired_state.desired_digest != receipt.desired_digest:
                    raise ValueError("dashboard receipt does not match desired state")
                existing = self._connection.execute(
                    "SELECT payload FROM dashboard_provider_receipts WHERE tenant_id = ? "
                    "AND dashboard_id = ? AND version = ? AND revision = ?",
                    key,
                ).fetchone()
                if existing is not None:
                    persisted = DashboardProviderReceipt.model_validate_json(existing[0])
                    if persisted.model_dump(exclude={"applied_at"}) != receipt.model_dump(
                        exclude={"applied_at"}
                    ):
                        raise ValueError("conflicting dashboard receipt replay")
                    self._connection.commit()
                    return persisted
                self._connection.execute(
                    "INSERT INTO dashboard_provider_receipts VALUES (?, ?, ?, ?, ?)",
                    (*key, payload),
                )
                self._connection.commit()
                return receipt
            except BaseException:
                self._connection.rollback()
                raise

    def load_desired(
        self, tenant_id: str, dashboard_id: str, version: int, revision: int
    ) -> DashboardDesiredState:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM dashboard_desired_states WHERE tenant_id = ? "
                "AND dashboard_id = ? AND version = ? AND revision = ?",
                (tenant_id, dashboard_id, version, revision),
            ).fetchone()
        if row is None:
            raise KeyError((dashboard_id, version, revision))
        return DashboardDesiredState.model_validate_json(row[0])

    def load_receipt(
        self, tenant_id: str, dashboard_id: str, version: int, revision: int
    ) -> DashboardProviderReceipt | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM dashboard_provider_receipts WHERE tenant_id = ? "
                "AND dashboard_id = ? AND version = ? AND revision = ?",
                (tenant_id, dashboard_id, version, revision),
            ).fetchone()
        if row is None:
            return None
        return DashboardProviderReceipt.model_validate_json(row[0])

    def load_current_desired(
        self, tenant_id: str, dashboard_id: str, version: int
    ) -> DashboardDesiredState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT desired.payload FROM dashboard_current_desired AS current "
                "INNER JOIN dashboard_desired_states AS desired "
                "ON desired.tenant_id = current.tenant_id "
                "AND desired.dashboard_id = current.dashboard_id "
                "AND desired.version = current.version AND desired.revision = current.revision "
                "WHERE current.tenant_id = ? AND current.dashboard_id = ? AND current.version = ?",
                (tenant_id, dashboard_id, version),
            ).fetchone()
        if row is None:
            return None
        desired = DashboardDesiredState.model_validate_json(row[0])
        if (desired.tenant_id, desired.dashboard_id, desired.version) != (
            tenant_id,
            dashboard_id,
            version,
        ):
            raise ValueError("dashboard desired state index does not match its payload")
        return desired

    def load_current_receipt(
        self, tenant_id: str, dashboard_id: str, version: int
    ) -> DashboardProviderReceipt | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT receipt.payload FROM dashboard_current_desired AS current "
                "INNER JOIN dashboard_provider_receipts AS receipt "
                "ON receipt.tenant_id = current.tenant_id "
                "AND receipt.dashboard_id = current.dashboard_id "
                "AND receipt.version = current.version AND receipt.revision = current.revision "
                "WHERE current.tenant_id = ? AND current.dashboard_id = ? AND current.version = ?",
                (tenant_id, dashboard_id, version),
            ).fetchone()
        if row is None:
            return None
        return DashboardProviderReceipt.model_validate_json(row[0])

    def load_current_publication(
        self, tenant_id: str, dashboard_id: str, version: int
    ) -> DashboardPublication | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT desired.payload, receipt.payload "
                "FROM dashboard_current_desired AS current "
                "INNER JOIN dashboard_desired_states AS desired "
                "ON desired.tenant_id = current.tenant_id "
                "AND desired.dashboard_id = current.dashboard_id "
                "AND desired.version = current.version AND desired.revision = current.revision "
                "INNER JOIN dashboard_provider_receipts AS receipt "
                "ON receipt.tenant_id = current.tenant_id "
                "AND receipt.dashboard_id = current.dashboard_id "
                "AND receipt.version = current.version AND receipt.revision = current.revision "
                "WHERE current.tenant_id = ? AND current.dashboard_id = ? AND current.version = ?",
                (tenant_id, dashboard_id, version),
            ).fetchone()
        if row is None:
            return None
        desired = DashboardDesiredState.model_validate_json(row[0])
        receipt = DashboardProviderReceipt.model_validate_json(row[1])
        expected_identity = (tenant_id, dashboard_id, version)
        if (
            (desired.tenant_id, desired.dashboard_id, desired.version) != expected_identity
            or (receipt.tenant_id, receipt.dashboard_id, receipt.version) != expected_identity
            or desired.revision != receipt.revision
        ):
            raise ValueError("dashboard publication index does not match its payload")
        return DashboardPublication(
            dashboard_id=desired.dashboard_id,
            version=desired.version,
            title=desired.title,
            source_request_id=desired.source_answer.request_id,
            data_product_version_ref=desired.dataset_product_ref,
            lifecycle_state=receipt.lifecycle_state,
            as_of=desired.source_answer.as_of,
            freshness_disposition=desired.source_answer.freshness_disposition,
            published_at=receipt.applied_at,
        )

    def load_current_provider_authority(
        self, tenant_id: str, dashboard_id: str, version: int
    ) -> tuple[DashboardDesiredState, DashboardProviderReceipt] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT desired.payload, receipt.payload "
                "FROM dashboard_current_desired AS current "
                "INNER JOIN dashboard_desired_states AS desired "
                "ON desired.tenant_id = current.tenant_id "
                "AND desired.dashboard_id = current.dashboard_id "
                "AND desired.version = current.version AND desired.revision = current.revision "
                "INNER JOIN dashboard_provider_receipts AS receipt "
                "ON receipt.tenant_id = current.tenant_id "
                "AND receipt.dashboard_id = current.dashboard_id "
                "AND receipt.version = current.version AND receipt.revision = current.revision "
                "WHERE current.tenant_id = ? AND current.dashboard_id = ? AND current.version = ?",
                (tenant_id, dashboard_id, version),
            ).fetchone()
        if row is None:
            return None
        desired = DashboardDesiredState.model_validate_json(row[0], strict=True)
        receipt = DashboardProviderReceipt.model_validate_json(row[1], strict=True)
        expected = (tenant_id, dashboard_id, version, desired.revision)
        if (
            (desired.tenant_id, desired.dashboard_id, desired.version, desired.revision) != expected
            or (receipt.tenant_id, receipt.dashboard_id, receipt.version, receipt.revision)
            != expected
            or receipt.desired_digest != desired.desired_digest
        ):
            raise ValueError("dashboard provider authority index does not match its payload")
        return desired, receipt

    def record_link_session(self, session: DashboardLinkSession) -> DashboardLinkSession:
        payload = canonical_bytes(session)
        key = (session.tenant_id, session.idempotency_key)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                authority_row = self._connection.execute(
                    "SELECT desired.payload, receipt.payload "
                    "FROM dashboard_current_desired AS current "
                    "INNER JOIN dashboard_desired_states AS desired "
                    "ON desired.tenant_id = current.tenant_id "
                    "AND desired.dashboard_id = current.dashboard_id "
                    "AND desired.version = current.version "
                    "AND desired.revision = current.revision "
                    "INNER JOIN dashboard_provider_receipts AS receipt "
                    "ON receipt.tenant_id = current.tenant_id "
                    "AND receipt.dashboard_id = current.dashboard_id "
                    "AND receipt.version = current.version "
                    "AND receipt.revision = current.revision "
                    "WHERE current.tenant_id = ? AND current.dashboard_id = ? "
                    "AND current.version = ?",
                    (session.tenant_id, session.dashboard_id, session.dashboard_version),
                ).fetchone()
                if authority_row is None:
                    raise ValueError("dashboard link publication is unavailable")
                desired = DashboardDesiredState.model_validate_json(authority_row[0], strict=True)
                receipt = DashboardProviderReceipt.model_validate_json(
                    authority_row[1], strict=True
                )
                if (
                    desired.revision != session.dashboard_revision
                    or desired.desired_digest != session.desired_digest
                    or desired.lifecycle_state != "active"
                    or receipt.revision != session.dashboard_revision
                    or receipt.desired_digest != session.desired_digest
                    or receipt.lifecycle_state != "active"
                    or receipt.external_url != session.external_url
                ):
                    raise ValueError("dashboard link publication is stale")
                row = self._connection.execute(
                    "SELECT payload FROM dashboard_link_sessions "
                    "WHERE tenant_id = ? AND idempotency_key = ?",
                    key,
                ).fetchone()
                if row is not None:
                    existing = DashboardLinkSession.model_validate_json(row[0], strict=True)
                    if existing.request_digest != session.request_digest:
                        raise ValueError("conflicting dashboard link replay")
                    self._connection.commit()
                    return existing
                self._connection.execute(
                    "INSERT INTO dashboard_link_sessions VALUES (?, ?, ?, ?)",
                    (*key, session.reference, payload),
                )
                self._connection.commit()
                return session
            except BaseException:
                with suppress(sqlite3.Error):
                    self._connection.rollback()
                raise

    def load_link_session(self, tenant_id: str, reference: str) -> DashboardLinkSession | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM dashboard_link_sessions WHERE tenant_id = ? AND reference = ?",
                (tenant_id, reference),
            ).fetchone()
        if row is None:
            return None
        session = DashboardLinkSession.model_validate_json(row[0], strict=True)
        if session.tenant_id != tenant_id or session.reference != reference:
            raise ValueError("dashboard link index does not match its payload")
        return session

    def list_current_publications(self, tenant_id: str) -> tuple[DashboardPublication, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT current.dashboard_id, current.version, current.revision, "
                "desired.payload, receipt.payload "
                "FROM dashboard_current_desired AS current "
                "INNER JOIN dashboard_desired_states AS desired "
                "ON desired.tenant_id = current.tenant_id "
                "AND desired.dashboard_id = current.dashboard_id "
                "AND desired.version = current.version AND desired.revision = current.revision "
                "INNER JOIN dashboard_provider_receipts AS receipt "
                "ON receipt.tenant_id = current.tenant_id "
                "AND receipt.dashboard_id = current.dashboard_id "
                "AND receipt.version = current.version AND receipt.revision = current.revision "
                "WHERE current.tenant_id = ? "
                "ORDER BY current.dashboard_id, current.version",
                (tenant_id,),
            ).fetchall()
        publications: list[DashboardPublication] = []
        for dashboard_id, version, revision, desired_payload, receipt_payload in rows:
            desired = DashboardDesiredState.model_validate_json(desired_payload)
            receipt = DashboardProviderReceipt.model_validate_json(receipt_payload)
            expected_identity = (tenant_id, str(dashboard_id), int(version), int(revision))
            if (
                (
                    desired.tenant_id,
                    desired.dashboard_id,
                    desired.version,
                    desired.revision,
                )
                != expected_identity
                or (
                    receipt.tenant_id,
                    receipt.dashboard_id,
                    receipt.version,
                    receipt.revision,
                )
                != expected_identity
                or desired.desired_digest != receipt.desired_digest
            ):
                raise ValueError("dashboard publication index does not match its payload")
            publications.append(
                DashboardPublication(
                    dashboard_id=desired.dashboard_id,
                    version=desired.version,
                    title=desired.title,
                    source_request_id=desired.source_answer.request_id,
                    data_product_version_ref=desired.dataset_product_ref,
                    lifecycle_state=receipt.lifecycle_state,
                    as_of=desired.source_answer.as_of,
                    freshness_disposition=desired.source_answer.freshness_disposition,
                    published_at=receipt.applied_at,
                )
            )
        return tuple(publications)


class DashboardControlService:
    def __init__(
        self,
        repository: SQLiteDashboardRepository,
        provider: BiProvider,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._clock = clock

    def apply(self, desired: DashboardDesiredState) -> DashboardProviderReceipt:
        stored = self._repository.record_desired(desired)
        existing = self._repository.load_receipt(
            stored.tenant_id, stored.dashboard_id, stored.version, stored.revision
        )
        if existing is not None:
            return existing
        result = self._provider.apply(
            BiDashboardDefinition(
                tenant_id=stored.tenant_id,
                dashboard_id=stored.dashboard_id,
                version=stored.version,
                revision=stored.revision,
                stable_external_key=stored.stable_external_key,
                desired_digest=stored.desired_digest,
                prior_desired_digest=stored.prior_desired_digest,
                title=stored.title,
                contract_digest=stored.contract_digest,
                contract_signature=stored.contract_signature,
                dataset_stable_key=stored.dataset_stable_key,
                dataset_generation=stored.dataset_generation,
                dataset_namespace=stored.dataset_namespace,
                dataset_relation_name=stored.dataset_relation_name,
                connection_secret_ref=stored.connection_secret_ref,
                metric_refs=tuple(_provider_reference(item) for item in stored.metric_refs),
                dimension_refs=tuple(_provider_reference(item) for item in stored.dimension_refs),
                filter_refs=tuple(_provider_reference(item) for item in stored.filter_refs),
                metric_projections=tuple(
                    BiMetricProjection(
                        semantic_ref=_provider_reference(item.semantic_ref),
                        aggregate=item.aggregate,
                        column_name=item.column_name,
                        output_name=item.output_name,
                    )
                    for item in stored.metric_projections
                ),
                dimension_projections=tuple(
                    BiDimensionProjection(
                        semantic_ref=_provider_reference(item.semantic_ref),
                        column_name=item.column_name,
                        output_name=item.output_name,
                    )
                    for item in stored.dimension_projections
                ),
                visual_intents=stored.visual_intents,
                lifecycle_state=stored.lifecycle_state,
            )
        )
        if (
            result.stable_external_key != stored.stable_external_key
            or result.desired_digest != stored.desired_digest
            or result.lifecycle_state != stored.lifecycle_state
        ):
            raise ProviderError(
                "BI provider returned conflicting receipt", "invalid_provider_response"
            )
        receipt = DashboardProviderReceipt(
            tenant_id=stored.tenant_id,
            dashboard_id=stored.dashboard_id,
            version=stored.version,
            revision=stored.revision,
            stable_external_key=result.stable_external_key,
            desired_digest=result.desired_digest,
            lifecycle_state=result.lifecycle_state,
            external_url=result.external_url,
            provider_version=result.provider_version,
            applied_at=self._clock(),
        )
        return self._repository.record_receipt(receipt)

    def get_current_desired(
        self, tenant_id: str, dashboard_id: str, version: int
    ) -> DashboardDesiredState | None:
        return self._repository.load_current_desired(tenant_id, dashboard_id, version)

    def get_publication(
        self, tenant_id: str, dashboard_id: str, version: int
    ) -> DashboardPublication | None:
        return self._repository.load_current_publication(tenant_id, dashboard_id, version)

    def list_publications(self, tenant_id: str) -> tuple[DashboardPublication, ...]:
        return self._repository.list_current_publications(tenant_id)


class DashboardLinkService:
    def __init__(
        self,
        *,
        repository: SQLiteDashboardRepository,
        access_authority: DashboardAccessAuthority,
        clock: Callable[[], datetime],
        reference_factory: Callable[[], str],
        lifetime: timedelta,
    ) -> None:
        if lifetime <= timedelta(0):
            raise ValueError("dashboard link lifetime must be positive")
        self._repository = repository
        self._access_authority = access_authority
        self._clock = clock
        self._reference_factory = reference_factory
        self._lifetime = lifetime

    def issue(
        self,
        command: DashboardLinkIssueCommand,
        authorization: DashboardAccessAuthorization,
    ) -> DashboardLink:
        now = self._now()
        publication = self._read_active_publication(
            command.tenant_id, command.dashboard_id, command.dashboard_version
        )
        desired, receipt = publication
        current_authorization = self._reauthorize(
            supplied=authorization,
            purpose=command.purpose,
            data_product_version_ref=desired.dataset_product_ref,
        )
        self._validate_scope(command, current_authorization, desired, now)
        expires_at = min(now + self._lifetime, current_authorization.expires_at)
        if expires_at <= now:
            raise DashboardLinkDenied("dashboard access is denied")
        request_digest = digest(
            {
                "command": command,
                "authorization_digest": current_authorization.authorization_digest,
                "desired_digest": desired.desired_digest,
            }
        )
        try:
            session = DashboardLinkSession(
                reference=f"dashboard-link:{self._reference_factory()}",
                tenant_id=command.tenant_id,
                idempotency_key=command.idempotency_key,
                request_digest=request_digest,
                dashboard_id=command.dashboard_id,
                dashboard_version=command.dashboard_version,
                dashboard_revision=desired.revision,
                desired_digest=desired.desired_digest,
                authorization=current_authorization,
                principal_ref=command.principal_ref,
                purpose=command.purpose,
                purpose_digest=command.purpose_digest,
                session_digest=command.session_digest,
                external_url=receipt.external_url,
                issued_at=now,
                expires_at=expires_at,
            )
            stored = self._repository.record_link_session(session)
        except (ValueError, sqlite3.Error) as error:
            raise DashboardLinkDenied("dashboard access is denied") from error
        return DashboardLink(reference=stored.reference, expires_at=stored.expires_at)

    def resolve(
        self, *, tenant_id: str, reference: str, session_digest: str
    ) -> DashboardPrivateTarget:
        now = self._now()
        try:
            session = self._repository.load_link_session(tenant_id, reference)
        except (ValueError, sqlite3.Error) as error:
            raise DashboardLinkDenied("dashboard access is denied") from error
        if session is None or session.session_digest != session_digest or now >= session.expires_at:
            raise DashboardLinkDenied("dashboard access is denied")
        publication = self._read_active_publication(
            session.tenant_id, session.dashboard_id, session.dashboard_version
        )
        desired, receipt = publication
        current_authorization = self._reauthorize(
            supplied=session.authorization,
            purpose=session.purpose,
            data_product_version_ref=desired.dataset_product_ref,
        )
        if (
            current_authorization.authorization_digest != session.authorization.authorization_digest
            or desired.revision != session.dashboard_revision
            or desired.desired_digest != session.desired_digest
            or receipt.external_url != session.external_url
        ):
            raise DashboardLinkDenied("dashboard access is denied")
        self._validate_authorization(current_authorization, now)
        return DashboardPrivateTarget(external_url=session.external_url)

    def _reauthorize(
        self,
        *,
        supplied: DashboardAccessAuthorization,
        purpose: str,
        data_product_version_ref: ArtifactReference,
    ) -> DashboardAccessAuthorization:
        try:
            current = self._access_authority.authorize(
                tenant_id=supplied.tenant_id,
                authorization_id=supplied.authorization_id,
                principal_ref=supplied.principal_ref,
                purpose=purpose,
                dashboard_id=supplied.dashboard_id,
                dashboard_version=supplied.dashboard_version,
                data_product_version_ref=data_product_version_ref,
            )
        except DashboardAccessAuthorityError as error:
            raise DashboardLinkDenied("dashboard access is denied") from error
        if current is None or current.authorization_digest != supplied.authorization_digest:
            raise DashboardLinkDenied("dashboard access is denied")
        return current

    def _read_active_publication(
        self, tenant_id: str, dashboard_id: str, version: int
    ) -> tuple[DashboardDesiredState, DashboardProviderReceipt]:
        try:
            publication = self._repository.load_current_provider_authority(
                tenant_id, dashboard_id, version
            )
        except (ValueError, sqlite3.Error) as error:
            raise DashboardLinkDenied("dashboard access is denied") from error
        if publication is None:
            raise DashboardLinkDenied("dashboard access is denied")
        desired, receipt = publication
        if desired.lifecycle_state != "active" or receipt.lifecycle_state != "active":
            raise DashboardLinkDenied("dashboard access is denied")
        return publication

    @staticmethod
    def _validate_scope(
        command: DashboardLinkIssueCommand,
        authorization: DashboardAccessAuthorization,
        desired: DashboardDesiredState,
        now: datetime,
    ) -> None:
        DashboardLinkService._validate_authorization(authorization, now)
        if (
            authorization.tenant_id != command.tenant_id
            or authorization.dashboard_id != command.dashboard_id
            or authorization.dashboard_version != command.dashboard_version
            or authorization.data_product_version_ref != desired.dataset_product_ref
            or authorization.principal_ref != command.principal_ref
            or authorization.purpose_digest != command.purpose_digest
        ):
            raise DashboardLinkDenied("dashboard access is denied")

    @staticmethod
    def _validate_authorization(authorization: DashboardAccessAuthorization, now: datetime) -> None:
        if (
            authorization.state != "active"
            or now < authorization.effective_at
            or now >= authorization.expires_at
            or authorization.verified_at > now
        ):
            raise DashboardLinkDenied("dashboard access is denied")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("clock must return timezone-aware UTC")
        return now.astimezone(UTC)


def _provider_reference(reference: ArtifactReference) -> str:
    return f"{reference.artifact_id}@{reference.version}#{reference.digest}"
