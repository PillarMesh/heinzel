from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pillarmesh_access_control import (
    AuthenticatedConnectedPolicyAuthority,
    CurrentEntitlementResolver,
    SQLiteEntitlementRepository,
)
from pillarmesh_compiler.query_repository import SQLiteQueryPlanRepository
from pillarmesh_console.governed_adapters import (
    InMemoryWorkspacePrincipalDirectory,
    SQLiteAnswerDownloadReceiptRepository,
)
from pillarmesh_request_management import (
    AnswerInterpreter,
    AnswerPolicyAdmissionService,
    AnswerQuestionService,
    AnswerScopePolicy,
    ExecuteGovernedAnswerWorkflowCommand,
    GovernedAnswer,
    GovernedAnswerNotVisible,
    GovernedAnswerService,
    GovernedAnswerStaleRevision,
    GovernedAnswerWorkflow,
    PolicyAdmissionExecutionAuthorizer,
    RepositoryAnswerExecutionReader,
    RepositoryAnswerPlanReader,
    RequestManagementDashboardAnswerAuthorityReader,
    RequestManagementService,
    RequestState,
    SQLiteAnswerAdmissionRepository,
    SQLiteAnswerScopePolicyRepository,
    SQLiteAnswerValidationRepository,
    SQLiteGovernedAnswerRepository,
    SQLitePolicyAdmissionEvidenceReader,
    SQLiteRequestRepository,
    WorkflowIncidentProjectionDeferred,
)
from pillarmesh_request_management.answer_investigation import (
    SQLiteAnswerInvestigationAuthority,
)
from pillarmesh_request_management.answer_usage import DurableStatementCeilingBreachReader
from pillarmesh_runtime import (
    AnswerExecutionIncidentProjectionUnavailable,
    AnswerExecutionIncidentProjector,
    AnswerExecutionReceipt,
    AnswerGenerationReader,
    AnswerQueryPlanSignatureVerifier,
    AnswerQueryProvider,
    GovernedQueryExecutor,
    QueryResultNotFound,
    SQLiteAnswerResultStore,
)
from pillarmesh_state import SQLiteIncidentRepository

from tests.acceptance.console_answer_authority import CurrentAnswerAuthority
from tests.acceptance.console_product_authority import (
    ApprovedProductAnswerMetadataReader,
    DurableProductAnswerAuthorityReader,
    MaterializationReceiptReader,
    ProductGenerationAuthorityReader,
    SourceFreshnessReader,
)


class _PrincipalReader:
    def __init__(self, directory: InMemoryWorkspacePrincipalDirectory) -> None:
        self._directory = directory

    def resolve_principal(self, *, tenant_id: str, actor_id: str) -> str | None:
        return self._directory.principal_ref(
            tenant_id=tenant_id,
            actor_id=actor_id,
            role="requester",
        )


class _GenerationReader(AnswerGenerationReader, ProductGenerationAuthorityReader, Protocol):
    pass


class _WorkflowIncidentProjector:
    def __init__(self, delegate: AnswerExecutionIncidentProjector) -> None:
        self._delegate = delegate

    def project(self, receipt: AnswerExecutionReceipt) -> object | None:
        try:
            return self._delegate.project(receipt)
        except AnswerExecutionIncidentProjectionUnavailable as error:
            raise WorkflowIncidentProjectionDeferred from error


class _StoredCurrentPolicyResolver:
    def __init__(self, connection: sqlite3.Connection, *, clock: Callable[[], datetime]) -> None:
        self._connection = connection
        self._clock = clock

    def resolve_current(
        self, *, tenant_id: str, principal_ref: str, purpose: str
    ) -> AnswerScopePolicy | None:
        rows = self._connection.execute(
            "SELECT payload FROM answer_scope_policies WHERE tenant_id = ? "
            "ORDER BY policy_id, revision DESC",
            (tenant_id,),
        ).fetchall()
        latest: dict[str, AnswerScopePolicy] = {}
        for row in rows:
            if not isinstance(row[0], bytes):
                return None
            policy = AnswerScopePolicy.model_validate_json(row[0], strict=True)
            if policy.tenant_id != tenant_id:
                return None
            latest.setdefault(policy.policy_id, policy)
        now = self._clock()
        matches = tuple(
            policy
            for policy in latest.values()
            if principal_ref in policy.principal_scope
            and purpose in policy.purposes
            and policy.valid_from <= now < policy.valid_until
        )
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class GovernedAnswerRuntimeConfiguration:
    connected_authority: AuthenticatedConnectedPolicyAuthority
    connected_authority_ref: str
    interpreter: AnswerInterpreter
    materializations: MaterializationReceiptReader
    freshness: SourceFreshnessReader
    product_metadata: ApprovedProductAnswerMetadataReader
    generations: _GenerationReader
    signature_verifier: AnswerQueryPlanSignatureVerifier
    provider_resolver: Callable[[str], AnswerQueryProvider]
    clock: Callable[[], datetime]
    sleeper: Callable[[float], None]
    intent_identifier: Callable[[], str]
    validation_identifier: Callable[[], str]
    admission_identifier: Callable[[], str]


