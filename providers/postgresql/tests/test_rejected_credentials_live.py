"""Rejected credentials at the reader, observer and access connect sites, on the pinned engine.

libpq reports a rejected password at connection startup without a SQLSTATE. These journeys prove
that each provider still classifies it as a non-retryable authorization failure against the
digest-pinned PostgreSQL 18.6 image, while an unreachable server stays transient.
"""

from __future__ import annotations

import os
import socket
from typing import cast

import psycopg
import pytest
from heinzel_dbt_adapter import DbtInvoker
from heinzel_provider_postgresql import (
    PostgresProvider,
    PostgreSQLAccessEffectProvider,
    PostgreSQLAccessSettings,
    PostgreSQLAnswerQueryProvider,
    PostgreSQLAnswerQuerySettings,
    PostgreSQLMaterializationSettings,
    PostgreSQLMaterializationWarehouse,
    PostgreSQLProductSemanticObserver,
    PostgreSQLProductSqlObservationSettings,
    PostgreSQLProductSqlObserver,
    PostgresSettings,
)
from heinzel_provider_postgresql.query_estimator import (
    PostgreSQLQueryEstimator,
    PostgreSQLQueryEstimatorSettings,
)
from heinzel_provider_sdk import AccessEffectProviderError, ProviderError
from pydantic import SecretStr

from providers.postgresql.tests.test_access import _Authority, _command
from providers.postgresql.tests.test_answer_query import _GenerationAuthority
from providers.postgresql.tests.test_answer_query import _request as _answer_request
from providers.postgresql.tests.test_destination_live import (
    postgresql_dsn as postgresql_dsn,
)
from providers.postgresql.tests.test_product_materialization import (
    _DbtMustNotRun,
    _magnitude_model,
    _magnitude_request,
)
from providers.postgresql.tests.test_product_observation import (
    _request as _semantic_observation_request,
)
from providers.postgresql.tests.test_product_sql_observation import _evidence
from providers.postgresql.tests.test_product_sql_observation import (
    _request as _sql_observation_request,
)
from providers.postgresql.tests.test_query_estimator import _request as _estimate_request

pytestmark = [
    pytest.mark.emulator,
    pytest.mark.skipif(
        os.environ.get("HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE") != "1",
        reason="set HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE=1",
    ),
]


def _with_password(dsn: str, password: str) -> str:
    return psycopg.conninfo.make_conninfo(dsn, password=password)


def _closed_loopback_dsn() -> str:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    return psycopg.conninfo.make_conninfo(
        host="127.0.0.1",
        port=port,
        dbname="postgres",
        user="reader",
        password="unused",
        connect_timeout=2,
    )


def _failure_classifications(dsn: str) -> dict[str, str]:
    classifications: dict[str, str] = {}
    with pytest.raises(ProviderError) as estimate:
        PostgreSQLQueryEstimator(
            settings=PostgreSQLQueryEstimatorSettings(
                dsn=SecretStr(dsn), connect_timeout_seconds=4, statement_timeout_seconds=2
            )
        ).estimate(_estimate_request())
    classifications["query_estimator"] = estimate.value.classification
    with pytest.raises(ProviderError) as answer:
        PostgreSQLAnswerQueryProvider(
            settings=PostgreSQLAnswerQuerySettings(dsn=SecretStr(dsn)),
            generation_authority=_GenerationAuthority(),
        ).execute_read_only(_answer_request())
    classifications["answer_query"] = answer.value.classification
    with pytest.raises(ProviderError) as sql_observation:
        PostgreSQLProductSqlObserver(
            PostgreSQLProductSqlObservationSettings(tenant_id="tenant-a", dsn=SecretStr(dsn))
        ).observe(_sql_observation_request(), warehouse_validation=_evidence())
    classifications["product_sql_observation"] = sql_observation.value.classification
    with pytest.raises(ProviderError) as semantic_observation:
        PostgreSQLProductSemanticObserver(
            PostgreSQLMaterializationSettings(
                tenant_id="tenant-a",
                dsn=SecretStr(dsn),
                consumption_schema_name="consumption",
                consumption_view_name="product_revenue",
                control_schema_name="product_control",
                generation_table_name="product_generations",
                generation_pointer_table_name="product_generation_pointers",
            )
        ).observe(_semantic_observation_request())
    classifications["product_observation"] = semantic_observation.value.classification
    with pytest.raises(AccessEffectProviderError) as access:
        PostgreSQLAccessEffectProvider(
            settings=PostgreSQLAccessSettings(administrative_dsn=SecretStr(dsn)),
            targets=_Authority(),
        ).enact(_command())
    classifications["access"] = access.value.outcome
    return classifications


def test_rejected_reader_and_administrative_credentials_are_not_retryable(
    postgresql_dsn: str,
) -> None:
    rejected_dsn = _with_password(postgresql_dsn, "not-the-password")

    classifications = _failure_classifications(rejected_dsn)
    with pytest.raises(psycopg.errors.InvalidPassword):
        PostgresProvider(
            PostgresSettings(
                dsn=SecretStr(rejected_dsn),
                connection_handle="source-live",
                schema_name="raw",
                table_name="raw_orders",
            )
        ).observe()

    assert classifications == {
        "query_estimator": "authorization_denied",
        "answer_query": "authorization_denied",
        "product_sql_observation": "authorization_denied",
        "product_observation": "authorization_denied",
        "access": "permanent_failure",
    }


def test_unreachable_reader_and_administrative_servers_stay_transient() -> None:
    classifications = _failure_classifications(_closed_loopback_dsn())

    assert classifications == {
        "query_estimator": "transient_transport",
        "answer_query": "transient_transport",
        "product_sql_observation": "transient_transport",
        "product_observation": "transient_transport",
        "access": "transient_failure",
    }


def test_rejected_materialization_credentials_are_denied_at_the_target_lock(
    postgresql_dsn: str,
) -> None:
    signed_model = _magnitude_model()
    warehouse = PostgreSQLMaterializationWarehouse(
        settings=PostgreSQLMaterializationSettings(
            tenant_id="tenant-a",
            dsn=SecretStr(_with_password(postgresql_dsn, "not-the-password")),
            consumption_schema_name="consumption",
            consumption_view_name="orders",
            control_schema_name="control",
            generation_table_name="generations",
            generation_pointer_table_name="pointers",
        ),
        signed_model=signed_model,
        invoker=cast(DbtInvoker, _DbtMustNotRun()),
    )

    with pytest.raises(ProviderError) as denied:
        warehouse.execute(_magnitude_request(signed_model))

    assert denied.value.classification == "authorization_denied"
