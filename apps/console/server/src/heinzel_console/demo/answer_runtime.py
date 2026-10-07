"""The governed answer runtime, composed over the demonstration's own warehouse and stores.

This is the fourteen collaborators `GovernedAnswerRuntimeConfiguration` asks for, supplied from
what the demonstration has: a local signed entitlement authority, its approved scope policy, the
product its own chain materialized, and the two read-only warehouse roles that may read the
product and explain a statement over it.

It is a context manager because two of those collaborators hold resources for as long as the
console does: the entitlement authority serves over loopback TLS, and the runtime holds four
SQLite connections. Both stop with the context.

Nothing in this package imports from `tests/`, and no test module is executed at runtime.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from heinzel_compiler.query_signing import QueryPlanSigner, QueryPlanVerifier
from heinzel_contract_model import digest
from heinzel_provider_postgresql import (
    PostgreSQLAnswerGenerationAuthority,
    PostgreSQLAnswerQueryProvider,
    PostgreSQLAnswerQuerySettings,
    PostgreSQLMaterializationSettings,
    PostgreSQLProductGenerationAuthority,
    PostgreSQLRelationSizeQueryEstimator,
    PostgreSQLRelationSizeQueryEstimatorSettings,
)
from heinzel_request_management import (
    BoundSemanticReference,
    RequestManagementService,
    StakeholderQuestion,
)
from pydantic import SecretStr

from ..answers import (
    LOCAL_CONNECTED_AUTHORITY_REF,
    DurableProductQueryBindingReader,
    GovernedAnswerRuntime,
    GovernedAnswerRuntimeConfiguration,
    local_signed_policy_authority,
)
from ..governed_adapters import InMemoryWorkspacePrincipalDirectory
from .answer_preparation import DemoAnswerPreparation
from .answers import (
    DemoAnswerInterpreter,
    activate_demo_answer_scope_policy,
    demo_answer_bindings,
    demo_entitlement_body,
    demo_product_reference,
)
from .bootstrap import DemoWarehouseGeneration
from .publication import DEMO_PRODUCT_NAME, DEMO_PURPOSE, DEMO_TENANT_ID, DemoPublication
from .stores import DemoStores

__all__ = ["DemoAnswerAdmission", "DemoGovernedAnswer", "demo_governed_answer"]

# How long the demonstration's entitlement and scope policy stay valid. A year, for the same
# reason the generation's retention and the publication's authority validity are: a demonstration
# may be left running, and an authority that expired overnight would read as a broken product.
_AUTHORITY_VALIDITY = timedelta(days=365)

# The estimator measures one relation's size. Both bounds are generous for that and still small
# enough that a warehouse which has stopped answering fails rather than holding the console's
# request open.
_ESTIMATOR_CONNECT_TIMEOUT_SECONDS = 5
_ESTIMATOR_STATEMENT_TIMEOUT_SECONDS = 5

# The key the demonstration signs query plans with. Generated per process and never stored,
# because a plan is compiled and executed inside one request: `GovernedQueryExecutor` verifies the
# signature before it runs the statement, and nothing reads a plan after the process that compiled
# it has gone. A deployment's compiler holds a managed key, which is what makes its plans
# verifiable by whatever executes them later.
_QUERY_SIGNING_KEY_ID = "compiler-demo-query-1"


@dataclass(frozen=True, slots=True)
class DemoAnswerAdmission:
    """Admit a question's governed plan, compiling it first because nothing else does.

    This is the console's `AnswerAdmissionCommands`. The backend has already checked the actor's
    role, the revision, the proposal digest the browser displayed and its own projection of the
    approvals recorded against that proposal; what is left is the decision about the statement,
    which is what this makes.

    It is not the fulfillment service's admission and does not stand in for one. Taking this
    path means that service never judges the admission, so the authority each approver still
    holds is not re-checked, proposal drift is not detected, and no fulfillment admission
    receipt is recorded. `GovernedConsoleBackend.admit_request` says why the two cannot both
    run, and the quickstart README lists it among where the demonstration stops.
    """

    preparation: DemoAnswerPreparation
    requests: RequestManagementService

    def admit_answer(
        self, *, tenant_id: str, request_id: str, actor_id: str, expected_revision: int
    ) -> object:
        if tenant_id != DEMO_TENANT_ID:
            raise ValueError("the demonstration admits answers for its own tenant alone")
        # The question is read from the request rather than taken on trust: the intent the
        # admission binds must be an intent about the question the request actually carries,
        # composed from the terms that request actually selected.
        request = self.requests.get(tenant_id, request_id)
        if not isinstance(request.payload, StakeholderQuestion):
            raise ValueError("the demonstration admits answers for stakeholder questions alone")
        return self.preparation.prepare(
            request_id=request_id,
            request_revision=expected_revision,
            question=request.payload,
            actor_id=actor_id,
        )


@dataclass(frozen=True, slots=True)
class DemoGovernedAnswer:
    """The runtime that executes an admitted answer, and the step that admits one."""

    runtime: GovernedAnswerRuntime
    preparation: DemoAnswerPreparation
    # How Superset reaches the product this answer was read from, as the least-privilege dashboard
    # reader. Carried here because the publication is composed from the answer and the generation
    # together, and kept out of the representation: it holds a role password.
    dashboard_database_uri: str = field(repr=False)

    def admission_commands(self, requests: RequestManagementService) -> DemoAnswerAdmission:
        """The console seam that admits a question's plan, over this preparation."""
        return DemoAnswerAdmission(preparation=self.preparation, requests=requests)

    def list_selectable_answer_terms(self, tenant_id: str) -> tuple[BoundSemanticReference, ...]:
        """The approved terms a question may be composed from, for one tenant.

        The preparation's own bindings, which are the publication's: the same tuple the
        interpreter resolves a selection against and the validation binds the intent against. A
        separate list here could offer a term the validation would then refuse.

        Another tenant gets nothing rather than an error, because this demonstration publishes for
        one tenant and a tenant with no publication has no terms to offer -- which is what the
        empty tuple says.
        """
        if tenant_id != DEMO_TENANT_ID:
            return ()
        return self.preparation.bindings


