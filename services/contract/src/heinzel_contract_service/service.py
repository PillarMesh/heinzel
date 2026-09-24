from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from heinzel_compiler import (
    NoValidPlan,
    compile_contract,
    evaluate_legality,
)
from heinzel_contract_model import IntegrationContract, canonical_bytes, digest
from heinzel_evidence import EvidenceEvent, RunRecord, SQLiteStore
from heinzel_execution_graph import GraphSigner
from heinzel_provider_sdk import ProviderObservation

from .acquisition_lifecycle import (
    AcquisitionContractLifecycleNotFoundError,
    AcquisitionContractLifecycleRepository,
    StaleAcquisitionContractLifecycleError,
)
from .models import AcquisitionContractLifecycleState, ActivationSummary


class ObservableProvider(Protocol):
    def observe(self) -> ProviderObservation: ...


class AcquisitionContractAuthorityInvalidator(Protocol):
    def activate_contract_authority(self, tenant_id: str, contract_digest: str) -> int: ...

    def invalidate_contract_authority(self, tenant_id: str, contract_digest: str) -> None: ...


class ContractAuthorityBoundaryError(RuntimeError):
    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        super().__init__(f"contract authority boundary failed during {operation}")


def observation_fingerprint(observation: ProviderObservation) -> str:
    return digest(
        {
            "provider": observation.provider,
            "connection_handle": observation.connection_handle,
            "object_identity": observation.object_identity,
            "object_kind": observation.object_kind,
            "schema_digest": observation.schema_digest,
            "columns": observation.columns,
            "key_name": observation.key_name,
            "key_type": observation.key_type,
            "key_nullable": observation.key_nullable,
            "key_constraint": observation.key_constraint,
            "stable_key_order": observation.stable_key_order,
            "read_only": observation.read_only,
            "capabilities": observation.capabilities,
            "snapshot_semantics": observation.snapshot_semantics,
            "commit_ledger_object_kind": observation.commit_ledger_object_kind,
            "commit_ledger_columns": observation.commit_ledger_columns,
            "commit_ledger_key_name": observation.commit_ledger_key_name,
            "commit_ledger_key_constraint": observation.commit_ledger_key_constraint,
            "evidence_safe": observation.evidence_safe,
        }
    )


