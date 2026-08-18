from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pillarmesh_contract_model import FIXED_PROJECTION, IntegrationContract
from pillarmesh_evidence import PackageMetadata

from tests.acceptance import run_m0
from tests.acceptance.config import REQUIRED_VARIABLES
from tests.acceptance.private_files import atomic_private_replace
from tests.acceptance.provider_adapter import (
    TRUSTED_POSTGRES_AUDIT_BODY_DIGEST,
    TRUSTED_POSTGRES_AUDIT_CONFIG,
)
from tests.acceptance.run_m0 import (
    AcceptanceConfig,
    AcceptanceHarness,
    AcceptanceResult,
    CliTimeout,
    DedicatedEnvironmentAttestation,
    ExpectedRunResources,
    HarnessError,
    NegativeObservation,
    PrivateResourceLedger,
    ProviderResourceState,
    ReplayObservation,
    RunEvidence,
    RunProgress,
    RunReservation,
    RuntimeIdentities,
    StateCounters,
    SubprocessCli,
    VisibilityObservation,
    build_contract,
    cleanup_status,
    opaque_label,
    preflight,
    snowflake_access_denied,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
KEY = 984_201


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "PILLARMESH_STATE_PATH": str(tmp_path / "operator-state.db"),
        "PILLARMESH_OUTPUT_DIR": str(tmp_path / "operator-output"),
        "PILLARMESH_CLEANUP_LEDGER_PATH": str(tmp_path / "cleanup-ledger.json"),
        "PILLARMESH_SIGNING_KEY_ID": "operator-one-key",
        "PILLARMESH_SIGNING_PRIVATE_KEY_B64": "signing-private-canary",
        "PILLARMESH_POSTGRES_DSN": "postgresql://runtime-one:secret@db/m0_acceptance",
        "PILLARMESH_POSTGRES_DATABASE": "m0_acceptance",
        "PILLARMESH_POSTGRES_RUNTIME_PRINCIPAL": "runtime_one",
        "PILLARMESH_POSTGRES_OWNER_PRINCIPAL": "m0_owner",
        "PILLARMESH_POSTGRES_CONNECTION_HANDLE": "pg-operator-one",
        "PILLARMESH_POSTGRES_SCHEMA": "pillarmesh_m0",
        "PILLARMESH_POSTGRES_TABLE": "orders",
        "PILLARMESH_POSTGRES_DENIAL_SCHEMA": "unrelated_private",
        "PILLARMESH_POSTGRES_FIXTURE_DSN": "postgresql://fixture:secret@db/m0_acceptance",
        "PILLARMESH_POSTGRES_FIXTURE_PRINCIPAL": "fixture",
        "PILLARMESH_SNOWFLAKE_ACCOUNT": "DEDICATED_ACCOUNT",
        "PILLARMESH_SNOWFLAKE_USER": "RUNTIME_ONE",
        "PILLARMESH_SNOWFLAKE_PASSWORD": "snowflake-password-canary",
        "PILLARMESH_SNOWFLAKE_OWNER_USER": "M0_OWNER",
        "PILLARMESH_SNOWFLAKE_ROLE": "PILLARMESH_M0_RUNTIME",
        "PILLARMESH_SNOWFLAKE_WAREHOUSE": "PILLARMESH_M0_WH",
        "PILLARMESH_SNOWFLAKE_DATABASE": "PILLARMESH_M0",
        "PILLARMESH_SNOWFLAKE_SCHEMA": "TRANSFER",
        "PILLARMESH_SNOWFLAKE_STAGE": "M0_STAGE",
        "PILLARMESH_SNOWFLAKE_TARGET_TABLE": "ORDERS",
        "PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE": "ORDERS_UNSUPPORTED_KEY",
        "PILLARMESH_SNOWFLAKE_LEDGER_TABLE": "COMMIT_LEDGER",
        "PILLARMESH_SNOWFLAKE_CONNECTION_HANDLE": "sf-operator-one",
        "PILLARMESH_SNOWFLAKE_DENIAL_DATABASE": "UNRELATED_PRIVATE",
        "PILLARMESH_CREDENTIAL_CANARIES_JSON": '["credential-one","credential-two"]',
        "PILLARMESH_ROW_VALUE_CANARY": "synthetic-row-canary",
        "PILLARMESH_OPERATOR_PSEUDONYM": "operator-one",
        "PILLARMESH_HOST_PSEUDONYM": "host-one",
        "PILLARMESH_MCP_PROTOCOL_VERSION": "2025-06-18",
        "PILLARMESH_OWNER_AUTHORIZATION_REFERENCE": "approval-ticket-opaque",
    }


def _attestation(environment_identity: str) -> DedicatedEnvironmentAttestation:
    return DedicatedEnvironmentAttestation(
        identities=RuntimeIdentities(
            postgres_runtime_session="runtime_one",
            postgres_runtime_current="runtime_one",
            postgres_fixture_session="fixture",
            postgres_fixture_current="fixture",
            snowflake_runtime="RUNTIME_ONE",
        ),
        postgres_database="m0_acceptance",
        postgres_database_owner="m0_owner",
        postgres_schema_owner="m0_owner",
        postgres_marker_environment_id=environment_identity,
        postgres_source_kind="base_table",
        postgres_marker_kind="base_table",
        postgres_source_owner="m0_owner",
        postgres_marker_owner="m0_owner",
        postgres_audit_kind="function",
        postgres_audit_owner="m0_owner",
        postgres_audit_security_definer=True,
        postgres_audit_language="sql",
        postgres_audit_volatility="stable",
        postgres_audit_result="bigint",
        postgres_audit_config=TRUSTED_POSTGRES_AUDIT_CONFIG,
        postgres_audit_body_digest=TRUSTED_POSTGRES_AUDIT_BODY_DIGEST,
        postgres_runtime_grants=(
            "CONNECT_DATABASE",
            "EXECUTE_AUDIT",
            "SELECT_MARKER",
            "SELECT_SOURCE",
            "USAGE_SCHEMA",
        ),
        postgres_fixture_grants=(
            "CONNECT_DATABASE",
            "DELETE_SOURCE",
            "INSERT_SOURCE",
            "SELECT_SOURCE_KEY_COLUMN",
            "USAGE_SCHEMA",
        ),
        postgres_denial_schema_exists=True,
        snowflake_account="DEDICATED_ACCOUNT",
        snowflake_account_locator="DEDICATED_ACCOUNT_LOCATOR",
        snowflake_role="PILLARMESH_M0_RUNTIME",
        snowflake_user_roles=("PILLARMESH_M0_RUNTIME",),
        snowflake_marker_environment_identity=environment_identity,
        snowflake_marker_owner_user="M0_OWNER",
        snowflake_marker_owner_role="PILLARMESH_M0_OWNER",
        snowflake_marker_denial_database="UNRELATED_PRIVATE",
        snowflake_marker_denial_database_owner_role="PILLARMESH_M0_OWNER",
        snowflake_object_kinds=(
            ("database", "DATABASE"),
            ("environment_marker", "TABLE"),
            ("file_format", "FILE FORMAT"),
            ("ledger", "TABLE"),
            ("negative_target", "TABLE"),
            ("schema", "SCHEMA"),
            ("stage", "STAGE"),
            ("target", "TABLE"),
            ("warehouse", "WAREHOUSE"),
        ),
        snowflake_object_owners=(
            ("database", "PILLARMESH_M0_OWNER"),
            ("environment_marker", "PILLARMESH_M0_OWNER"),
            ("file_format", "PILLARMESH_M0_OWNER"),
            ("ledger", "PILLARMESH_M0_OWNER"),
            ("negative_target", "PILLARMESH_M0_OWNER"),
            ("schema", "PILLARMESH_M0_OWNER"),
            ("stage", "PILLARMESH_M0_OWNER"),
            ("target", "PILLARMESH_M0_OWNER"),
            ("warehouse", "PILLARMESH_M0_OWNER"),
        ),
        snowflake_object_grants=(
            ("database", "OWNERSHIP", "PILLARMESH_M0_OWNER"),
            ("database", "USAGE", "PILLARMESH_M0_RUNTIME"),
            ("environment_marker", "OWNERSHIP", "PILLARMESH_M0_OWNER"),
            ("environment_marker", "SELECT", "PILLARMESH_M0_RUNTIME"),
            ("file_format", "OWNERSHIP", "PILLARMESH_M0_OWNER"),
            ("ledger", "INSERT", "PILLARMESH_M0_RUNTIME"),
            ("ledger", "OWNERSHIP", "PILLARMESH_M0_OWNER"),
            ("ledger", "SELECT", "PILLARMESH_M0_RUNTIME"),
            ("negative_target", "OWNERSHIP", "PILLARMESH_M0_OWNER"),
            ("negative_target", "SELECT", "PILLARMESH_M0_RUNTIME"),
            ("schema", "OWNERSHIP", "PILLARMESH_M0_OWNER"),
            ("schema", "USAGE", "PILLARMESH_M0_RUNTIME"),
            ("stage", "OWNERSHIP", "PILLARMESH_M0_OWNER"),
            ("stage", "READ", "PILLARMESH_M0_RUNTIME"),
            ("stage", "WRITE", "PILLARMESH_M0_RUNTIME"),
            ("target", "INSERT", "PILLARMESH_M0_RUNTIME"),
            ("target", "OWNERSHIP", "PILLARMESH_M0_OWNER"),
            ("target", "SELECT", "PILLARMESH_M0_RUNTIME"),
            ("target", "UPDATE", "PILLARMESH_M0_RUNTIME"),
            ("warehouse", "OWNERSHIP", "PILLARMESH_M0_OWNER"),
            ("warehouse", "USAGE", "PILLARMESH_M0_RUNTIME"),
        ),
        snowflake_runtime_grants=(
            "INSERT_LEDGER",
            "INSERT_TARGET",
            "READ_STAGE",
            "SELECT_ENVIRONMENT_MARKER",
            "SELECT_LEDGER",
            "SELECT_NEGATIVE_TARGET",
            "SELECT_TARGET",
            "UPDATE_TARGET",
            "USAGE_DATABASE",
            "USAGE_SCHEMA",
            "USAGE_WAREHOUSE",
            "WRITE_STAGE",
        ),
    )


