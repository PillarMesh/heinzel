from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from heinzel_contract_model import digest
from heinzel_provider_clickhouse import (
    CLICKHOUSE_SERVER_VERSION,
    ClickHousePublicationConformanceProbe,
    ClickHousePublicationConformanceRequest,
    ClickHousePublicationConformanceSettings,
)
from heinzel_provider_sdk import ProviderError
from pydantic import SecretStr

_NOW = datetime(2026, 9, 15, 22, tzinfo=UTC)
_INITIAL_UUID = "11111111-1111-4111-8111-111111111111"
_REPLACEMENT_UUID = "22222222-2222-4222-8222-222222222222"


class _Response:
    status_code = 200
    exception_code: int | None = None

    def __init__(self, lines: tuple[str, ...] = ()) -> None:
        self.lines = lines


class _Transport:
    def __init__(self) -> None:
        self.view_target = "revenue_v1_g1"
        self.sealed = False
        self.lose_view_response = False
        self.lose_seal_response = False
        self.invalid_replacement_identity = False
        self.partial_revokes_observed = False
        self.calls: list[dict[str, object]] = []

    def execute(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        content: bytes,
        timeout_seconds: float,
    ) -> _Response:
        statement = content.decode("utf-8")
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "statement": statement,
                "timeout_seconds": timeout_seconds,
            }
        )
        if statement == "SELECT version() FORMAT TabSeparatedRaw":
            return _Response((CLICKHOUSE_SERVER_VERSION,))
        if statement.startswith("SELECT uuid, engine, create_table_query FROM system.tables"):
            name = params["param_table"]
            if name == "revenue_v1_g1":
                return _Response(
                    (
                        f"{_INITIAL_UUID}\tMergeTree\tCREATE TABLE product.revenue_v1_g1 "
                        "(region String) ENGINE = MergeTree ORDER BY region",
                    )
                )
            engine = "Memory" if self.invalid_replacement_identity else "MergeTree"
            return _Response(
                (
                    f"{_REPLACEMENT_UUID}\t{engine}\tCREATE TABLE product.revenue_v1_g2 "
                    f"(region String) ENGINE = {engine} ORDER BY region",
                )
            )
        if statement.startswith("SELECT create_table_query FROM system.tables"):
            return _Response(
                (
                    "CREATE VIEW consumption.revenue_current AS SELECT * FROM product."
                    + self.view_target,
                )
            )
        if statement.startswith("CREATE OR REPLACE VIEW"):
            if not self.sealed or not self.partial_revokes_observed:
                raise AssertionError("stable view changed before the generation seal was proven")
            self.view_target = "revenue_v1_g2"
            if self.lose_view_response:
                self.lose_view_response = False
                raise TimeoutError("response lost after view replacement")
            return _Response()
        if statement.startswith("CHECK GRANT SELECT"):
            return _Response(("1",))
        if statement.startswith("CHECK GRANT"):
            return _Response(("0" if self.sealed else "1",))
        if statement.startswith("REVOKE"):
            self.sealed = True
            if self.lose_seal_response:
                self.lose_seal_response = False
                raise TimeoutError("response lost after revoke")
            return _Response()
        if statement.startswith("SELECT access_type, database, table, is_partial_revoke"):
            if not self.sealed:
                return _Response()
            self.partial_revokes_observed = True
            return _Response(
                (
                    "INSERT\tproduct\trevenue_v1_g2\t1",
                    "ALTER TABLE\tproduct\trevenue_v1_g2\t1",
                    "ALTER VIEW\tproduct\trevenue_v1_g2\t1",
                    "DROP TABLE\tproduct\trevenue_v1_g2\t1",
                )
            )
        raise AssertionError(f"unexpected statement: {statement}")


def _settings() -> ClickHousePublicationConformanceSettings:
    return ClickHousePublicationConformanceSettings(
        endpoint="https://clickhouse.test",
        administration_username="administrator",
        administration_password=SecretStr("private-admin"),
        transformation_username="transformer",
        transformation_password=SecretStr("private-transform"),
    )


def _request() -> ClickHousePublicationConformanceRequest:
    return ClickHousePublicationConformanceRequest(
        product_database="product",
        consumption_database="consumption",
        initial_generation_table="revenue_v1_g1",
        replacement_generation_table="revenue_v1_g2",
        stable_view_name="revenue_current",
        transformation_role="transformation_runtime",
    )