@contextmanager
def demo_governed_answer(
    directory: Path,
    *,
    stores: DemoStores,
    requests: RequestManagementService,
    principals: InMemoryWorkspacePrincipalDirectory,
    publication: DemoPublication,
    generation: DemoWarehouseGeneration,
    principal_ref: str,
    clock: Callable[[], datetime],
) -> Iterator[DemoGovernedAnswer]:
    """Compose the governed answer over one published generation, for the length of the context.

    The order here is forced. The entitlement authority has to be serving before the runtime is
    composed, because the runtime resolves entitlements through it; the runtime has to exist
    before the scope policy is activated, because the policy is stored in the runtime's own
    repository; and the policy has to exist before the preparation, which validates against it.
    """
    product_ref = demo_product_reference(
        publication.contract.version, digest(publication.contract.destination_product)
    )
    # One tuple, read by two collaborators: the interpreter resolves a requester's selection
    # against it and the validation binds the intent against it. Building it twice would let the
    # two disagree, and the disagreement would read as an unresolved reference rather than as a
    # composition mistake.
    bindings = demo_answer_bindings(publication.semantic_version, product_ref=product_ref)
    entitlement = demo_entitlement_body(
        principal_ref=principal_ref,
        purpose=DEMO_PURPOSE,
        semantic_version=publication.semantic_version,
        product_ref=product_ref,
        now=clock(),
        valid_for=_AUTHORITY_VALIDITY,
    )
    signed_model = generation.signed_model
    with local_signed_policy_authority(directory / "policy", body=entitlement) as authority:
        provider = PostgreSQLAnswerQueryProvider(
            settings=PostgreSQLAnswerQuerySettings(dsn=SecretStr(generation.answer_dsn)),
            generation_authority=PostgreSQLAnswerGenerationAuthority(
                query_bindings=stores.query_bindings,
                materializations=stores.materialization_receipts,
                signed_model=signed_model.signed_model,
                # Keyed by the model's own `key_id` rather than a constant written here: a
                # verifier that looked the key up under a different name would find none and
                # refuse the answer as an integrity failure.
                trusted_compiler_keys={signed_model.signed_model.key_id: signed_model.public_key},
            ),
        )
        query_signing_key = Ed25519PrivateKey.generate()
        # Closed with this context, like the authority above it: the runtime opens four SQLite
        # connections of its own -- its incidents, its results, its downloads and its resolved
        # entitlements -- and a console that composed one per start without closing it would
        # hold every one of them until the process ended.
        runtime = GovernedAnswerRuntime(
            directory / "answer-runtime",
            requests=stores.requests,
            request_service=requests,
            principals=principals,
            configuration=GovernedAnswerRuntimeConfiguration(
                connected_authority=authority.reader,
                connected_authority_ref=LOCAL_CONNECTED_AUTHORITY_REF,
                interpreter=DemoAnswerInterpreter(published=bindings),
                materializations=stores.materialization_receipts,
                freshness=stores.source_freshness,
                product_metadata=stores.product_versions,
                generations=PostgreSQLProductGenerationAuthority(
                    PostgreSQLMaterializationSettings(
                        tenant_id=publication.contract.tenant_id,
                        # The answer role, not the materialization role: this reads which
                        # generation is addressable and must not be able to change it.
                        dsn=SecretStr(generation.answer_dsn),
                        consumption_schema_name="consumption",
                        consumption_view_name=DEMO_PRODUCT_NAME,
                        control_schema_name="product_control",
                        generation_table_name="product_generations",
                        generation_pointer_table_name="product_generation_pointers",
                    )
                ),
                signature_verifier=QueryPlanVerifier(
                    {_QUERY_SIGNING_KEY_ID: query_signing_key.public_key()}
                ),
                provider_resolver=lambda engine_kind: provider,
                clock=clock,
                # Nothing to wait for: the demonstration's product is three rows in a local
                # warehouse, so a retry delay would only hold the console's request open.
                sleeper=lambda delay: None,
                intent_identifier=lambda: f"intent-demo-{uuid.uuid4()}",
                validation_identifier=lambda: f"validation-demo-{uuid.uuid4()}",
                admission_identifier=lambda: f"admission-demo-{uuid.uuid4()}",
            ),
        )
        with closing(runtime):
            yield DemoGovernedAnswer(
                runtime=runtime,
                dashboard_database_uri=generation.dashboard_database_uri,
                preparation=DemoAnswerPreparation(
                    entitlements=runtime.entitlements,
                    questions=runtime.questions,
                    plans=runtime.plans,
                    admissions=runtime.policy_admissions,
                    materializations=stores.materialization_receipts,
                    freshness=stores.source_freshness,
                    query_bindings=DurableProductQueryBindingReader(stores.query_bindings),
                    policy=activate_demo_answer_scope_policy(
                        runtime.policies,
                        semantic_version=publication.semantic_version,
                        product_ref=product_ref,
                        principal_ref=principal_ref,
                        purpose=DEMO_PURPOSE,
                        clock=clock,
                        valid_for=_AUTHORITY_VALIDITY,
                    ),
                    bindings=bindings,
                    product_ref=product_ref,
                    generation=generation.generation,
                    purpose=DEMO_PURPOSE,
                    principal_ref=principal_ref,
                    signer=QueryPlanSigner(_QUERY_SIGNING_KEY_ID, query_signing_key),
                    # Bounded by the product relation's measured size rather than by PostgreSQL's
                    # plan estimates, which `PostgreSQLQueryEstimator` beside it declines to report
                    # as scan bytes. Configured with the relation the generation actually landed in,
                    # so a statement over anything else is refused rather than bounded by the wrong
                    # measurement.
                    estimator=PostgreSQLRelationSizeQueryEstimator(
                        settings=PostgreSQLRelationSizeQueryEstimatorSettings(
                            dsn=SecretStr(generation.estimator_dsn),
                            connect_timeout_seconds=_ESTIMATOR_CONNECT_TIMEOUT_SECONDS,
                            statement_timeout_seconds=_ESTIMATOR_STATEMENT_TIMEOUT_SECONDS,
                            namespace=generation.namespace,
                            relation_name=generation.relation_name,
                        )
                    ),
                    clock=clock,
                ),
            )