class GovernedAnswerRuntime:
    """Compose the owning answer services around one durable request repository."""

    def __init__(
        self,
        directory: Path,
        *,
        requests: SQLiteRequestRepository,
        request_service: RequestManagementService,
        principals: InMemoryWorkspacePrincipalDirectory,
        configuration: GovernedAnswerRuntimeConfiguration,
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._entitlement_connection = sqlite3.connect(
            directory / "answer-entitlements.sqlite3", check_same_thread=False
        )
        self._result_connection = sqlite3.connect(
            directory / "answer-results.sqlite3", check_same_thread=False
        )
        self._download_connection = sqlite3.connect(
            directory / "answer-downloads.sqlite3", check_same_thread=False
        )
        self.incidents = SQLiteIncidentRepository(
            directory / "answer-incidents.sqlite3", check_same_thread=False
        )

        self.validations = SQLiteAnswerValidationRepository(requests.connection)
        self.policies = SQLiteAnswerScopePolicyRepository(requests.connection)
        self.investigations = SQLiteAnswerInvestigationAuthority(
            requests.connection,
            clock=configuration.clock,
        )
        self.admissions = SQLiteAnswerAdmissionRepository(requests)
        self.plans = SQLiteQueryPlanRepository(requests.connection)
        self.results = SQLiteAnswerResultStore(
            self._result_connection,
            clock=configuration.clock,
        )
        self.downloads = SQLiteAnswerDownloadReceiptRepository(self._download_connection)
        self.entitlements = CurrentEntitlementResolver(
            repository=SQLiteEntitlementRepository(self._entitlement_connection),
            connected_authority=configuration.connected_authority,
            connected_authority_ref=configuration.connected_authority_ref,
            clock=configuration.clock,
        )
        products = DurableProductAnswerAuthorityReader(
            materializations=configuration.materializations,
            freshness=configuration.freshness,
            product_metadata=configuration.product_metadata,
            generations=configuration.generations,
            clock=configuration.clock,
        )
        authority = CurrentAnswerAuthority(
            requests=requests,
            admissions=self.admissions,
            plans=self.plans,
            policies=self.policies,
            validations=self.validations,
            entitlements=self.entitlements,
            principals=_PrincipalReader(principals),
            products=products,
            clock=configuration.clock,
        )
        self.questions = AnswerQuestionService(
            configuration.interpreter,
            self.validations,
            policy_resolver=_StoredCurrentPolicyResolver(
                requests.connection,
                clock=configuration.clock,
            ),
            intent_identifier=configuration.intent_identifier,
            validation_identifier=configuration.validation_identifier,
            clock=configuration.clock,
        )
        self.policy_admissions = AnswerPolicyAdmissionService(
            self.admissions,
            plans=self.plans,
            breaches=DurableStatementCeilingBreachReader(
                admissions=self.admissions,
                executions=self.results,
                plans=self.plans,
            ),
            clock=configuration.clock,
            admission_identifier=configuration.admission_identifier,
            investigations=self.investigations,
        )
        executor = GovernedQueryExecutor(
            store=self.results,
            signature_verifier=configuration.signature_verifier,
            authorizer=PolicyAdmissionExecutionAuthorizer(
                self.admissions,
                authority.runtime_rechecker(),
            ),
            generation_reader=configuration.generations,
            provider_resolver=configuration.provider_resolver,
            clock=configuration.clock,
            sleeper=configuration.sleeper,
        )
        answer_repository = SQLiteGovernedAnswerRepository(requests)
        execution_reader = RepositoryAnswerExecutionReader(
            self.results,
            missing_result_error=QueryResultNotFound,
        )
        self.answers = GovernedAnswerService(
            repository=answer_repository,
            admission_reader=SQLitePolicyAdmissionEvidenceReader(self.admissions, requests),
            execution_reader=execution_reader,
            plan_reader=RepositoryAnswerPlanReader(self.plans),
            authorization_rechecker=authority.delivery_rechecker(),
            clock=configuration.clock,
        )
        self.dashboard_answers = RequestManagementDashboardAnswerAuthorityReader(
            requests=requests,
            answers=answer_repository,
            admissions=SQLitePolicyAdmissionEvidenceReader(self.admissions, requests),
            executions=execution_reader,
            clock=configuration.clock,
        )
        self._workflow = GovernedAnswerWorkflow(
            requests=request_service,
            admissions=self.admissions,
            plans=self.plans,
            executions=self.results,
            executor=executor,
            incident_projector=_WorkflowIncidentProjector(
                AnswerExecutionIncidentProjector(self.incidents)
            ),
            delivery=self.answers,
        )

    def execute_answer(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        expected_revision: int,
    ) -> GovernedAnswer:
        try:
            request = self.admissions.load_request(tenant_id, request_id)
            matching = tuple(
                admission
                for admission in self.admissions.list_admissions(tenant_id, request_id)
                if admission.plan_digest is not None
                and admission.request_revision + 1 == expected_revision
            )
        except KeyError:
            raise GovernedAnswerNotVisible("answer workflow authority is not visible") from None
        permitted_revisions = (expected_revision, expected_revision + 1, expected_revision + 2)
        if request.revision not in permitted_revisions:
            raise GovernedAnswerStaleRevision("governed answer workflow revision is stale")
        expected_states = {
            expected_revision: RequestState.EXECUTING,
            expected_revision + 1: RequestState.VERIFYING,
            expected_revision + 2: RequestState.DELIVERED,
        }
        if expected_states.get(request.revision) is not request.state or len(matching) != 1:
            raise GovernedAnswerNotVisible("answer workflow authority is not visible")
        admission = matching[0]
        return self._workflow.execute(
            ExecuteGovernedAnswerWorkflowCommand(
                tenant_id=tenant_id,
                request_id=request_id,
                expected_revision=expected_revision,
                requester_id=request.requester_id,
                actor_id=actor_id,
                admission_ref=admission.admission_id,
                model_narrative=None,
                refreshes_answer_ref=None,
            )
        )

    def close(self) -> None:
        failure: BaseException | None = None
        try:
            self.incidents.close()
        except BaseException as error:
            failure = error
        for connection in (
            self._download_connection,
            self._result_connection,
            self._entitlement_connection,
        ):
            try:
                connection.close()
            except BaseException as error:
                failure = failure or error
        if failure is not None:
            raise failure