def test_probe_observes_identity_view_replacement_and_exact_write_seal() -> None:
    transport = _Transport()
    probe = ClickHousePublicationConformanceProbe(
        settings=_settings(), transport=transport, clock=lambda: _NOW
    )

    evidence = probe.run(_request())

    assert evidence.engine_version == CLICKHOUSE_SERVER_VERSION
    assert evidence.initial_generation_uuid == _INITIAL_UUID
    assert evidence.replacement_generation_uuid == _REPLACEMENT_UUID
    assert evidence.generation_engine == "MergeTree"
    assert evidence.observed_view_target == "product.revenue_v1_g2"
    assert evidence.sealed_write_privileges == ("ALTER", "DROP TABLE", "INSERT")
    assert evidence.view_ambiguous_outcome_reconciled is False
    assert evidence.seal_ambiguous_outcome_reconciled is False
    assert evidence.observed_at == _NOW
    assert evidence.evidence_digest == digest(
        evidence.model_dump(exclude={"evidence_digest"}, mode="python")
    )
    view_effects = [
        call for call in transport.calls if str(call["statement"]).startswith("CREATE OR REPLACE")
    ]
    seal_effects = [call for call in transport.calls if str(call["statement"]).startswith("REVOKE")]
    assert len(view_effects) == 1
    assert len(seal_effects) == 1
    assert all(call["params"] == {"readonly": "0"} for call in view_effects + seal_effects)
    serialized = evidence.model_dump_json()
    assert "private-admin" not in serialized
    assert "private-transform" not in serialized


def test_probe_proves_the_write_seal_before_mutating_the_stable_view() -> None:
    transport = _Transport()
    probe = ClickHousePublicationConformanceProbe(settings=_settings(), transport=transport)

    probe.run(_request())

    statements = [str(call["statement"]) for call in transport.calls]
    view_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("CREATE OR REPLACE")
    )
    partial_revoke_index = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("SELECT access_type, database, table, is_partial_revoke")
    )
    assert partial_revoke_index < view_index


@pytest.mark.parametrize("lost_effect", ["view", "seal"])
def test_probe_reconciles_a_lost_response_without_repeating_the_effect(lost_effect: str) -> None:
    transport = _Transport()
    if lost_effect == "view":
        transport.lose_view_response = True
    else:
        transport.lose_seal_response = True
    probe = ClickHousePublicationConformanceProbe(settings=_settings(), transport=transport)

    evidence = probe.run(_request())

    assert evidence.view_ambiguous_outcome_reconciled is (lost_effect == "view")
    assert evidence.seal_ambiguous_outcome_reconciled is (lost_effect == "seal")
    effect_prefix = "CREATE OR REPLACE" if lost_effect == "view" else "REVOKE"
    assert sum(str(call["statement"]).startswith(effect_prefix) for call in transport.calls) == 1


def test_probe_fails_closed_when_a_generation_is_not_a_mergetree() -> None:
    transport = _Transport()
    transport.invalid_replacement_identity = True
    probe = ClickHousePublicationConformanceProbe(settings=_settings(), transport=transport)

    with pytest.raises(ProviderError) as captured:
        probe.run(_request())

    assert captured.value.classification == "integrity_failure"
    assert not any(
        str(call["statement"]).startswith("CREATE OR REPLACE") for call in transport.calls
    )


def test_probe_fails_closed_when_sealing_cannot_be_observed() -> None:
    class _UnsealedTransport(_Transport):
        def execute(
            self,
            *,
            url: str,
            headers: Mapping[str, str],
            params: Mapping[str, str],
            content: bytes,
            timeout_seconds: float,
        ) -> _Response:
            statement = content.decode("utf-8")
            if statement.startswith("REVOKE"):
                self.calls.append({"statement": statement, "params": params})
                return _Response()
            return super().execute(
                url=url,
                headers=headers,
                params=params,
                content=content,
                timeout_seconds=timeout_seconds,
            )

    transport = _UnsealedTransport()
    probe = ClickHousePublicationConformanceProbe(settings=_settings(), transport=transport)

    with pytest.raises(ProviderError) as captured:
        probe.run(_request())

    assert captured.value.classification == "integrity_failure"
