from datetime import datetime, timedelta

from pillarmesh_contract_model import IntegrationContract, digest
from pillarmesh_execution_graph import (
    EvidenceRequirement,
    ExecutionGraph,
    GraphSigner,
)
from pillarmesh_iir import lower_contract
from pillarmesh_provider_sdk import ProviderObservation

from .legality import evaluate_legality
from .models import AdmittedPlan, CompilationBundle, NoValidPlan


class CompilerDefect(RuntimeError):
    pass


def compile_contract(
    contract: IntegrationContract,
    source: ProviderObservation,
    destination: ProviderObservation,
    signer: GraphSigner,
    now: datetime,
    *,
    graph_evidence_override: tuple[str, ...] | None = None,
) -> CompilationBundle:
    decision = evaluate_legality(contract, source, destination, now)
    if isinstance(decision, NoValidPlan):
        failed = ", ".join(
            str(item.number) for item in decision.preconditions if item.status != "satisfied"
        )
        raise ValueError(f"No Valid Plan; failed preconditions: {failed}")
    if not isinstance(decision, AdmittedPlan):
        raise CompilerDefect("unknown legality result")
    iir = lower_contract(contract)
    plan = decision.physical_plan
    graph_evidence = plan.required_evidence
    if graph_evidence_override is not None:
        graph_evidence = tuple(
            EvidenceRequirement(event_type=item, redaction_class="metadata")
            for item in graph_evidence_override
        )
    if graph_evidence != plan.required_evidence:
        raise CompilerDefect("execution graph evidence set differs from admitted plan evidence set")
    graph = ExecutionGraph(
        contract_digest=digest(contract),
        iir_digest=iir.semantic_digest,
        physical_plan_digest=digest(plan),
        legality_decision_digest=digest(decision),
        source_observation_digest=digest(source),
        destination_observation_digest=digest(destination),
        source_connection_handle=contract.source.connection_handle,
        destination_connection_handle=contract.destination.connection_handle,
        source_object_identity=source.object_identity,
        source_schema_digest=source.schema_digest,
        projection=contract.projection,
        operators=plan.operators,
        required_evidence=graph_evidence,
        issued_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    return CompilationBundle(
        contract_digest=digest(contract),
        verified_at=now,
        iir=iir,
        physical_plan=plan,
        legality_decision=decision,
        signed_graph=signer.sign(graph),
    )
