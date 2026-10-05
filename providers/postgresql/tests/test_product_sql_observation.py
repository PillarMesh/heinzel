from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_contract_model import digest
from heinzel_provider_postgresql import (
    PostgreSQLProductSqlObservationRequest,
    PostgreSQLProductSqlObservationSettings,
    PostgreSQLProductSqlObserver,
)
from heinzel_provider_sdk import (
    InvalidProductSqlProviderObservation,
    ProductSqlProviderObservationSigner,
    ProductSqlProviderObservationVerifier,
    ProviderError,
)
from heinzel_warehouse_control import (
    EncryptionAtRestDisposition,
    EngineKind,
    WarehouseValidationEvidence,
    WarehouseValidationProfile,
)
from pydantic import SecretStr

_NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
_IMAGE_DIGEST = "33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935"
_BUILD_DIGEST = digest(
    {
        "domain": "heinzel-postgresql-engine-build-v1",
        "version": {
            "server_version_num": "180006",
            "server_version": "18.6 (Debian 18.6-1.pgdg13+1)",
        },
    }
)


class _Cursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _Connection:
    def __init__(self, results: list[list[tuple[object, ...]]]) -> None:
        self._results = results
        self.statements: list[tuple[object, tuple[object, ...]]] = []
        self.closed = False

    def execute(self, statement: object, parameters: tuple[object, ...] = ()) -> _Cursor:
        self.statements.append((statement, parameters))
        return _Cursor(self._results.pop(0) if self._results else [])

    def close(self) -> None:
        self.closed = True


def _settings() -> PostgreSQLProductSqlObservationSettings:
    return PostgreSQLProductSqlObservationSettings(
        tenant_id="tenant-a",
        dsn=SecretStr("postgresql://observer:private@localhost/warehouse"),
    )


def _request(**changes: object) -> PostgreSQLProductSqlObservationRequest:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "warehouse_binding_id": "warehouse-a",
        "warehouse_binding_revision": 4,
        "relation_ref": "relation-revenue-events-v1",
        "relation_namespace": "raw",
        "relation_name": "revenue_events",
    }
    values.update(changes)
    return PostgreSQLProductSqlObservationRequest.model_validate(values)


def _evidence(**changes: object) -> WarehouseValidationEvidence:
    values: dict[str, object] = {
        "evidence_id": "warehouse-validation-1",
        "tenant_id": "tenant-a",
        "binding_id": "warehouse-a",
        "binding_revision": 4,
        "validation_profile": WarehouseValidationProfile.LOCAL_ACCEPTANCE,
        "engine_kind": EngineKind.POSTGRESQL,
        "engine_version": "18.6",
        "engine_build_digest": _BUILD_DIGEST,
        "engine_image_digest": _IMAGE_DIGEST,
        "principal_profile_digest": "1" * 64,
        "namespace_grant_matrix_digest": "2" * 64,
        "tls_probe_digest": "3" * 64,
        "network_isolation_probe_digest": "4" * 64,
        "encryption_at_rest_evidence_digest": "5" * 64,
        "encryption_at_rest_disposition": (EncryptionAtRestDisposition.DEFERRED_LOCAL_ACCEPTANCE),
        "positive_probe_digest": "6" * 64,
        "denial_probe_digest": "7" * 64,
        "ledger_probe_digest": "8" * 64,
        "monitoring_probe_digest": "9" * 64,
        "capacity_alert_probe_digest": "a" * 64,
        "backup_artifact_digest": "b" * 64,
        "restore_verification_digest": "c" * 64,
        "restore_cleanup_digest": "d" * 64,
        "observed_at": _NOW,
    }
    values.update(changes)
    return WarehouseValidationEvidence.model_validate(values)


def _successful_results() -> list[list[tuple[object, ...]]]:
    return [
        [],
        [("180006", "18.6 (Debian 18.6-1.pgdg13+1)", "UTC", "UTF8", 0, _NOW)],
        [],
        [(16425, "r", True, True)],
        [
            (1, "region", "text", False, "text", None, None, "pg_catalog", "C", "UTF8"),
            (
                2,
                "revenue",
                "numeric(38,9)",
                False,
                "numeric",
                38,
                9,
                None,
                None,
                None,
            ),
        ],
        [("internal", "numeric")],
        [("199999999999999999999999999999.999999998", 2, 0)],
        [],
    ]