class FakeProviders:
    def __init__(
        self,
        events: list[str],
        *,
        fail_insert: BaseException | None = None,
        replay_observations: tuple[ReplayObservation, ReplayObservation] | None = None,
        negative_observations: tuple[NegativeObservation, NegativeObservation] | None = None,
        reconciliation: ProviderResourceState | None = None,
        reconciliation_error: BaseException | None = None,
        fail_admission_assertion_at: int | None = None,
    ) -> None:
        self.events = events
        self.environment_identity = ""
        self.fail_insert = fail_insert
        self.calls = 0
        self.fixture_exists = False
        replay = ReplayObservation(
            target_rows=1,
            target_value_digest="a" * 64,
            ledger_rows=1,
            ledger_manifest_digest="1" * 64,
            ledger_committed_identity="8" * 64,
            stage_state_digest="5" * 64,
            product_mutation_count=1,
        )
        self.replay_observations = list(replay_observations or (replay, replay))
        negative = NegativeObservation(
            positive_target_state_digest="1" * 64,
            negative_target_state_digest="2" * 64,
            stage_state_digest="3" * 64,
            ledger_state_digest="4" * 64,
            postgres_source_data_read_count=1,
            snowflake_product_data_query_count=2,
            metadata_observation_count=0,
        )
        self.negative_observations = list(negative_observations or (negative, negative))
        self.reconciliation = reconciliation or ProviderResourceState(
            source_row_exists=True,
            target_row_exists=True,
            commit_ledger_entry_exists=True,
            staged_segment_exists=True,
        )
        self.reconciliation_error = reconciliation_error
        self.admission_assertions = 0
        self.fail_admission_assertion_at = fail_admission_assertion_at

    @contextmanager
    def admission(self, environment_identity: str) -> Any:
        assert len(environment_identity) == 64
        self.events.append("provider:admission-enter")
        try:
            yield self
        finally:
            self.events.append("provider:admission-exit")

    def assert_intact(self) -> None:
        self.admission_assertions += 1
        self.events.append("provider:admission-assert-intact")
        if self.admission_assertions == self.fail_admission_assertion_at:
            raise HarnessError("provider admission lock was lost")

    def preflight(self) -> DedicatedEnvironmentAttestation:
        self.calls += 1
        self.events.append("provider:preflight")
        assert self.environment_identity
        return _attestation(self.environment_identity)

    def prove_destination_absent(self, acceptance_key: int) -> None:
        assert acceptance_key == KEY
        self.events.append("provider:absence")

    def insert_fixture(self, row: Any) -> None:
        assert row.order_id == KEY
        self.events.append("provider:insert-fixture")
        if self.fail_insert is not None:
            raise self.fail_insert
        self.fixture_exists = True

    def replay_observation(self, acceptance_key: int, batch_id: str) -> ReplayObservation:
        assert acceptance_key == KEY
        assert batch_id == "batch-opaque"
        self.events.append("provider:replay-observation")
        return self.replay_observations.pop(0)

    def verify_destination(self, acceptance_key: int) -> VisibilityObservation:
        assert acceptance_key == KEY
        self.events.append("provider:independent-visibility")
        return VisibilityObservation(value_digest="a" * 64, query_id="query-opaque")

    def negative_observation(self) -> NegativeObservation:
        self.events.append("provider:negative-observation")
        observation = self.negative_observations.pop(0)
        return NegativeObservation(
            positive_target_state_digest=observation.positive_target_state_digest,
            negative_target_state_digest=observation.negative_target_state_digest,
            stage_state_digest=observation.stage_state_digest,
            ledger_state_digest=observation.ledger_state_digest,
            postgres_source_data_read_count=observation.postgres_source_data_read_count,
            snowflake_product_data_query_count=observation.snowflake_product_data_query_count,
            metadata_observation_count=observation.metadata_observation_count
            + (2 - len(self.negative_observations)),
        )

    def reconcile_resources(
        self, acceptance_key: int, batch_id: str | None
    ) -> ProviderResourceState:
        assert acceptance_key == KEY
        assert batch_id in {None, "batch-opaque", "batch-ambiguous"}
        self.events.append("provider:reconcile")
        if self.reconciliation_error is not None:
            raise self.reconciliation_error
        if batch_id is None:
            return ProviderResourceState(
                source_row_exists=self.fixture_exists,
                target_row_exists=None,
                commit_ledger_entry_exists=None,
                staged_segment_exists=None,
            )
        return self.reconciliation


