from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from pillarmesh_contract_model import FIXED_PROJECTION, digest
from pillarmesh_provider_sdk import SegmentManifest, VisibilityProof

from .config import AcceptanceConfig, HarnessError
from .private_files import RunReservation, write_private_file
from .provider_adapter import (
    FixtureRow,
    ProviderActions,
    ProviderResourceState,
    private_attestation_record,
    validate_attestation,
)
from .resource_ledger import ONE_DAY, THIRTY_DAYS, PrivateResourceLedger, utc_text


@dataclass(frozen=True)
class RunEvidence:
    batch_id: str
    manifest_digest: str
    acceptance_value_digest: str
    visibility_value_digest: str


@dataclass(frozen=True)
class StateCounters:
    runs: int
    evidence_events: int
    private_states: int
    segment_files: int


@dataclass(frozen=True)
class RunProgress:
    run_id: str
    batch_id: str | None
    event_types: tuple[str, ...]
    segment_path: Path | None = None


@dataclass(frozen=True)
class ExpectedRunResources:
    run_id: str
    batch_id: str


@dataclass(frozen=True)
class AcceptanceResult:
    run_id: str
    package_index_digest: str
    verification_result_digest: str
    cleanup_ledger_path: Path


class CliInvoker(Protocol):
    def invoke(
        self,
        arguments: tuple[str, ...],
        *,
        stdin: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> object: ...


class RunInspector(Protocol):
    def run_evidence(self, run_id: str) -> RunEvidence: ...

    def expected_run_resources(
        self, contract_digest: str, summary_digest: str, acceptance_key: int
    ) -> ExpectedRunResources: ...

    def counters(self) -> StateCounters: ...

    def latest_progress(self) -> RunProgress | None: ...


def opaque_label(
    prefix: str, token_factory: Callable[[], str] = lambda: secrets.token_hex(12)
) -> str:
    token = token_factory()
    if re.fullmatch(r"[0-9a-f]{24}", token) is None:
        raise HarnessError("opaque label token is invalid")
    return f"{prefix}-{token}"


def build_contract(
    config: AcceptanceConfig,
    contract_id: str,
    *,
    negative: bool = False,
) -> dict[str, object]:
    environment = config.environment
    target = (
        environment["PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE"]
        if negative
        else environment["PILLARMESH_SNOWFLAKE_TARGET_TABLE"]
    )
    return {
        "schema_version": "1",
        "contract_id": contract_id,
        "version": 1,
        "source": {
            "connection_handle": environment["PILLARMESH_POSTGRES_CONNECTION_HANDLE"],
            "schema": environment["PILLARMESH_POSTGRES_SCHEMA"],
            "table": environment["PILLARMESH_POSTGRES_TABLE"],
            "primary_key": "order_id",
        },
        "destination": {
            "connection_handle": environment["PILLARMESH_SNOWFLAKE_CONNECTION_HANDLE"],
            "database": environment["PILLARMESH_SNOWFLAKE_DATABASE"],
            "schema": environment["PILLARMESH_SNOWFLAKE_SCHEMA"],
            "table": target,
            "key": "order_id",
        },
        "projection": [item.model_dump() for item in FIXED_PROJECTION],
        "materialization_mode": "snapshot",
        "commit_behavior": "idempotent_key_upsert",
        "deletion_behavior": "not_observed",
        "freshness_seconds": 300,
        "data_classification": "synthetic_non_sensitive",
        "evidence_retention": "m0_30_days",
        "producer": "pillarmesh-contract-service",
    }


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise HarnessError(f"{context} returned an invalid response")
    return cast(Mapping[str, Any], value)


class AcceptanceHarness:
    def __init__(
        self,
        config: AcceptanceConfig,
        providers: ProviderActions,
        cli: CliInvoker,
        inspector: RunInspector,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        acceptance_key_factory: Callable[[], int] = lambda: secrets.randbelow(2**62) + 1,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(12),
        revision_factory: Callable[[], tuple[str, str, str]],
    ) -> None:
        self._config = config
        self._providers = providers
        self._cli = cli
        self._inspector = inspector
        self._clock = clock
        self._acceptance_key_factory = acceptance_key_factory
        self._token_factory = token_factory
        self._revision_factory = revision_factory
        self._reservation: RunReservation | None = None

    def _invoke_object(
        self,
        arguments: tuple[str, ...],
        *,
        stdin: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> object:
        if self._reservation is None:
            raise HarnessError("acceptance reservation is not active")
        self._reservation.assert_intact()
        result = self._cli.invoke(
            arguments,
            stdin=stdin,
            environment=environment or self._config.child_environment(),
        )
        self._reservation.assert_intact()
        return result

    def _invoke(
        self,
        arguments: tuple[str, ...],
        *,
        stdin: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        return _mapping(
            self._invoke_object(arguments, stdin=stdin, environment=environment),
            arguments[0],
        )

    def _write_contract(self, name: str, contract: Mapping[str, object]) -> Path:
        path = self._config.output_dir / name
        write_private_file(
            path,
            (json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        return path

    def _register_provider_resources(
        self,
        ledger: PrivateResourceLedger,
        acceptance_key: int,
        batch_id: str,
    ) -> dict[str, str]:
        environment = self._config.environment
        values = {
            "source_row": (
                "postgresql:"
                f"{environment['PILLARMESH_POSTGRES_CONNECTION_HANDLE']}:"
                f"{environment['PILLARMESH_POSTGRES_DATABASE']}."
                f"{environment['PILLARMESH_POSTGRES_SCHEMA']}."
                f"{environment['PILLARMESH_POSTGRES_TABLE']}:order_id={acceptance_key}"
            ),
            "target_row": (
                "snowflake:"
                f"{environment['PILLARMESH_SNOWFLAKE_CONNECTION_HANDLE']}:"
                f"{environment['PILLARMESH_SNOWFLAKE_DATABASE']}."
                f"{environment['PILLARMESH_SNOWFLAKE_SCHEMA']}."
                f"{environment['PILLARMESH_SNOWFLAKE_TARGET_TABLE']}:order_id={acceptance_key}"
            ),
            "staged_segment": (
                "snowflake-stage:"
                f"{environment['PILLARMESH_SNOWFLAKE_CONNECTION_HANDLE']}:"
                f"{environment['PILLARMESH_SNOWFLAKE_DATABASE']}."
                f"{environment['PILLARMESH_SNOWFLAKE_SCHEMA']}."
                f"{environment['PILLARMESH_SNOWFLAKE_STAGE']}/runs/{batch_id}"
            ),
            "commit_ledger_entry": (
                "snowflake-ledger:"
                f"{environment['PILLARMESH_SNOWFLAKE_CONNECTION_HANDLE']}:"
                f"{environment['PILLARMESH_SNOWFLAKE_DATABASE']}."
                f"{environment['PILLARMESH_SNOWFLAKE_SCHEMA']}."
                f"{environment['PILLARMESH_SNOWFLAKE_LEDGER_TABLE']}:batch_id={batch_id}"
            ),
        }
        return {
            "source_row": ledger.register(
                kind="source_row",
                exact_identifier=values["source_row"],
                retention_seconds=THIRTY_DAYS,
                cleanup_operation="delete_synthetic_rows",
            ),
            "target_row": ledger.register(
                kind="target_row",
                exact_identifier=values["target_row"],
                retention_seconds=THIRTY_DAYS,
                cleanup_operation="delete_synthetic_rows",
            ),
            "staged_segment": ledger.register(
                kind="staged_segment",
                exact_identifier=values["staged_segment"],
                retention_seconds=ONE_DAY,
                cleanup_operation="delete_staged_segments",
            ),
            "commit_ledger_entry": ledger.register(
                kind="commit_ledger_entry",
                exact_identifier=values["commit_ledger_entry"],
                retention_seconds=THIRTY_DAYS,
                cleanup_operation="delete_synthetic_rows",
            ),
        }

    def _register_source_resource(self, ledger: PrivateResourceLedger, acceptance_key: int) -> str:
        environment = self._config.environment
        return ledger.register(
            kind="source_row",
            exact_identifier=(
                "postgresql:"
                f"{environment['PILLARMESH_POSTGRES_CONNECTION_HANDLE']}:"
                f"{environment['PILLARMESH_POSTGRES_DATABASE']}."
                f"{environment['PILLARMESH_POSTGRES_SCHEMA']}."
                f"{environment['PILLARMESH_POSTGRES_TABLE']}:order_id={acceptance_key}"
            ),
            retention_seconds=THIRTY_DAYS,
            cleanup_operation="delete_synthetic_rows",
        )

    @staticmethod
    def _apply_reconciliation(
        ledger: PrivateResourceLedger,
        resources: Mapping[str, str],
        observation: ProviderResourceState,
        *,
        failed: bool,
    ) -> None:
        values = {
            "source_row": observation.source_row_exists,
            "target_row": observation.target_row_exists,
            "commit_ledger_entry": observation.commit_ledger_entry_exists,
            "staged_segment": observation.staged_segment_exists,
        }
        for kind, present in values.items():
            resource = resources.get(kind)
            if resource is None:
                continue
            if present is True:
                ledger.mark_created(resource)
            elif present is False:
                ledger.mark_not_created(resource)
        stage = resources.get("staged_segment")
        if stage is not None and observation.staged_segment_exists:
            if failed:
                ledger.mark_quarantined(stage)
            else:
                ledger.mark_scheduled(stage, retention_seconds=ONE_DAY)

    def _record_recoverable_resources(
        self,
        ledger: PrivateResourceLedger,
        acceptance_key: int | None,
        expected: ExpectedRunResources | None,
        resources: dict[str, str],
        *,
        activation_started: bool,
    ) -> None:
        commit_attempt_absent = not activation_started
        source = resources.get("source_row")
        try:
            progress = self._inspector.latest_progress()
        except BaseException:
            progress = None
            ledger.set_context(progress_reconciliation="unavailable")
        if progress is not None:
            commit_attempt_absent = "commit_attempted" not in progress.event_types
        if progress is not None and progress.segment_path is not None:
            local = ledger.register(
                kind="local_segment",
                exact_identifier=str(progress.segment_path),
                retention_seconds=0,
                cleanup_operation="delete_local_state",
            )
            ledger.mark_attempted(local)
        batch_id = expected.batch_id if expected is not None else None
        if batch_id is None and progress is not None:
            batch_id = progress.batch_id
        if acceptance_key is None:
            return
        if batch_id is not None:
            resources.update(self._register_provider_resources(ledger, acceptance_key, batch_id))
            for kind in ("target_row", "staged_segment", "commit_ledger_entry"):
                ledger.mark_attempted(resources[kind])
            ledger.mark_quarantined(resources["staged_segment"])
        observation: ProviderResourceState | None = None
        try:
            observation = self._providers.reconcile_resources(acceptance_key, batch_id)
        except BaseException:
            ledger.set_context(provider_reconciliation="unavailable")
        else:
            self._apply_reconciliation(ledger, resources, observation, failed=True)
        provider_effects_absent = (
            observation is not None
            and observation.target_row_exists is False
            and observation.commit_ledger_entry_exists is False
        )
        if source is not None and commit_attempt_absent and provider_effects_absent:
            ledger.mark_scheduled(source, retention_seconds=0)

    def run(self) -> AcceptanceResult:
        run_state = "failed"
        acceptance_key: int | None = None
        expected: ExpectedRunResources | None = None
        resources: dict[str, str] = {}
        activation_started = False
        with ExitStack() as stack:
            reservation = stack.enter_context(RunReservation(self._config))
            admission = stack.enter_context(
                self._providers.admission(self._config.environment_identity)
            )
            self._reservation = reservation
            ledger = PrivateResourceLedger(
                self._config.cleanup_ledger_path,
                clock=self._clock,
                writer=reservation.write_ledger,
            )
            ledger.set_context(
                owner_authorization_reference=self._config.environment[
                    "PILLARMESH_OWNER_AUTHORIZATION_REFERENCE"
                ],
                environment_identity=self._config.environment_identity,
            )
            resources["local_output"] = ledger.register(
                kind="local_output",
                exact_identifier=str(self._config.output_dir),
                retention_seconds=THIRTY_DAYS,
                cleanup_operation="delete_local_state",
            )
            resources["local_state"] = ledger.register(
                kind="local_state",
                exact_identifier=str(self._config.state_path),
                retention_seconds=THIRTY_DAYS,
                cleanup_operation="delete_local_state",
            )
            ledger.mark_created(resources["local_output"])
            ledger.mark_created(resources["local_state"])
            ledger.persist(run_state="running")
            primary_error: BaseException | None = None
            try:
                attestation = self._providers.preflight()
                validate_attestation(self._config, attestation)
                ledger.set_context(
                    attestation=private_attestation_record(self._config, attestation)
                )
                ledger.persist(run_state="running")
                acceptance_key = self._acceptance_key_factory()
                if acceptance_key <= 0:
                    raise HarnessError("acceptance key generator returned an invalid value")
                ledger.set_context(
                    acceptance_key=acceptance_key,
                    started_at=utc_text(self._clock()),
                )
                self._providers.prove_destination_absent(acceptance_key)

                resources["source_row"] = self._register_source_resource(ledger, acceptance_key)
                ledger.mark_attempted(resources["source_row"])
                fixture_label = opaque_label("fixture", self._token_factory)
                ledger.set_context(fixture_label=fixture_label)
                ledger.persist(run_state="running")
                environment = self._config.environment
                admission.assert_intact()
                self._providers.insert_fixture(
                    FixtureRow(
                        order_id=acceptance_key,
                        customer_ref=environment["PILLARMESH_ROW_VALUE_CANARY"],
                        amount=Decimal("10.50"),
                        currency="USD",
                        status="acceptance",
                        updated_at=self._clock(),
                    )
                )
                ledger.mark_created(resources["source_row"])

                contract_id = opaque_label("contract", self._token_factory)
                contract_path = self._write_contract(
                    "contract.json", build_contract(self._config, contract_id)
                )
                ledger.persist(run_state="running")
                created = self._invoke(("create-draft", str(contract_path)))
                contract_digest = str(created.get("contract_digest", ""))
                if re.fullmatch(r"[0-9a-f]{64}", contract_digest) is None:
                    raise HarnessError("create-draft returned an invalid response")
                verified = self._invoke(("verify", contract_id, "1"))
                summary_digest = str(verified.get("summary_digest", ""))
                if re.fullmatch(r"[0-9a-f]{64}", summary_digest) is None:
                    raise HarnessError("verification did not admit the fixed contract")

                expected = self._inspector.expected_run_resources(
                    contract_digest, summary_digest, acceptance_key
                )
                resources.update(
                    self._register_provider_resources(ledger, acceptance_key, expected.batch_id)
                )
                for kind in ("target_row", "staged_segment", "commit_ledger_entry"):
                    ledger.mark_attempted(resources[kind])
                ledger.mark_quarantined(resources["staged_segment"])
                ledger.persist(run_state="running")

                activation = ("activate-stdin", contract_digest, summary_digest)
                private_stdin = f"{acceptance_key}\n"
                activation_started = True
                admission.assert_intact()
                first_run = self._invoke(activation, stdin=private_stdin)
                run_id = str(first_run.get("run_id", ""))
                if first_run.get("state") != "succeeded" or run_id != expected.run_id:
                    raise HarnessError("activation did not reach predicted terminal success")
                evidence = self._inspector.run_evidence(run_id)
                if evidence.batch_id != expected.batch_id:
                    raise HarnessError("activation returned an unexpected batch identity")
                reconciled = self._providers.reconcile_resources(acceptance_key, expected.batch_id)
                self._apply_reconciliation(ledger, resources, reconciled, failed=False)
                if not (
                    reconciled.target_row_exists
                    and reconciled.commit_ledger_entry_exists
                    and reconciled.staged_segment_exists
                ):
                    raise HarnessError("terminal provider resources are incomplete")
                trace = self._invoke_object(("get-trace", run_id))
                if (
                    not isinstance(trace, list)
                    or not trace
                    or not isinstance(trace[-1], dict)
                    or trace[-1].get("event_type") != "terminal_success"
                ):
                    raise HarnessError("terminal trace is missing")
                independent = self._providers.verify_destination(acceptance_key)
                if (
                    independent.value_digest != evidence.acceptance_value_digest
                    or evidence.visibility_value_digest != evidence.acceptance_value_digest
                ):
                    raise HarnessError("independent visibility does not match run evidence")

                replay_before = self._providers.replay_observation(
                    acceptance_key, evidence.batch_id
                )
                if (
                    replay_before.target_rows != 1
                    or replay_before.ledger_rows != 1
                    or replay_before.target_value_digest != evidence.acceptance_value_digest
                    or replay_before.ledger_manifest_digest != evidence.manifest_digest
                    or re.fullmatch(r"[0-9a-f]{64}", replay_before.ledger_committed_identity)
                    is None
                ):
                    raise HarnessError("replay baseline does not match terminal evidence")
                admission.assert_intact()
                replay = self._invoke(activation, stdin=private_stdin)
                replay_after = self._providers.replay_observation(acceptance_key, evidence.batch_id)
                if replay.get("run_id") != run_id or replay.get("state") != "succeeded":
                    raise HarnessError("exact activation replay did not return the terminal run")
                if replay_after != replay_before:
                    raise HarnessError("exact activation replay changed destination state")

                state_before = self._inspector.counters()
                negative_before = self._providers.negative_observation()
                negative_id = opaque_label("negative-contract", self._token_factory)
                negative_path = self._write_contract(
                    "negative-contract.json",
                    build_contract(self._config, negative_id, negative=True),
                )
                negative_environment = self._config.child_environment()
                negative_environment["PILLARMESH_SNOWFLAKE_TARGET_TABLE"] = environment[
                    "PILLARMESH_SNOWFLAKE_NEGATIVE_TARGET_TABLE"
                ]
                self._invoke(
                    ("create-draft", str(negative_path)),
                    environment=negative_environment,
                )
                negative = self._invoke(
                    ("verify", negative_id, "1"),
                    environment=negative_environment,
                )
                decision = _mapping(negative.get("legality_decision"), "negative verification")
                preconditions = decision.get("preconditions")
                if (
                    decision.get("result") != "no_valid_plan"
                    or decision.get("execution_occurred") is not False
                    or not isinstance(preconditions, list)
                    or not any(
                        isinstance(item, dict)
                        and item.get("number") == 6
                        and item.get("status") == "unsatisfied"
                        for item in preconditions
                    )
                ):
                    raise HarnessError("negative contract was not rejected by precondition 6")
                negative_after = self._providers.negative_observation()
                state_after = self._inspector.counters()
                if state_after != state_before:
                    raise HarnessError("negative plan created local execution state")
                if negative_after.effect_state() != negative_before.effect_state():
                    raise HarnessError("negative plan created a data read or provider effect")

                package_dir = self._config.output_dir / "evidence-package"
                resources["evidence_package"] = ledger.register(
                    kind="evidence_package",
                    exact_identifier=str(package_dir),
                    retention_seconds=THIRTY_DAYS,
                    cleanup_operation="delete_local_state",
                )
                ledger.mark_attempted(resources["evidence_package"])
                commit_sha, lock_digest, python_version = self._revision_factory()
                metadata_path = self._config.output_dir / "package-metadata.json"
                metadata = {
                    "commit_sha": commit_sha,
                    "uv_lock_digest": lock_digest,
                    "python_version": python_version,
                    "mcp_protocol_version": environment["PILLARMESH_MCP_PROTOCOL_VERSION"],
                    "operator_pseudonym": environment["PILLARMESH_OPERATOR_PSEUDONYM"],
                    "host_pseudonym": environment["PILLARMESH_HOST_PSEUDONYM"],
                    "transport_decision": "cli-fallback",
                    "resources": ledger.sanitized_dispositions(),
                    "limitations": (
                        "snapshot_only",
                        "deletions_not_observed",
                        "cdc_absent",
                    ),
                }
                write_private_file(
                    metadata_path,
                    (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode(),
                )
                scan_input = {
                    "credential_canaries": self._config.credential_canaries,
                    "row_value_canaries": (environment["PILLARMESH_ROW_VALUE_CANARY"],),
                    "acceptance_keys": (acceptance_key,),
                    "local_path_prefixes": (
                        str(self._config.state_path.parent),
                        str(self._config.output_dir),
                    ),
                }
                package_environment = self._config.child_environment()
                package_environment["PILLARMESH_SCAN_INPUT_JSON"] = json.dumps(
                    scan_input, sort_keys=True, separators=(",", ":")
                )
                ledger.persist(run_state="running")
                exported = self._invoke(
                    (
                        "export-evidence",
                        run_id,
                        str(package_dir),
                        str(metadata_path),
                    ),
                    environment=package_environment,
                )
                ledger.mark_created(resources["evidence_package"])
                reservation.assert_private_tree()
                verified_package = self._invoke(
                    ("verify-evidence", str(package_dir)),
                    environment=package_environment,
                )
                package_digest = str(exported.get("package_index_digest", ""))
                verification_digest = str(exported.get("verification_result_digest", ""))
                if (
                    verified_package.get("package_index_digest") != package_digest
                    or verified_package.get("verification_result_digest") != verification_digest
                    or re.fullmatch(r"[0-9a-f]{64}", package_digest) is None
                    or re.fullmatch(r"[0-9a-f]{64}", verification_digest) is None
                ):
                    raise HarnessError("independent package verification did not match export")
                ledger.set_context(
                    run_id=run_id,
                    package_path=str(package_dir),
                    package_index_digest=package_digest,
                )
                run_state = "succeeded"
                return AcceptanceResult(
                    run_id=run_id,
                    package_index_digest=package_digest,
                    verification_result_digest=verification_digest,
                    cleanup_ledger_path=self._config.cleanup_ledger_path,
                )
            except BaseException as error:
                primary_error = error
                raise
            finally:
                if run_state != "succeeded":
                    try:
                        self._record_recoverable_resources(
                            ledger,
                            acceptance_key,
                            expected,
                            resources,
                            activation_started=activation_started,
                        )
                    except BaseException:
                        ledger.set_context(resource_recovery="unavailable")
                try:
                    ledger.persist(run_state=run_state)
                except BaseException:
                    if primary_error is None:
                        raise
                self._reservation = None


class SQLiteRunInspector:
    def __init__(self, state_path: Path, output_dir: Path) -> None:
        self._state_path = state_path
        self._output_dir = output_dir

    def run_evidence(self, run_id: str) -> RunEvidence:
        connection = sqlite3.connect(self._state_path)
        try:
            rows = connection.execute(
                "SELECT kind,digest,payload FROM artifacts "
                "WHERE kind IN ('segment_manifest','visibility_proof')"
            ).fetchall()
            run = connection.execute(
                "SELECT batch_id FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            connection.close()
        if run is None:
            raise HarnessError("terminal run is absent from local state")
        batch_id = str(run[0])
        manifests = [
            (str(item[1]), SegmentManifest.model_validate_json(bytes(item[2])))
            for item in rows
            if item[0] == "segment_manifest"
        ]
        proofs = [
            VisibilityProof.model_validate_json(bytes(item[2]))
            for item in rows
            if item[0] == "visibility_proof"
        ]
        manifest = next((item for item in manifests if item[1].batch_id == batch_id), None)
        proof = next((item for item in proofs if item.batch_id == batch_id), None)
        if manifest is None or proof is None:
            raise HarnessError("terminal evidence is incomplete")
        return RunEvidence(
            batch_id=batch_id,
            manifest_digest=manifest[0],
            acceptance_value_digest=manifest[1].acceptance_value_digest,
            visibility_value_digest=proof.value_digest,
        )

    def expected_run_resources(
        self, contract_digest: str, summary_digest: str, acceptance_key: int
    ) -> ExpectedRunResources:
        connection = sqlite3.connect(self._state_path)
        try:
            row = connection.execute(
                "SELECT payload FROM artifacts WHERE kind='activation_summary' AND digest=?",
                (summary_digest,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise HarnessError("activation summary is absent from local state")
        summary = json.loads(bytes(row[0]))
        signed_graph = summary.get("signed_graph")
        if not isinstance(signed_graph, dict) or not isinstance(signed_graph.get("graph"), dict):
            raise HarnessError("activation summary graph is invalid")
        activation_identity = digest(
            {
                "domain": "pillarmesh-m0-activation-v1",
                "contract_digest": contract_digest,
                "summary_digest": summary_digest,
                "acceptance_key": acceptance_key,
            }
        )
        run_id = f"run-{activation_identity[:24]}"
        batch_id = digest(
            {
                "domain": "pillarmesh-m0-batch-v1",
                "run_id": run_id,
                "graph": signed_graph["graph"],
            }
        )[:32]
        return ExpectedRunResources(run_id, batch_id)

    def counters(self) -> StateCounters:
        connection = sqlite3.connect(self._state_path)
        try:
            runs = int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
            events = int(connection.execute("SELECT COUNT(*) FROM evidence_events").fetchone()[0])
            private_states = int(
                connection.execute("SELECT COUNT(*) FROM run_private_state").fetchone()[0]
            )
        finally:
            connection.close()
        segment_files = sum(1 for path in self._output_dir.rglob("segment.csv") if path.is_file())
        return StateCounters(runs, events, private_states, segment_files)

    def latest_progress(self) -> RunProgress | None:
        if not self._state_path.exists():
            return None
        try:
            connection = sqlite3.connect(self._state_path)
            try:
                run = connection.execute(
                    "SELECT run_id,batch_id FROM runs ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if run is None:
                    return None
                event_rows = connection.execute(
                    "SELECT event_type FROM evidence_events WHERE run_id=? ORDER BY sequence",
                    (run[0],),
                ).fetchall()
                private = connection.execute(
                    "SELECT segment_path FROM run_private_state WHERE run_id=?", (run[0],)
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return None
        segment_path = None if private is None or private[0] is None else Path(str(private[0]))
        return RunProgress(
            run_id=str(run[0]),
            batch_id=None if run[1] is None else str(run[1]),
            event_types=tuple(str(row[0]) for row in event_rows),
            segment_path=segment_path,
        )
