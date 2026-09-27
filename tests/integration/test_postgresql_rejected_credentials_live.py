"""Rejected credentials at LAND and product materialization, on the pinned PostgreSQL engine.

libpq reports a rejected password at connection startup without a SQLSTATE. These journeys prove
that the destination and materialization providers still classify it as `authorization_denied`
against the digest-pinned PostgreSQL 18.6 image, while an unreachable server stays transient.
"""

from __future__ import annotations

import os
import secrets
import socket

import psycopg
import pytest
from heinzel_provider_postgresql import (
    PostgreSQLLandStore,
    PostgreSQLLandStoreSettings,
    PostgreSQLMaterializationSettings,
    PostgreSQLProductGenerationAuthority,
)
from heinzel_provider_postgresql.destination import PostgreSQLLandStoreError
from heinzel_provider_sdk import ProviderError
from heinzel_runtime import AnswerProductGenerationReference, AnswerQueryReference
from pydantic import SecretStr

from tests.integration.test_postgresql_checked_sum_evidence import _pinned_postgresql
from tests.integration.test_postgresql_product_materialization_live import (
    _provision_cluster,
    _role_dsn,
)

_RUN_LIVE = os.environ.get("HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE") == "1"

pytestmark = [
    pytest.mark.emulator,
    pytest.mark.skipif(not _RUN_LIVE, reason="set HEINZEL_RUN_PRODUCT_SQL_CONFORMANCE=1"),
]


def _land_store(dsn: str) -> PostgreSQLLandStore:
    return PostgreSQLLandStore(
        PostgreSQLLandStoreSettings(
            dsn=SecretStr(dsn),
            raw_schema_name="raw",
            ledger_schema_name="land_control",
            ledger_table_name="land_receipts",
        )
    )


def _generation_authority(dsn: str) -> PostgreSQLProductGenerationAuthority:
    return PostgreSQLProductGenerationAuthority(
        PostgreSQLMaterializationSettings(
            tenant_id="tenant-live-a",
            dsn=SecretStr(dsn),
            consumption_schema_name="consumption",
            consumption_view_name="product_revenue",
            control_schema_name="product_control",
            generation_table_name="product_generations",
            generation_pointer_table_name="product_generation_pointers",
        )
    )


def _generation_reference() -> AnswerProductGenerationReference:
    return AnswerProductGenerationReference(
        product_ref=AnswerQueryReference(artifact_id="product-revenue", version=1, digest="1" * 64),
        generation=1,
    )


def _closed_loopback_dsn(role: str) -> str:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    return psycopg.conninfo.make_conninfo(
        host="127.0.0.1",
        port=port,
        dbname="postgres",
        user=role,
        password="unused",
        connect_timeout=2,
    )


def test_rejected_landing_and_materialization_credentials_are_authorization_denied() -> None:
    landing_password = secrets.token_urlsafe(24)
    with _pinned_postgresql() as bootstrap_dsn:
        _provision_cluster(
            bootstrap_dsn,
            acquisition_password=secrets.token_urlsafe(24),
            landing_password=landing_password,
            materialization_password=secrets.token_urlsafe(24),
        )
        accepted_receipt = _land_store(
            _role_dsn(bootstrap_dsn, "landing_runtime", landing_password)
        ).inspect_receipt(idempotency_key="4" * 64)
        with pytest.raises(PostgreSQLLandStoreError) as landing_denied:
            _land_store(
                _role_dsn(bootstrap_dsn, "landing_runtime", "not-the-password")
            ).inspect_receipt(idempotency_key="4" * 64)
        with pytest.raises(ProviderError) as materialization_denied:
            _generation_authority(
                _role_dsn(bootstrap_dsn, "materialization_runtime", "not-the-password")
            ).observe(_generation_reference())

    assert accepted_receipt is None
    assert landing_denied.value.classification == "authorization_denied"
    assert materialization_denied.value.classification == "authorization_denied"
    assert "not-the-password" not in str(landing_denied.value)
    assert "not-the-password" not in str(materialization_denied.value)


def test_unreachable_landing_and_materialization_servers_stay_transient() -> None:
    with pytest.raises(PostgreSQLLandStoreError) as landing_unreachable:
        _land_store(_closed_loopback_dsn("landing_runtime")).inspect_receipt(
            idempotency_key="4" * 64
        )
    with pytest.raises(ProviderError) as materialization_unreachable:
        _generation_authority(_closed_loopback_dsn("materialization_runtime")).observe(
            _generation_reference()
        )

    assert landing_unreachable.value.classification == "transient_transport"
    assert materialization_unreachable.value.classification == "transient_transport"