def test_observer_binds_live_relation_and_sum_semantics_to_warehouse_evidence() -> None:
    connection = _Connection(_successful_results())
    observer = PostgreSQLProductSqlObserver(_settings(), connect=lambda _dsn: connection)

    observation = observer.observe(_request(), warehouse_validation=_evidence())

    assert observation.tenant_id == "tenant-a"
    assert observation.warehouse_binding_id == "warehouse-a"
    assert observation.warehouse_binding_revision == 4
    assert observation.engine_version == "18.6"
    assert observation.engine_image_digest == _IMAGE_DIGEST
    assert observation.engine_build_digest == _BUILD_DIGEST
    assert tuple(column.name for column in observation.columns) == ("region", "revenue")
    assert observation.columns[0].physical_type == "TEXT"
    assert observation.columns[0].collation == "C"
    assert observation.columns[0].encoding == "UTF8"
    assert observation.columns[1].decimal_precision == 38
    assert observation.columns[1].decimal_scale == 9
    assert observation.sum_semantics.input_physical_type == "NUMERIC(38,9)"
    assert observation.sum_semantics.accumulator_physical_type == "INTERNAL"
    assert observation.sum_semantics.result_physical_type == "NUMERIC"
    assert observation.sum_semantics.overflow_behavior == "promote"
    assert observation.sum_semantics.null_input_behavior == "exclude"
    assert observation.sum_semantics.empty_group_behavior == "no_row"
    assert connection.statements[0][0] == (
        "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    assert connection.statements[-1][0] == "ROLLBACK"
    assert connection.closed is True
    assert "private" not in observation.model_dump_json()


def test_observe_signed_signs_the_exact_single_raw_observation_inside_provider_boundary() -> None:
    connection = _Connection(_successful_results())
    connection_count = 0

    def connect(dsn: str) -> _Connection:
        nonlocal connection_count
        del dsn
        connection_count += 1
        return connection

    private_key = Ed25519PrivateKey.generate()
    observer = PostgreSQLProductSqlObserver(
        _settings(),
        connect=connect,
        signer=ProductSqlProviderObservationSigner("postgresql-provider-key-1", private_key),
    )

    signed = observer.observe_signed(_request(), warehouse_validation=_evidence())

    assert connection_count == 1
    assert signed.observation_digest == digest(signed.observation)
    assert (
        ProductSqlProviderObservationVerifier(
            {"postgresql-provider-key-1": private_key.public_key()},
            maximum_observation_age=timedelta(minutes=1),
        ).verify(signed, evaluated_at=_NOW)
        == signed.observation
    )
    assert len(connection.statements) == len(_successful_results())


def test_observe_signed_rejects_key_substitution() -> None:
    private_key = Ed25519PrivateKey.generate()
    observer = PostgreSQLProductSqlObserver(
        _settings(),
        connect=lambda _dsn: _Connection(_successful_results()),
        signer=ProductSqlProviderObservationSigner("postgresql-provider-key-1", private_key),
    )
    signed = observer.observe_signed(_request(), warehouse_validation=_evidence())
    verifier = ProductSqlProviderObservationVerifier(
        {"postgresql-provider-key-1": Ed25519PrivateKey.generate().public_key()},
        maximum_observation_age=timedelta(minutes=1),
    )

    with pytest.raises(InvalidProductSqlProviderObservation, match="signature"):
        verifier.verify(signed, evaluated_at=_NOW)


def test_observe_signed_rejects_missing_signer_before_connecting() -> None:
    connected = False

    def connect(dsn: str) -> _Connection:
        nonlocal connected
        del dsn
        connected = True
        return _Connection(_successful_results())

    observer = PostgreSQLProductSqlObserver(
        _settings(),
        connect=connect,
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe_signed(_request(), warehouse_validation=_evidence())

    assert caught.value.classification == "permanent_configuration"
    assert connected is False


def test_observer_rejects_malformed_signer_configuration_before_connecting() -> None:
    connected = False

    def connect(dsn: str) -> _Connection:
        nonlocal connected
        del dsn
        connected = True
        return _Connection(_successful_results())

    with pytest.raises(ProviderError) as caught:
        PostgreSQLProductSqlObserver(
            _settings(),
            connect=connect,
            signer=cast(ProductSqlProviderObservationSigner, object()),
        )

    assert caught.value.classification == "permanent_configuration"
    assert connected is False


@pytest.mark.parametrize(
    "evidence",
    (
        _evidence(binding_revision=5),
        _evidence(engine_build_digest="e" * 64),
        _evidence(engine_image_digest="f" * 64),
    ),
)
def test_observer_rejects_mismatched_authority_before_or_during_observation(
    evidence: WarehouseValidationEvidence,
) -> None:
    connection = _Connection(_successful_results())
    observer = PostgreSQLProductSqlObserver(_settings(), connect=lambda _dsn: connection)

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request(), warehouse_validation=evidence)

    assert caught.value.classification == "integrity_failure"
    assert "private" not in str(caught.value)


def test_observer_rejects_wrong_tenant_without_connecting() -> None:
    connected = False

    def connect(dsn: str) -> _Connection:
        nonlocal connected
        del dsn
        connected = True
        return _Connection([])

    observer = PostgreSQLProductSqlObserver(_settings(), connect=connect)

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request(tenant_id="tenant-b"), warehouse_validation=_evidence())

    assert caught.value.classification == "authorization_denied"
    assert connected is False