class ContractService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        signer: GraphSigner,
        source_resolver: Callable[[str], ObservableProvider],
        destination_resolver: Callable[[str], ObservableProvider],
        clock: Callable[[], datetime] | None = None,
        acquisition_authority_invalidator: AcquisitionContractAuthorityInvalidator | None = None,
        acquisition_lifecycle_repository: AcquisitionContractLifecycleRepository | None = None,
    ) -> None:
        self._store = store
        self._signer = signer
        self._source_resolver = source_resolver
        self._destination_resolver = destination_resolver
        self._clock = clock or (lambda: datetime.now(UTC))
        self._acquisition_authority_invalidator = acquisition_authority_invalidator
        self._acquisition_lifecycle_repository = acquisition_lifecycle_repository

    @staticmethod
    def _draft_ref(contract_id: str, version: int) -> str:
        return f"{contract_id}:{version:020d}"

    def create_draft(self, contract: IntegrationContract) -> IntegrationContract:
        contract_digest = digest(contract)
        self._store.save_artifact(
            "integration_contract", contract_digest, canonical_bytes(contract)
        )
        self._store.bind_artifact_ref(
            "contracts", self._draft_ref(contract.contract_id, contract.version), contract_digest
        )
        return contract

    def get_draft(self, contract_id: str, version: int) -> IntegrationContract:
        contract_digest = self._store.resolve_artifact_ref(
            "contracts", self._draft_ref(contract_id, version)
        )
        if contract_digest is None:
            raise KeyError((contract_id, version))
        payload = self._store.load_artifact("integration_contract", contract_digest)
        if payload is None:
            raise RuntimeError("contract reference points to a missing artifact")
        return IntegrationContract.model_validate_json(payload)

    def _latest_version(self, contract_id: str) -> int:
        refs = self._store.list_artifact_refs("contracts", f"{contract_id}:")
        if not refs:
            raise KeyError(contract_id)
        return max(int(ref_key.rsplit(":", 1)[1]) for ref_key, _digest in refs)

    def verify(self, contract_id: str, version: int) -> ActivationSummary | NoValidPlan:
        contract = self.get_draft(contract_id, version)
        source = self._source_resolver(contract.source.connection_handle).observe()
        destination = self._destination_resolver(contract.destination.connection_handle).observe()
        now = self._clock()
        source_digest = digest(source)
        destination_digest = digest(destination)
        decision = evaluate_legality(contract, source, destination, now)
        if isinstance(decision, NoValidPlan):
            self._store.save_artifact(
                "provider_observation", source_digest, canonical_bytes(source)
            )
            self._store.save_artifact(
                "provider_observation", destination_digest, canonical_bytes(destination)
            )
            decision_digest = digest(decision)
            self._store.save_artifact(
                "legality_decision", decision_digest, canonical_bytes(decision)
            )
            return decision

        compilation = compile_contract(contract, source, destination, self._signer, now)
        summary = ActivationSummary(
            contract_id=contract.contract_id,
            contract_version=contract.version,
            contract_digest=compilation.contract_digest,
            source_observation_digest=source_digest,
            destination_observation_digest=destination_digest,
            source_fingerprint=observation_fingerprint(source),
            destination_fingerprint=observation_fingerprint(destination),
            iir_digest=compilation.iir_digest,
            physical_plan_digest=compilation.physical_plan_digest,
            legality_decision_digest=compilation.legality_decision_digest,
            graph_digest=compilation.signed_graph.graph_digest,
            signed_graph_artifact_digest=compilation.signed_graph_artifact_digest,
            signing_key_id=compilation.signed_graph.key_id,
            verified_at=compilation.verified_at,
            expires_at=compilation.signed_graph.graph.expires_at,
            limitations=("snapshot_only", "deletions_not_observed", "cdc_absent"),
            source_effect="read one bounded read-only snapshot",
            destination_effect="idempotently upsert one bounded segment",
            signed_graph=compilation.signed_graph,
        )
        summary_digest = digest(summary)
        self._store.publish_verification(
            (
                (
                    "integration_contract",
                    compilation.contract_digest,
                    canonical_bytes(contract),
                ),
                ("provider_observation", source_digest, canonical_bytes(source)),
                ("provider_observation", destination_digest, canonical_bytes(destination)),
                ("intent_ir", compilation.iir_digest, canonical_bytes(compilation.iir)),
                (
                    "physical_plan",
                    compilation.physical_plan_digest,
                    canonical_bytes(compilation.physical_plan),
                ),
                (
                    "legality_decision",
                    compilation.legality_decision_digest,
                    canonical_bytes(compilation.legality_decision),
                ),
                (
                    "signed_execution_graph",
                    compilation.signed_graph_artifact_digest,
                    canonical_bytes(compilation.signed_graph),
                ),
            ),
            summary_digest,
            canonical_bytes(summary),
        )
        return summary

    def get_activation_summary(self, summary_digest: str) -> ActivationSummary:
        published_digest = self._store.resolve_artifact_ref("activation_summaries", summary_digest)
        if published_digest != summary_digest:
            raise KeyError(summary_digest)
        payload = self._store.load_artifact("activation_summary", summary_digest)
        if payload is None:
            raise KeyError(summary_digest)
        return ActivationSummary.model_validate_json(payload)

    def activate(self, contract_digest: str, summary_digest: str, acceptance_key: int) -> RunRecord:
        try:
            summary = self.get_activation_summary(summary_digest)
        except KeyError as error:
            raise ValueError("activation summary digest is unknown") from error
        if digest(summary) != summary_digest:
            raise ValueError("activation summary digest mismatch")
        if summary.contract_digest != contract_digest:
            raise ValueError("contract digest does not match activation summary")
        payload = self._store.load_artifact("integration_contract", contract_digest)
        if payload is None:
            raise ValueError("contract digest is unknown")
        contract = IntegrationContract.model_validate_json(payload)
        if contract.version != self._latest_version(contract.contract_id):
            raise ValueError("contract version is superseded")

        activation_identity = digest(
            {
                "domain": "heinzel-activation-v1",
                "contract_digest": contract_digest,
                "summary_digest": summary_digest,
                "acceptance_key": acceptance_key,
            }
        )
        existing = self._store.get_run_by_activation(activation_identity)
        if existing is not None:
            return existing
        now = self._clock()
        if now >= summary.expires_at:
            raise ValueError("signed execution graph is expired")
        source = self._source_resolver(contract.source.connection_handle).observe()
        destination = self._destination_resolver(contract.destination.connection_handle).observe()
        if (
            observation_fingerprint(source) != summary.source_fingerprint
            or observation_fingerprint(destination) != summary.destination_fingerprint
        ):
            raise ValueError("provider drift detected during activation")

        run_id = f"run-{activation_identity[:24]}"
        lifecycle_events: tuple[tuple[str, dict[str, object]], ...] = (
            ("draft_created", {"contract_digest": contract_digest}),
            (
                "verification_completed",
                {
                    "summary_digest": summary_digest,
                    "source_observation_digest": summary.source_observation_digest,
                    "destination_observation_digest": summary.destination_observation_digest,
                    "iir_digest": summary.iir_digest,
                    "physical_plan_digest": summary.physical_plan_digest,
                    "legality_decision_digest": summary.legality_decision_digest,
                    "signed_graph_artifact_digest": summary.signed_graph_artifact_digest,
                },
            ),
            (
                "legality_admitted",
                {"legality_decision_digest": summary.legality_decision_digest},
            ),
            (
                "graph_signed",
                {
                    "graph_digest": summary.graph_digest,
                    "signed_graph_artifact_digest": summary.signed_graph_artifact_digest,
                    "key_id": summary.signing_key_id,
                },
            ),
            (
                "activation",
                {
                    "contract_digest": contract_digest,
                    "summary_digest": summary_digest,
                    "graph_digest": summary.graph_digest,
                },
            ),
        )
        run = self._store.create_activated_run(
            run_id,
            activation_identity,
            contract_digest,
            summary_digest,
            summary.signed_graph.model_dump_json(),
            now,
            acceptance_key=acceptance_key,
            lifecycle_events=lifecycle_events,
            producer="heinzel-contract-service",
        )
        self._store.bind_artifact_ref("runs", run.run_id, activation_identity)
        return run

    def get_run(self, run_id: str) -> RunRecord:
        return self._store.get_run(run_id)

    def get_trace(self, run_id: str) -> tuple[EvidenceEvent, ...]:
        return self._store.trace(run_id)

    def activate_acquisition_contract(
        self,
        tenant_id: str,
        contract_digest: str,
    ) -> AcquisitionContractLifecycleState:
        invalidator = self._acquisition_authority_invalidator
        lifecycle_repository = self._acquisition_lifecycle_repository
        if invalidator is None or lifecycle_repository is None:
            raise ContractAuthorityBoundaryError(operation="activate acquisition contract")
        try:
            invalidator.activate_contract_authority(tenant_id, contract_digest)
            state = lifecycle_repository.activate(
                tenant_id=tenant_id,
                contract_digest=contract_digest,
                activated_at=self._clock(),
            )
            return state
        except Exception:
            raise ContractAuthorityBoundaryError(
                operation="activate acquisition contract"
            ) from None

    def retire_acquisition_contract(
        self,
        tenant_id: str,
        contract_digest: str,
        *,
        expected_revision: int,
    ) -> AcquisitionContractLifecycleState:
        invalidator = self._acquisition_authority_invalidator
        lifecycle_repository = self._acquisition_lifecycle_repository
        if invalidator is None or lifecycle_repository is None:
            raise ContractAuthorityBoundaryError(operation="retire acquisition contract")
        try:
            current = lifecycle_repository.get(tenant_id, contract_digest)
            if current.lifecycle_state != "activated" or current.revision != expected_revision:
                raise StaleAcquisitionContractLifecycleError(
                    "acquisition contract lifecycle revision is stale"
                )
            invalidator.invalidate_contract_authority(tenant_id, contract_digest)
            return lifecycle_repository.retire(
                tenant_id=tenant_id,
                contract_digest=contract_digest,
                expected_revision=expected_revision,
                retired_at=self._clock(),
            )
        except (
            AcquisitionContractLifecycleNotFoundError,
            StaleAcquisitionContractLifecycleError,
        ):
            raise
        except Exception:
            raise ContractAuthorityBoundaryError(operation="retire acquisition contract") from None