class FakeCli:
    def __init__(self, events: list[str], *, fail_command: str | None = None) -> None:
        self.events = events
        self.fail_command = fail_command
        self.invocations: list[tuple[tuple[str, ...], str | None, Mapping[str, str]]] = []
        self.contracts: dict[str, dict[str, Any]] = {}

    def invoke(
        self,
        arguments: tuple[str, ...],
        *,
        stdin: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        child_environment = dict(environment or {})
        self.invocations.append((arguments, stdin, child_environment))
        command = arguments[0]
        self.events.append(f"cli:{command}")
        if command == self.fail_command:
            raise RuntimeError("injected CLI failure")
        if command == "create-draft":
            contract = json.loads(Path(arguments[1]).read_text(encoding="utf-8"))
            self.contracts[contract["contract_id"]] = contract
            negative = contract["destination"]["table"] == "ORDERS_UNSUPPORTED_KEY"
            return {
                "contract": contract,
                "contract_digest": ("d" if negative else "c") * 64,
            }
        if command == "verify":
            contract = self.contracts[arguments[1]]
            if contract["destination"]["table"] == "ORDERS_UNSUPPORTED_KEY":
                return {
                    "decision_digest": "f" * 64,
                    "legality_decision": {
                        "result": "no_valid_plan",
                        "rule_id": "M0-PG-SNAPSHOT-SNOWFLAKE-001",
                        "preconditions": [
                            {
                                "number": 6,
                                "status": "unsatisfied",
                                "reason": "destination key must preserve the source key",
                                "evidence_ids": [],
                            }
                        ],
                        "smallest_changes": ["destination key must preserve the source key"],
                        "execution_occurred": False,
                    },
                }
            return {"summary_digest": "e" * 64}
        if command == "activate-stdin":
            assert stdin == f"{KEY}\n"
            return {
                "run_id": "run-opaque",
                "state": "succeeded",
                "checkpoint": "succeeded",
                "batch_id": "batch-opaque",
            }
        if command == "get-trace":
            return [{"event_type": "terminal_success"}]
        if command == "export-evidence":
            Path(arguments[2]).mkdir(parents=True, mode=0o700)
            return {
                "package_index_digest": "b" * 64,
                "verification_result_digest": "9" * 64,
                "checks": [],
            }
        if command == "verify-evidence":
            return {
                "package_index_digest": "b" * 64,
                "verification_result_digest": "9" * 64,
                "checks": [],
            }
        raise AssertionError(arguments)


class FakeInspector:
    def __init__(self, progress: RunProgress | None = None) -> None:
        self.counter_values = StateCounters(
            runs=1,
            evidence_events=10,
            private_states=1,
            segment_files=1,
        )
        self.progress = progress

    def run_evidence(self, run_id: str) -> RunEvidence:
        assert run_id == "run-opaque"
        return RunEvidence(
            batch_id="batch-opaque",
            manifest_digest="1" * 64,
            acceptance_value_digest="a" * 64,
            visibility_value_digest="a" * 64,
        )

    def expected_run_resources(
        self, contract_digest: str, summary_digest: str, acceptance_key: int
    ) -> ExpectedRunResources:
        assert contract_digest == "c" * 64
        assert summary_digest == "e" * 64
        assert acceptance_key == KEY
        return ExpectedRunResources(run_id="run-opaque", batch_id="batch-opaque")

    def counters(self) -> StateCounters:
        return self.counter_values

    def latest_progress(self) -> RunProgress | None:
        return self.progress


def _harness(
    tmp_path: Path,
    *,
    providers: FakeProviders | None = None,
    fail_command: str | None = None,
    progress: RunProgress | None = None,
) -> tuple[AcceptanceHarness, FakeProviders, FakeCli, list[str]]:
    events: list[str] = []
    selected_providers = providers or FakeProviders(events)
    selected_providers.events = events
    cli = FakeCli(events, fail_command=fail_command)
    config = AcceptanceConfig.from_environment(
        _environment(tmp_path), repository_root=tmp_path / "repository"
    )
    selected_providers.environment_identity = config.environment_identity
    harness = AcceptanceHarness(
        config,
        selected_providers,
        cli,
        FakeInspector(progress),
        clock=lambda: NOW,
        acceptance_key_factory=lambda: KEY,
        token_factory=lambda: "0123456789abcdef01234567",
        revision_factory=lambda: ("1" * 40, "2" * 64, "3.13.7"),
    )
    return harness, selected_providers, cli, events


def test_preflight_aggregates_missing_names_before_constructing_providers(
    tmp_path: Path,
) -> None:
    calls = 0
    supplied_secret = "must-never-be-reported"

    def provider_factory(_config: AcceptanceConfig) -> FakeProviders:
        nonlocal calls
        calls += 1
        return FakeProviders([])

    with pytest.raises(HarnessError) as caught:
        preflight(
            {"PILLARMESH_POSTGRES_DSN": supplied_secret},
            repository_root=tmp_path,
            provider_factory=provider_factory,
        )

    message = str(caught.value)
    assert calls == 0
    assert "PILLARMESH_STATE_PATH" in message
    assert "PILLARMESH_SNOWFLAKE_PASSWORD" in message
    assert "PILLARMESH_POSTGRES_DSN" not in message
    assert supplied_secret not in message


def test_documented_preflight_command_fails_names_only_without_credentials(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        (
            sys.executable,
            str(repository_root / "tests" / "acceptance" / "run_m0.py"),
            "preflight",
        ),
        cwd=repository_root,
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("missing required variables: ")
    assert "PILLARMESH_POSTGRES_DSN" in result.stderr
    assert "PILLARMESH_SNOWFLAKE_PASSWORD" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "path_variable",
    [
        "PILLARMESH_STATE_PATH",
        "PILLARMESH_OUTPUT_DIR",
        "PILLARMESH_CLEANUP_LEDGER_PATH",
    ],
)
def test_preflight_refuses_repository_local_private_paths(
    tmp_path: Path, path_variable: str
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = _environment(tmp_path / "external")
    environment[path_variable] = str(repository / "private")

    with pytest.raises(HarnessError, match=path_variable):
        AcceptanceConfig.from_environment(environment, repository_root=repository)


def test_preflight_refuses_reused_state_and_nonempty_output(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    Path(environment["PILLARMESH_STATE_PATH"]).write_text("existing", encoding="utf-8")
    output = Path(environment["PILLARMESH_OUTPUT_DIR"])
    output.mkdir()
    (output / "existing").write_text("existing", encoding="utf-8")

    with pytest.raises(HarnessError) as caught:
        AcceptanceConfig.from_environment(environment, repository_root=tmp_path / "repository")

    assert "PILLARMESH_STATE_PATH" in str(caught.value)
    assert "PILLARMESH_OUTPUT_DIR" in str(caught.value)


@pytest.mark.parametrize(
    ("variable", "outside_value"),
    [
        ("PILLARMESH_POSTGRES_SCHEMA", "shared_public"),
        ("PILLARMESH_POSTGRES_TABLE", "shared_orders"),
        ("PILLARMESH_SNOWFLAKE_DATABASE", "SHARED_PRODUCTION"),
        ("PILLARMESH_SNOWFLAKE_SCHEMA", "PUBLIC"),
        ("PILLARMESH_SNOWFLAKE_TARGET_TABLE", "SHARED_ORDERS"),
        ("PILLARMESH_SNOWFLAKE_STAGE", "SHARED_STAGE"),
        ("PILLARMESH_SNOWFLAKE_LEDGER_TABLE", "SHARED_LEDGER"),
    ],
)
def test_preflight_refuses_objects_outside_fixed_dedicated_boundary(
    tmp_path: Path, variable: str, outside_value: str
) -> None:
    environment = _environment(tmp_path)
    environment[variable] = outside_value

    with pytest.raises(HarnessError, match="outside the dedicated acceptance boundary"):
        AcceptanceConfig.from_environment(environment, repository_root=tmp_path / "repository")


def test_run_refuses_an_existing_empty_output_directory(tmp_path: Path) -> None:
    Path(_environment(tmp_path)["PILLARMESH_OUTPUT_DIR"]).mkdir(parents=True)

    with pytest.raises(HarnessError, match="PILLARMESH_OUTPUT_DIR"):
        AcceptanceConfig.from_environment(
            _environment(tmp_path), repository_root=tmp_path / "repository"
        )


@pytest.mark.parametrize(
    ("variable", "other"),
    [
        ("PILLARMESH_POSTGRES_RUNTIME_PRINCIPAL", "PILLARMESH_POSTGRES_FIXTURE_PRINCIPAL"),
        ("PILLARMESH_POSTGRES_RUNTIME_PRINCIPAL", "PILLARMESH_POSTGRES_OWNER_PRINCIPAL"),
        ("PILLARMESH_SNOWFLAKE_USER", "PILLARMESH_SNOWFLAKE_OWNER_USER"),
    ],
)
def test_preflight_rejects_fixture_or_owner_runtime_principals(
    tmp_path: Path, variable: str, other: str
) -> None:
    environment = _environment(tmp_path)
    environment[variable] = environment[other]

    with pytest.raises(HarnessError, match="runtime principal is not isolated"):
        AcceptanceConfig.from_environment(environment, repository_root=tmp_path / "repository")


def test_preflight_rejects_fixture_principal_equal_to_declared_owner(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    environment["PILLARMESH_POSTGRES_FIXTURE_PRINCIPAL"] = environment[
        "PILLARMESH_POSTGRES_OWNER_PRINCIPAL"
    ]

    with pytest.raises(HarnessError, match="fixture principal is not isolated"):
        AcceptanceConfig.from_environment(environment, repository_root=tmp_path / "repository")


def test_preflight_rejects_connected_principal_mismatch(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    providers = FakeProviders([])
    config = AcceptanceConfig.from_environment(environment, repository_root=tmp_path / "repository")
    providers.environment_identity = config.environment_identity

    def mismatched() -> DedicatedEnvironmentAttestation:
        observation = _attestation(config.environment_identity)
        return DedicatedEnvironmentAttestation(
            **{
                **observation.__dict__,
                "identities": RuntimeIdentities(
                    postgres_runtime_session="fixture",
                    postgres_runtime_current="fixture",
                    postgres_fixture_session="fixture",
                    postgres_fixture_current="fixture",
                    snowflake_runtime="RUNTIME_ONE",
                ),
            }
        )

    providers.preflight = mismatched  # type: ignore[method-assign]

    with pytest.raises(HarnessError, match="connected runtime principal is not isolated"):
        preflight(
            environment,
            repository_root=tmp_path / "repository",
            provider_factory=lambda _config: providers,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("postgres_database", "production"),
        ("postgres_database_owner", "unexpected_owner"),
        ("postgres_schema_owner", "unexpected_owner"),
        ("postgres_marker_environment_id", "wrong-environment"),
        ("postgres_source_owner", "unexpected_owner"),
        ("postgres_audit_security_definer", False),
        ("postgres_audit_language", "plpgsql"),
        ("postgres_audit_volatility", "volatile"),
        ("postgres_audit_result", "integer"),
        ("postgres_audit_config", ("search_path=public",)),
        ("postgres_audit_body_digest", "0" * 64),
        ("postgres_denial_schema_exists", False),
        ("snowflake_account", "PRODUCTION"),
        ("snowflake_role", "ACCOUNTADMIN"),
        ("snowflake_user_roles", ("PILLARMESH_M0_RUNTIME", "ACCOUNTADMIN")),
        ("snowflake_marker_environment_identity", "wrong-environment"),
        ("snowflake_marker_owner_user", "WRONG_OWNER"),
        ("snowflake_marker_owner_role", "ACCOUNTADMIN"),
        ("snowflake_marker_denial_database", "ABSENT_PRIVATE"),
        ("snowflake_marker_denial_database_owner_role", "ACCOUNTADMIN"),
        ("snowflake_object_kinds", (("target", "VIEW"),)),
        ("snowflake_object_owners", (("target", "ACCOUNTADMIN"),)),
        (
            "snowflake_object_grants",
            (("target", "SELECT", "PILLARMESH_M0_RUNTIME"), ("target", "SELECT", "PUBLIC")),
        ),
        ("snowflake_runtime_grants", ("OWNERSHIP",)),
    ],
)
def test_preflight_rejects_non_dedicated_environment_attestation(
    tmp_path: Path, field: str, value: object
) -> None:
    environment = _environment(tmp_path)
    providers = FakeProviders([])
    config = AcceptanceConfig.from_environment(environment, repository_root=tmp_path / "repository")
    providers.environment_identity = config.environment_identity
    observation = _attestation(config.environment_identity)
    providers.preflight = lambda: DedicatedEnvironmentAttestation(  # type: ignore[method-assign]
        **{**observation.__dict__, field: value}
    )

    with pytest.raises(HarnessError, match="dedicated environment attestation failed"):
        preflight(
            environment,
            repository_root=tmp_path / "repository",
            provider_factory=lambda _config: providers,
        )


def test_snowflake_denial_probe_rejects_unrelated_query_errors() -> None:
    class ProviderError(Exception):
        def __init__(self, sqlstate: str) -> None:
            self.sqlstate = sqlstate

    assert snowflake_access_denied(ProviderError("42501")) is True
    assert snowflake_access_denied(ProviderError("42000")) is False


def test_contract_and_fixture_labels_are_opaque_and_independent_from_key(tmp_path: Path) -> None:
    config = AcceptanceConfig.from_environment(
        _environment(tmp_path), repository_root=tmp_path / "repository"
    )
    label = opaque_label("contract", lambda: "abcdef0123456789abcdef01")
    contract = build_contract(config, label)
    payload = json.dumps(contract, sort_keys=True)

    assert label == "contract-abcdef0123456789abcdef01"
    assert str(KEY) not in payload
    assert contract["contract_id"] == label
    assert contract["projection"] == [
        {"source": "order_id", "destination": "order_id"},
        {"source": "customer_ref", "destination": "customer_ref"},
        {"source": "amount", "destination": "amount"},
        {"source": "currency", "destination": "currency"},
        {"source": "status", "destination": "order_status"},
        {"source": "updated_at", "destination": "updated_at"},
    ]


def test_checked_in_contract_fixture_is_fixed_shape_and_contains_no_secret_placeholder() -> None:
    fixture_path = Path(__file__).parents[2] / "docs" / "m0" / "contract.example.json"

    fixture = IntegrationContract.model_validate_json(fixture_path.read_bytes())

    assert fixture.projection == FIXED_PROJECTION
    assert fixture.contract_id == "contract-opaque-example"
    assert "<secret>" not in fixture_path.read_text(encoding="utf-8")


def test_env_example_matches_exact_required_variable_inventory() -> None:
    example = Path(__file__).parents[2] / ".env.example"
    names = tuple(
        line.split("=", 1)[0]
        for line in example.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )

    assert len(names) == len(set(names))
    assert set(names) == set(REQUIRED_VARIABLES)


def test_acceptance_contract_has_exactly_the_approved_35_variable_names() -> None:
    assert REQUIRED_VARIABLES == (
        "PILLARMESH_STATE_PATH",
        "PILLARMESH_OUTPUT_DIR",
        "PILLARMESH_CLEANUP_LEDGER_PATH",
        "PILLARMESH_SIGNING_KEY_ID",
        "PILLARMESH_SIGNING_PRIVATE_KEY_B64",
        "PILLARMESH_POSTGRES_DSN",
        "PILLARMESH_POSTGRES_DATABASE",
        "PILLARMESH_POSTGRES_RUNTIME_PRINCIPAL",
        "PILLARMESH_POSTGRES_OWNER_PRINCIPAL",
        "PILLARMESH_POSTGRES_CONNECTION_HANDLE",
        "PILLARMESH_POSTGRES_SCHEMA",
        "PILLARMESH_POSTGRES_TABLE",
        "PILLARMESH_POSTGRES_DENIAL_SCHEMA",
        "PILLARMESH_POSTGRES_FIXTURE_DSN",
        "PILLARMESH_POSTGRES_FIXTURE_PRINCIPAL",
        "PILLARMESH_SNOWFLAKE_ACCOUNT",
        "PILLARMESH_SNOWFLAKE_USER",
        "PILLARMESH_SNOWFLAKE_PASSWORD",
        "PILLARMESH_SNOWFLAKE_OWNER_USER",
        "PILLARMESH_SNOWFLAKE_ROLE",
        "PILLARMESH_SNOWFLAKE_WAREHOUSE",
        "PILLARMESH_SNOWFLAKE_DATABASE",
        "PILLARMESH_SNOWFLAKE_SCHEMA",
        "PILLARMESH_SNOWFLAKE_STAGE",
        "PILLARMESH_SNOWFLAKE_TARGET_TABLE",
        "PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE",
        "PILLARMESH_SNOWFLAKE_LEDGER_TABLE",
        "PILLARMESH_SNOWFLAKE_CONNECTION_HANDLE",
        "PILLARMESH_SNOWFLAKE_DENIAL_DATABASE",
        "PILLARMESH_CREDENTIAL_CANARIES_JSON",
        "PILLARMESH_ROW_VALUE_CANARY",
        "PILLARMESH_OPERATOR_PSEUDONYM",
        "PILLARMESH_HOST_PSEUDONYM",
        "PILLARMESH_MCP_PROTOCOL_VERSION",
        "PILLARMESH_OWNER_AUTHORIZATION_REFERENCE",
    )


def test_environment_identity_ignores_operator_paths_but_changes_with_provider_boundary(
    tmp_path: Path,
) -> None:
    first_environment = _environment(tmp_path / "operator-one")
    second_environment = _environment(tmp_path / "operator-two")
    second_environment.update(
        {
            "PILLARMESH_POSTGRES_RUNTIME_PRINCIPAL": "runtime_two",
            "PILLARMESH_POSTGRES_CONNECTION_HANDLE": "pg-operator-two",
            "PILLARMESH_SNOWFLAKE_USER": "RUNTIME_TWO",
            "PILLARMESH_SNOWFLAKE_CONNECTION_HANDLE": "sf-operator-two",
            "PILLARMESH_OPERATOR_PSEUDONYM": "operator-two",
        }
    )
    first = AcceptanceConfig.from_environment(
        first_environment, repository_root=tmp_path / "repository"
    )
    second = AcceptanceConfig.from_environment(
        second_environment, repository_root=tmp_path / "repository"
    )

    assert first.environment_identity == second.environment_identity
    assert first.reservation_path == second.reservation_path

    case_only_environment = dict(second_environment)
    for name in (
        "PILLARMESH_SNOWFLAKE_ACCOUNT",
        "PILLARMESH_SNOWFLAKE_DATABASE",
        "PILLARMESH_SNOWFLAKE_SCHEMA",
        "PILLARMESH_SNOWFLAKE_STAGE",
        "PILLARMESH_SNOWFLAKE_TARGET_TABLE",
        "PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE",
        "PILLARMESH_SNOWFLAKE_LEDGER_TABLE",
    ):
        case_only_environment[name] = case_only_environment[name].lower()
    case_only = AcceptanceConfig.from_environment(
        case_only_environment, repository_root=tmp_path / "repository"
    )
    assert case_only.environment_identity == first.environment_identity
    assert case_only.reservation_path == first.reservation_path

    changed_environment = dict(second_environment)
    changed_environment["PILLARMESH_SNOWFLAKE_ACCOUNT"] = "other-dedicated-account"
    changed = AcceptanceConfig.from_environment(
        changed_environment, repository_root=tmp_path / "repository"
    )
    assert changed.environment_identity != first.environment_identity
    assert changed.reservation_path != first.reservation_path


def test_run_uses_stdin_activation_and_exact_replay(tmp_path: Path) -> None:
    harness, _providers, cli, _events = _harness(tmp_path)

    result = harness.run()

    activations = [item for item in cli.invocations if item[0][0] == "activate-stdin"]
    assert isinstance(result, AcceptanceResult)
    assert result.run_id == "run-opaque"
    assert len(activations) == 2
    assert activations[0] == activations[1]
    assert activations[0][0] == ("activate-stdin", "c" * 64, "e" * 64)
    assert activations[0][1] == f"{KEY}\n"
    assert str(KEY) not in " ".join(activations[0][0])
    assert all(
        "PILLARMESH_POSTGRES_FIXTURE_DSN" not in invocation[2]
        and "PILLARMESH_POSTGRES_OWNER_PRINCIPAL" not in invocation[2]
        for invocation in cli.invocations
    )


def test_run_keeps_attestation_details_only_in_private_ledger(tmp_path: Path) -> None:
    harness, _providers, _cli, _events = _harness(tmp_path)

    harness.run()

    environment = _environment(tmp_path)
    ledger = json.loads(
        Path(environment["PILLARMESH_CLEANUP_LEDGER_PATH"]).read_text(encoding="utf-8")
    )
    attestation = ledger["context"]["attestation"]
    assert attestation["status"] == "passed"
    assert attestation["declared"]["postgres_runtime_principal"] == "runtime_one"
    assert attestation["declared"]["snowflake_role"] == "PILLARMESH_M0_RUNTIME"
    assert attestation["observed"]["snowflake_account_locator"] == ("DEDICATED_ACCOUNT_LOCATOR")
    assert attestation["observed"]["postgres_fixture_grants"] == [
        "CONNECT_DATABASE",
        "DELETE_SOURCE",
        "INSERT_SOURCE",
        "SELECT_SOURCE_KEY_COLUMN",
        "USAGE_SCHEMA",
    ]

    private_attestation = json.dumps(attestation, sort_keys=True)
    for secret in (
        environment["PILLARMESH_POSTGRES_DSN"],
        environment["PILLARMESH_POSTGRES_FIXTURE_DSN"],
        environment["PILLARMESH_SNOWFLAKE_PASSWORD"],
        environment["PILLARMESH_SIGNING_PRIVATE_KEY_B64"],
        "credential-one",
        "credential-two",
        environment["PILLARMESH_ROW_VALUE_CANARY"],
    ):
        assert secret not in private_attestation

    output_dir = Path(environment["PILLARMESH_OUTPUT_DIR"])
    public_bytes = b"".join(
        path.read_bytes()
        for path in output_dir.rglob("*")
        if path.is_file() and path.name != "segment.csv"
    )
    for private_identifier in (
        "DEDICATED_ACCOUNT_LOCATOR",
        "PILLARMESH_M0_RUNTIME",
        "m0_owner",
        "pillarmesh_m0.orders",
    ):
        assert private_identifier.encode() not in public_bytes


def test_absence_precedes_fixture_and_contract_creation(tmp_path: Path) -> None:
    harness, _providers, _cli, events = _harness(tmp_path)

    harness.run()

    assert events.index("provider:absence") < events.index("provider:insert-fixture")
    assert events.index("provider:insert-fixture") < events.index("cli:create-draft")
    assert events.index("cli:activate-stdin") < events.index("provider:independent-visibility")
    assert events.index("provider:admission-enter") < events.index("provider:preflight")
    assert events.index("provider:admission-exit") > events.index("cli:export-evidence")


def test_run_reasserts_admission_before_each_mutation_capable_boundary(tmp_path: Path) -> None:
    harness, providers, _cli, events = _harness(tmp_path)

    harness.run()

    assertions = [
        index for index, event in enumerate(events) if event == "provider:admission-assert-intact"
    ]
    insert = events.index("provider:insert-fixture")
    activations = [index for index, event in enumerate(events) if event == "cli:activate-stdin"]
    assert providers.admission_assertions == 3
    assert assertions[0] < insert
    assert assertions[1] < activations[0]
    assert assertions[2] < activations[1]


@pytest.mark.parametrize(
    ("failed_assertion", "forbidden_event"),
    ((1, "provider:insert-fixture"), (2, "cli:activate-stdin")),
)
def test_lost_admission_fails_before_next_provider_mutation(
    tmp_path: Path,
    failed_assertion: int,
    forbidden_event: str,
) -> None:
    events: list[str] = []
    providers = FakeProviders(events, fail_admission_assertion_at=failed_assertion)
    harness, _providers, _cli, _events = _harness(tmp_path, providers=providers)

    with pytest.raises(HarnessError, match="provider admission lock was lost"):
        harness.run()

    assert forbidden_event not in events


def test_negative_plan_preserves_state_provider_counts_and_source_sessions(
    tmp_path: Path,
) -> None:
    harness, providers, cli, _events = _harness(tmp_path)

    harness.run()

    negative_verifies = [
        invocation
        for invocation in cli.invocations
        if invocation[0][0] == "verify" and invocation[0][1].startswith("negative-contract-")
    ]
    assert len(negative_verifies) == 1
    assert providers.events.count("provider:replay-observation") == 2
    assert providers.events.count("provider:negative-observation") == 2


def test_failed_run_persists_registered_private_resources(tmp_path: Path) -> None:
    events: list[str] = []
    providers = FakeProviders(events, fail_insert=RuntimeError("injected fixture failure"))
    harness, _providers, _cli, _events = _harness(tmp_path, providers=providers)

    with pytest.raises(RuntimeError, match="injected fixture failure"):
        harness.run()

    ledger_path = Path(_environment(tmp_path)["PILLARMESH_CLEANUP_LEDGER_PATH"])
    private = json.loads(ledger_path.read_text(encoding="utf-8"))
    source = next(item for item in private["resources"] if item["kind"] == "source_row")
    assert source["exact_identifier"].endswith(f"order_id={KEY}")
    assert source["creation_state"] == "not_created"
    assert private["run_state"] == "failed"


def test_failure_before_batch_identity_preserves_source_retention(
    tmp_path: Path,
) -> None:
    harness, _providers, _cli, _events = _harness(tmp_path, fail_command="create-draft")

    with pytest.raises(RuntimeError, match="injected CLI failure"):
        harness.run()

    private = json.loads(
        Path(_environment(tmp_path)["PILLARMESH_CLEANUP_LEDGER_PATH"]).read_text(encoding="utf-8")
    )
    source = next(item for item in private["resources"] if item["kind"] == "source_row")
    assert source["creation_state"] == "created"
    assert source["cleanup_status"] == "scheduled"
    assert source["retention_deadline"] == "2026-09-12T12:00:00Z"


def test_failure_after_terminal_activation_records_created_destination_resources(
    tmp_path: Path,
) -> None:
    harness, _providers, _cli, _events = _harness(tmp_path, fail_command="get-trace")

    with pytest.raises(RuntimeError, match="injected CLI failure"):
        harness.run()

    ledger_path = Path(_environment(tmp_path)["PILLARMESH_CLEANUP_LEDGER_PATH"])
    private = json.loads(ledger_path.read_text(encoding="utf-8"))
    by_kind = {item["kind"]: item for item in private["resources"]}
    assert by_kind["target_row"]["creation_state"] == "created"
    assert by_kind["commit_ledger_entry"]["creation_state"] == "created"
    assert by_kind["staged_segment"]["creation_state"] == "created"
    assert by_kind["staged_segment"]["cleanup_status"] == "quarantined"
    assert by_kind["staged_segment"]["retention_deadline"] == "2026-08-20T12:00:00Z"


def test_ambiguous_activation_failure_quarantines_recoverable_batch_resources(
    tmp_path: Path,
) -> None:
    harness, _providers, _cli, _events = _harness(
        tmp_path,
        fail_command="activate-stdin",
        progress=RunProgress(
            run_id="run-opaque",
            batch_id="batch-ambiguous",
            event_types=("extraction_completed", "manifest_created", "commit_attempted"),
        ),
    )

    with pytest.raises(RuntimeError, match="injected CLI failure"):
        harness.run()

    private = json.loads(
        Path(_environment(tmp_path)["PILLARMESH_CLEANUP_LEDGER_PATH"]).read_text(encoding="utf-8")
    )
    by_kind = {item["kind"]: item for item in private["resources"]}
    assert by_kind["staged_segment"]["cleanup_status"] == "quarantined"
    assert by_kind["target_row"]["creation_state"] == "created"
    assert by_kind["commit_ledger_entry"]["creation_state"] == "created"
    assert by_kind["source_row"]["retention_deadline"] == "2026-09-12T12:00:00Z"


@pytest.mark.parametrize(
    ("target_exists", "ledger_exists", "expected_deadline"),
    (
        (False, False, "2026-08-13T12:00:00Z"),
        (True, False, "2026-09-12T12:00:00Z"),
        (False, True, "2026-09-12T12:00:00Z"),
        (True, True, "2026-09-12T12:00:00Z"),
        (None, False, "2026-09-12T12:00:00Z"),
        (False, None, "2026-09-12T12:00:00Z"),
    ),
)
def test_precommit_source_cleanup_requires_definitive_provider_effect_absence(
    tmp_path: Path,
    target_exists: bool | None,
    ledger_exists: bool | None,
    expected_deadline: str,
) -> None:
    providers = FakeProviders(
        [],
        reconciliation=ProviderResourceState(
            source_row_exists=True,
            target_row_exists=target_exists,
            commit_ledger_entry_exists=ledger_exists,
            staged_segment_exists=False,
        ),
    )
    harness, _providers, _cli, _events = _harness(
        tmp_path,
        providers=providers,
        fail_command="activate-stdin",
        progress=RunProgress(
            run_id="run-opaque",
            batch_id="batch-ambiguous",
            event_types=("extraction_completed", "manifest_created"),
        ),
    )

    with pytest.raises(RuntimeError, match="injected CLI failure"):
        harness.run()

    private = json.loads(
        Path(_environment(tmp_path)["PILLARMESH_CLEANUP_LEDGER_PATH"]).read_text(encoding="utf-8")
    )
    by_kind = {item["kind"]: item for item in private["resources"]}
    source = by_kind["source_row"]
    assert source["creation_state"] == "created"
    assert source["retention_deadline"] == expected_deadline
    if target_exists is None:
        assert by_kind["target_row"]["creation_state"] == "indeterminate"
    if ledger_exists is None:
        assert by_kind["commit_ledger_entry"]["creation_state"] == "indeterminate"


def test_precommit_reconciliation_exception_preserves_source_retention(tmp_path: Path) -> None:
    providers = FakeProviders([], reconciliation_error=RuntimeError("reconciliation unavailable"))
    harness, _providers, _cli, _events = _harness(
        tmp_path,
        providers=providers,
        fail_command="activate-stdin",
        progress=RunProgress(
            run_id="run-opaque",
            batch_id="batch-ambiguous",
            event_types=("extraction_completed", "manifest_created"),
        ),
    )

    with pytest.raises(RuntimeError, match="injected CLI failure"):
        harness.run()

    private = json.loads(
        Path(_environment(tmp_path)["PILLARMESH_CLEANUP_LEDGER_PATH"]).read_text(encoding="utf-8")
    )
    source = next(item for item in private["resources"] if item["kind"] == "source_row")
    assert source["retention_deadline"] == "2026-09-12T12:00:00Z"


def test_cleanup_status_is_sanitized_read_only_and_non_destructive(tmp_path: Path) -> None:
    ledger_path = tmp_path / "private-ledger.json"
    ledger = PrivateResourceLedger(ledger_path, clock=lambda: NOW)
    ledger.register(
        kind="source_row",
        exact_identifier="postgresql:m0.orders:order_id=984201",
        retention_seconds=30 * 24 * 60 * 60,
        cleanup_operation="delete_synthetic_rows",
    )
    ledger.persist(run_state="failed")
    before = ledger_path.read_bytes()

    status = cleanup_status(ledger_path)

    assert ledger_path.read_bytes() == before
    assert status == (
        {
            "resource_digest": status[0]["resource_digest"],
            "kind": "source_row",
            "creation_state": "not_created",
            "cleanup_status": "scheduled",
            "cleanup_operation": "delete_synthetic_rows",
            "retention_deadline": "2026-09-12T12:00:00Z",
        },
    )
    assert len(status[0]["resource_digest"]) == 64
    assert "984201" not in json.dumps(status)


def test_failed_diagnostic_resource_can_be_completed_or_quarantined(tmp_path: Path) -> None:
    ledger = PrivateResourceLedger(tmp_path / "ledger.json", clock=lambda: NOW)
    local = ledger.register(
        kind="local_segment",
        exact_identifier="/private/segment.csv",
        retention_seconds=0,
        cleanup_operation="delete_local_state",
    )
    staged = ledger.register(
        kind="staged_segment",
        exact_identifier="snowflake-stage:opaque-private-identifier",
        retention_seconds=24 * 60 * 60,
        cleanup_operation="delete_staged_segments",
    )
    ledger.mark_created(local)
    ledger.mark_created(staged)

    ledger.mark_completed(local)
    ledger.mark_quarantined(staged)
    ledger.persist(run_state="failed")

    statuses = cleanup_status(ledger.path)
    by_kind = {item["kind"]: item for item in statuses}
    assert by_kind["local_segment"]["cleanup_status"] == "completed"
    assert by_kind["staged_segment"]["cleanup_status"] == "quarantined"
    assert by_kind["staged_segment"]["retention_deadline"] == "2026-08-20T12:00:00Z"


def test_resource_dispositions_expose_digests_without_provider_identifiers(
    tmp_path: Path,
) -> None:
    harness, _providers, cli, _events = _harness(tmp_path)

    harness.run()

    export = next(item for item in cli.invocations if item[0][0] == "export-evidence")
    metadata_payload = Path(export[0][3]).read_bytes()
    # The fake CLI accepts anything, so the metadata this harness writes must be checked
    # against the model the real `export-evidence` parses it with -- otherwise a schema
    # mismatch only ever surfaces during a live acceptance run.
    PackageMetadata.model_validate_json(metadata_payload)
    metadata = json.loads(metadata_payload.decode("utf-8"))
    serialized = json.dumps(metadata["resources"], sort_keys=True)
    private = json.loads(
        Path(_environment(tmp_path)["PILLARMESH_CLEANUP_LEDGER_PATH"]).read_text(encoding="utf-8")
    )
    package_digest = next(
        item["resource_digest"]
        for item in private["resources"]
        if item["kind"] == "evidence_package"
    )
    assert metadata["resources"]
    assert package_digest in {item["resource_digest"] for item in metadata["resources"]}
    assert all(len(item["resource_digest"]) == 64 for item in metadata["resources"])
    assert "PILLARMESH_M0" not in serialized
    assert "pillarmesh_m0" not in serialized
    assert str(KEY) not in serialized


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("target_value_digest", "b" * 64),
        ("ledger_manifest_digest", "c" * 64),
        ("ledger_committed_identity", "d" * 64),
        ("stage_state_digest", "e" * 64),
        ("product_mutation_count", 2),
    ],
)
def test_replay_rejects_row_ledger_or_update_effect_changes(
    tmp_path: Path, field: str, changed: object
) -> None:
    baseline = ReplayObservation(
        target_rows=1,
        target_value_digest="a" * 64,
        ledger_rows=1,
        ledger_manifest_digest="1" * 64,
        ledger_committed_identity="8" * 64,
        stage_state_digest="5" * 64,
        product_mutation_count=1,
    )
    after = ReplayObservation(**{**baseline.__dict__, field: changed})
    providers = FakeProviders([], replay_observations=(baseline, after))
    harness, _providers, _cli, _events = _harness(tmp_path, providers=providers)

    with pytest.raises(HarnessError, match="exact activation replay changed destination state"):
        harness.run()


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("positive_target_state_digest", "0" * 64),
        ("negative_target_state_digest", "5" * 64),
        ("stage_state_digest", "6" * 64),
        ("ledger_state_digest", "7" * 64),
        ("postgres_source_data_read_count", 2),
        ("snowflake_product_data_query_count", 3),
    ],
)
def test_negative_gate_rejects_mutation_or_transient_data_read_evidence(
    tmp_path: Path, field: str, changed: object
) -> None:
    before = NegativeObservation(
        positive_target_state_digest="1" * 64,
        negative_target_state_digest="2" * 64,
        stage_state_digest="3" * 64,
        ledger_state_digest="4" * 64,
        postgres_source_data_read_count=1,
        snowflake_product_data_query_count=2,
        metadata_observation_count=10,
    )
    after = NegativeObservation(**{**before.__dict__, field: changed})
    providers = FakeProviders([], negative_observations=(before, after))
    harness, _providers, _cli, _events = _harness(tmp_path, providers=providers)

    with pytest.raises(HarnessError, match="negative plan created a data read or provider effect"):
        harness.run()