def test_observer_rejects_relation_without_select_privilege() -> None:
    results = _successful_results()
    results[3] = [(16425, "r", True, False)]
    observer = PostgreSQLProductSqlObserver(_settings(), connect=lambda _dsn: _Connection(results))

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert caught.value.classification == "authorization_denied"


def test_observer_rejects_malformed_or_unsupported_column_observation() -> None:
    results = _successful_results()
    results[4][1] = (2, "revenue", "double precision", False, "float8")
    observer = PostgreSQLProductSqlObserver(_settings(), connect=lambda _dsn: _Connection(results))

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert caught.value.classification == "invalid_provider_response"


def test_observer_describes_a_whole_landing_relation_including_unread_columns() -> None:
    """A real landing relation has JSON and bookkeeping columns a product statement never reads.

    They are reported rather than refused, so the compiler can check the two columns the statement
    does read. SUM semantics are observed for the statement's NUMERIC(38,9) input type, because a
    landing relation has no decimal column of its own.
    """
    results = _successful_results()
    results[4] = [
        (1, "generation_id", "text", False, "text", None, None, "pg_catalog", "default", "UTF8"),
        (2, "row_ordinal", "bigint", False, "int8", None, None, None, None, None),
        (3, "payload", "jsonb", False, "jsonb", None, None, None, None, None),
        (4, "score", "double precision", True, "float8", None, None, None, None, None),
        (5, "amount", "numeric", True, "numeric", None, None, None, None, None),
    ]
    observer = PostgreSQLProductSqlObserver(_settings(), connect=lambda _dsn: _Connection(results))

    observation = observer.observe(_request(), warehouse_validation=_evidence())

    assert tuple(
        (c.name, c.logical_type, c.physical_type, c.nullable) for c in observation.columns
    ) == (
        ("generation_id", "string", "TEXT", False),
        ("row_ordinal", "other", "BIGINT", False),
        ("payload", "json", "JSONB", False),
        ("score", "other", "DOUBLE PRECISION", True),
        ("amount", "other", "NUMERIC", True),
    )
    assert observation.columns[0].collation == "default"
    assert observation.sum_semantics.input_physical_type == "NUMERIC(38,9)"


def test_observer_rejects_a_non_utc_execution_context() -> None:
    results = _successful_results()
    results[1] = [
        (
            "180006",
            "18.6 (Debian 18.6-1.pgdg13+1)",
            "Europe/London",
            "UTF8",
            3_600,
            _NOW,
        )
    ]
    observer = PostgreSQLProductSqlObserver(_settings(), connect=lambda _dsn: _Connection(results))

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert caught.value.classification == "integrity_failure"


def test_observer_classifies_transport_failure_without_leaking_driver_message() -> None:
    secret = "private-host-statement"
    observer = PostgreSQLProductSqlObserver(
        _settings(),
        connect=lambda _dsn: (_ for _ in ()).throw(psycopg.OperationalError(secret)),
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert caught.value.classification == "transient_transport"
    assert secret not in str(caught.value)


def test_observer_rejects_incorrect_sum_probe_results() -> None:
    results = _successful_results()
    results[6] = [(str(Decimal("1")), 1, 1)]
    observer = PostgreSQLProductSqlObserver(_settings(), connect=lambda _dsn: _Connection(results))

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert caught.value.classification == "integrity_failure"


@pytest.mark.parametrize(
    ("probe_outcome", "classification"),
    (
        (psycopg.errors.InvalidPassword(), "authorization_denied"),
        (psycopg.OperationalError("probe transport failed"), "transient_transport"),
    ),
)
def test_observer_attributes_only_structured_startup_rejections(
    probe_outcome: Exception, classification: str
) -> None:
    probe_calls: list[dict[str, object]] = []

    def rejected(dsn: str) -> _Connection:
        raise psycopg.OperationalError("localized startup rejection without SQLSTATE")

    def probe(**parameters: object) -> _Connection:
        probe_calls.append(parameters)
        raise probe_outcome

    observer = PostgreSQLProductSqlObserver(
        _settings().model_copy(
            update={
                "dsn": SecretStr(
                    "host=warehouse.internal dbname=warehouse user=observer "
                    "password=private-secret sslmode=disable gssencmode=disable"
                )
            }
        ),
        connect=rejected,
        startup_denial_probe=probe,
    )

    with pytest.raises(ProviderError) as caught:
        observer.observe(_request(), warehouse_validation=_evidence())

    assert caught.value.classification == classification
    assert len(probe_calls) == 1
    assert "private" not in str(caught.value)