def test_negative_gate_allows_required_metadata_observation(tmp_path: Path) -> None:
    before = NegativeObservation(
        positive_target_state_digest="1" * 64,
        negative_target_state_digest="2" * 64,
        stage_state_digest="3" * 64,
        ledger_state_digest="4" * 64,
        postgres_source_data_read_count=1,
        snowflake_product_data_query_count=2,
        metadata_observation_count=10,
    )
    after = NegativeObservation(**{**before.__dict__, "metadata_observation_count": 14})
    providers = FakeProviders([], negative_observations=(before, after))
    harness, _providers, _cli, _events = _harness(tmp_path, providers=providers)

    assert harness.run().run_id == "run-opaque"


def test_run_reservation_is_exclusive_and_detects_output_symlink_replacement(
    tmp_path: Path,
) -> None:
    config = AcceptanceConfig.from_environment(
        _environment(tmp_path), repository_root=tmp_path / "repository"
    )
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)

    with RunReservation(config) as first:
        with pytest.raises(HarnessError, match="already reserved"), RunReservation(config):
            pass
        config.output_dir.rmdir()
        config.output_dir.symlink_to(replacement, target_is_directory=True)
        with pytest.raises(HarnessError, match="private path changed during run"):
            first.assert_intact()


def test_run_reservation_exclusively_creates_and_retains_state_and_ledger(
    tmp_path: Path,
) -> None:
    config = AcceptanceConfig.from_environment(
        _environment(tmp_path), repository_root=tmp_path / "repository"
    )

    with RunReservation(config) as reservation:
        assert config.state_path.is_file()
        assert config.cleanup_ledger_path.is_file()
        assert stat.S_IMODE(config.state_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(config.cleanup_ledger_path.stat().st_mode) == 0o600
        config.state_path.unlink()
        config.state_path.write_bytes(b"replacement")
        config.state_path.chmod(0o600)
        with pytest.raises(HarnessError, match="private path changed during run"):
            reservation.assert_intact()


def test_reservation_serializes_distinct_operator_paths_for_same_environment(
    tmp_path: Path,
) -> None:
    (tmp_path / "operator-one").mkdir(mode=0o700)
    (tmp_path / "operator-two").mkdir(mode=0o700)
    first_environment = _environment(tmp_path / "operator-one")
    second_environment = _environment(tmp_path / "operator-two")
    second_environment["PILLARMESH_CLEANUP_LEDGER_PATH"] = str(
        tmp_path / "operator-two" / "second-ledger.json"
    )
    first = AcceptanceConfig.from_environment(
        first_environment, repository_root=tmp_path / "repository"
    )
    second = AcceptanceConfig.from_environment(
        second_environment, repository_root=tmp_path / "repository"
    )
    assert first.reservation_path == second.reservation_path

    with (
        RunReservation(first),
        pytest.raises(HarnessError, match="already reserved"),
        RunReservation(second),
    ):
        pass


def test_reservation_does_not_unlink_a_replacement_lock(tmp_path: Path) -> None:
    config = AcceptanceConfig.from_environment(
        _environment(tmp_path), repository_root=tmp_path / "repository"
    )

    with (
        pytest.raises(HarnessError, match="reservation changed"),
        RunReservation(config) as reservation,
    ):
        config.reservation_path.unlink()
        config.reservation_path.write_text("replacement", encoding="utf-8")
        config.reservation_path.chmod(0o600)
        reservation.assert_intact()

    assert config.reservation_path.read_text(encoding="utf-8") == "replacement"
    config.reservation_path.unlink()


def test_config_rejects_private_path_through_symlink(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    environment = _environment(tmp_path)
    environment["PILLARMESH_STATE_PATH"] = str(linked / "state.db")

    with pytest.raises(HarnessError, match="PILLARMESH_STATE_PATH"):
        AcceptanceConfig.from_environment(environment, repository_root=tmp_path / "repository")


def test_private_ledger_is_atomic_owner_only_and_refuses_symlink_destination(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    ledger_path = private / "ledger.json"
    ledger = PrivateResourceLedger(ledger_path, clock=lambda: NOW)
    ledger.persist(run_state="failed")
    first = ledger_path.read_bytes()
    ledger.set_context(attempt=2)
    ledger.persist(run_state="failed")

    assert ledger_path.read_bytes() != first
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600
    replacement_identity = atomic_private_replace(ledger_path, ledger_path.read_bytes())
    assert replacement_identity == (ledger_path.stat().st_dev, ledger_path.stat().st_ino)
    assert not tuple(private.glob(".ledger.json.*.tmp"))

    ledger_path.unlink()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    ledger_path.symlink_to(sentinel)
    with pytest.raises(HarnessError, match="private cleanup ledger"):
        ledger.persist(run_state="failed")
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize("unsafe", ["repo", "symlink", "mode"])
def test_cleanup_status_rejects_unsafe_private_ledger(tmp_path: Path, unsafe: str) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    private = repository if unsafe == "repo" else tmp_path / "private"
    if private != repository:
        private.mkdir(mode=0o700)
    ledger_path = private / "ledger.json"
    ledger = PrivateResourceLedger(ledger_path, clock=lambda: NOW)
    ledger.persist(run_state="failed")
    selected = ledger_path
    if unsafe == "symlink":
        selected = tmp_path / "ledger-link.json"
        selected.symlink_to(ledger_path)
    elif unsafe == "mode":
        ledger_path.chmod(0o644)

    with pytest.raises(HarnessError, match="private cleanup ledger"):
        cleanup_status(selected, repository_root=repository)


def _write_subprocess_helper(path: Path) -> None:
    path.write_text(
        "import json, os, sys, time\n"
        "command = sys.argv[1]\n"
        "if command == 'environment': print(json.dumps(dict(os.environ)))\n"
        "elif command == 'sleep': time.sleep(2); print('{}')\n"
        "else: print(json.dumps({'command': command}))\n",
        encoding="utf-8",
    )


def test_real_subprocess_cli_applies_exact_environment_allowlist(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    _write_subprocess_helper(helper)
    cli = SubprocessCli(
        repository_root=tmp_path,
        command_prefix=(sys.executable, str(helper)),
        timeout_seconds=1,
    )

    result = cli.invoke(
        ("environment",),
        environment={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "AWS_SECRET_ACCESS_KEY": "must-not-pass",
            "LEAK_ME": "must-not-pass",
            "PILLARMESH_POSTGRES_DSN": "allowed-product-value",
        },
    )

    assert isinstance(result, dict)
    assert result["PATH"] == os.environ["PATH"]
    assert result["PILLARMESH_POSTGRES_DSN"] == "allowed-product-value"
    assert "AWS_SECRET_ACCESS_KEY" not in result
    assert "LEAK_ME" not in result


def test_real_subprocess_cli_times_out_without_echoing_values(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    _write_subprocess_helper(helper)
    cli = SubprocessCli(
        repository_root=tmp_path,
        command_prefix=(sys.executable, str(helper)),
        timeout_seconds=0.01,
    )

    with pytest.raises(CliTimeout, match="sleep") as caught:
        cli.invoke(("sleep",), environment={"PATH": os.environ["PATH"]})

    assert "PATH" not in str(caught.value)


def test_real_subprocess_timeout_quarantines_pessimistically_registered_resources(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "acceptance-helper.py"
    helper.write_text(
        "import json, sys, time\n"
        "command = sys.argv[1]\n"
        "if command == 'create-draft': print(json.dumps({'contract_digest': 'c' * 64}))\n"
        "elif command == 'verify': print(json.dumps({'summary_digest': 'e' * 64}))\n"
        "elif command == 'activate-stdin': time.sleep(2); print('{}')\n"
        "else: print('{}')\n",
        encoding="utf-8",
    )
    config = AcceptanceConfig.from_environment(
        _environment(tmp_path), repository_root=tmp_path / "repository"
    )
    providers = FakeProviders([])
    providers.environment_identity = config.environment_identity
    harness = AcceptanceHarness(
        config,
        providers,
        SubprocessCli(
            repository_root=tmp_path,
            command_prefix=(sys.executable, str(helper)),
            timeout_seconds=0.5,
        ),
        FakeInspector(),
        clock=lambda: NOW,
        acceptance_key_factory=lambda: KEY,
        token_factory=lambda: "0123456789abcdef01234567",
        revision_factory=lambda: ("1" * 40, "2" * 64, "3.13.7"),
    )

    with pytest.raises(CliTimeout, match="activate-stdin"):
        harness.run()

    private = json.loads(
        Path(_environment(tmp_path)["PILLARMESH_CLEANUP_LEDGER_PATH"]).read_text(encoding="utf-8")
    )
    by_kind = {item["kind"]: item for item in private["resources"]}
    assert by_kind["target_row"]["creation_state"] == "created"
    assert by_kind["commit_ledger_entry"]["creation_state"] == "created"
    assert by_kind["staged_segment"]["cleanup_status"] == "quarantined"


def test_base_exception_persists_pessimistic_resource_ledger(tmp_path: Path) -> None:
    class StopNow(BaseException):
        pass

    providers = FakeProviders([])

    def ambiguous_insert(_row: object) -> None:
        providers.fixture_exists = True
        raise StopNow()

    providers.insert_fixture = ambiguous_insert  # type: ignore[method-assign]
    harness, _providers, _cli, _events = _harness(tmp_path, providers=providers)

    with pytest.raises(StopNow):
        harness.run()

    private = json.loads(
        Path(_environment(tmp_path)["PILLARMESH_CLEANUP_LEDGER_PATH"]).read_text(encoding="utf-8")
    )
    source = next(item for item in private["resources"] if item["kind"] == "source_row")
    assert source["creation_state"] == "created"
    assert source["cleanup_status"] == "scheduled"
    assert source["retention_deadline"] == "2026-09-12T12:00:00Z"
    assert private["run_state"] == "failed"


def test_cli_reports_a_note_attached_to_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(*_arguments: object, **_keywords: object) -> object:
        error = HarnessError("private cleanup ledger is invalid")
        error.add_note("provider admission release also failed: OperationalError [57P01]")
        raise error

    monkeypatch.setattr(run_m0, "cleanup_status", failing)
    monkeypatch.setenv("PILLARMESH_CLEANUP_LEDGER_PATH", str(tmp_path / "ledger.json"))

    assert run_m0.main(["cleanup-status"]) == 2

    reported = capsys.readouterr().err
    assert "private cleanup ledger is invalid" in reported
    # Without this the note reaches pytest and nobody else: the operator would see
    # the primary failure and never learn the advisory lock was not released.
    assert "provider admission release also failed: OperationalError [57P01]" in reported
