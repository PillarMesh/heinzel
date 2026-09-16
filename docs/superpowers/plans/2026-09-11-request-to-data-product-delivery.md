# Request-to-Data-Product Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an authorized user submit a business data request, clarify and approve it, create any necessary governed acquisition and transformation work, execute it against a managed warehouse, and receive a durable dataset, browsable table, and governed Superset dashboard. Stakeholders, and the AI agents they delegate to, then ask questions that are answered from warehouse facts through governed queries, and the architect sees what every change affects.

**Architecture:** Extend the existing `Integration Contract -> Semantic IIR -> Physical Plan -> Execution Graph` path. Request management owns request intent, clarification, approval, answer scope policies, answer intent validation, and requester-visible delivery. Contract service owns activated process, source, and product contracts. The compiler deterministically turns approved contracts and validated answer intents into restricted plans. Runtime performs EXTRACT, LAND, and TRANSFORM, and executes compiled governed queries; a governed query is a read-only delivery action, not a fourth mode. The trigger service materializes run intents, the dbt adapter runs and observes compiled transformation models (ADR-0004), the knowledge graph projects the context graph, and context exposure serves the agent interface. Provider packages own PostgreSQL, ClickHouse, OpenMetadata, and Superset effects. State owns run identity, leases, attempts, checkpoints, replay, and recovery. The console projects those authorities and never invents contract or execution state.

**Tech Stack:** Python 3.13, frozen Pydantic v2 contracts, Starlette console adapters, SQLite control-state repositories, PostgreSQL and ClickHouse provider implementations, React/TypeScript console, OpenMetadata, Apache Superset, Docker Compose, pytest, mypy, Ruff, Vitest, Playwright.

**Specs:**
- `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`, including the reviewed 2026-09-11 amendment: §3.5 governed answers and agent access, §9.5 context graph, §12.4 governed query execution, §13.8 governed answer contract, §13.9 agent interface, §17.1 scouts, and the planned `answer_runtime` principal in §18.1. That amendment must be committed and merged before Task 1 closes; it is not present in `c67a654`.
- `docs/architecture/decisions/ADR-0003-managed-data-engineering-platform.md`, `ADR-0004-compilation-target.md`, and `ADR-0005-data-acquisition-strategy.md`
- `docs/superpowers/specs/2026-08-27-processing-model-design.md`
- `docs/superpowers/specs/2026-09-03-source-acquisition-delivery-design.md`
- `docs/audit/2026-09-10-live-catalog-warehouse-ux-audit.md`

**Product inspiration:** Parts of the governed-answer, agent-access, impact-analysis, and scout experience were inspired by Embrasure (`embrasure.ai`). PillarMesh's contracts, authority boundaries, deterministic compilation, execution controls, and acceptance requirements are defined independently by the specifications and ADRs above. This note does not claim affiliation, implementation compatibility, or shared technology.

## Global Constraints

- Keep the user model narrow: users ask for an outcome; they do not construct arbitrary workflow graphs.
- Use only three processing modes: EXTRACT, LAND, and TRANSFORM. Governed query execution (addendum §12.4) is a read-only delivery action over approved consumption objects, not a fourth mode.
- AI may propose a candidate typed intent, or a narrative that passes the addendum §13.8 narrative check. It may not author any SQL that runs, bind a term to meaning, resolve ambiguity, authorize contracts or policies, satisfy an approval, decide legality or verification, sign artifacts, or transition lifecycle state.
- Deliver a value only from a verified `AnswerExecutionReceipt`. A definition string never answers a question that asks for a value. The 2026-09-10 live audit found exactly this failure.
- Agent sessions act for a delegated human principal and are rechecked on every call. An agent call never satisfies an approval.
- Store request titles and other request facts in request management. The console only renders service-owned values.
- Never advance an acquisition checkpoint until the exact downstream LAND commit is durable and acknowledged.
- Do not store result rows, source values, credentials, or raw SQL in evidence artifacts. Evidence stores digests, counts, identities, decisions, and references. Compiled statement text lives only in the signed plan artifact, which only the architect can read.
- Every provider entry point translates driver failures into `ProviderError` classifications. Retry behavior must honor those classifications.
- Enforce tenant and actor authorization again on every result page, download, dashboard link, and access mutation.
- A live acceptance result requires a new request and new source rows created during the run, traced to terminal delivery. Existing catalog or warehouse rows are not proof.
- Preserve the current M0 artifact reader until all durable v1 artifacts have an explicit migration or retirement decision. New plan formats use a new schema version.

## Product Definition of Done

A release candidate is complete only when one fresh request can do all of the following in both PostgreSQL and ClickHouse destination profiles:

1. Capture a user-owned title, outcome, grain, measures, dimensions, freshness, and delivery needs.
2. Resolve clarification into an approved, versioned integration contract or produce an attributable `No Valid Plan` decision.
3. Activate source acquisition without exposing credentials or allowing one tenant to name another tenant's binding.
4. EXTRACT a bounded source interval, LAND it once, and advance its checkpoint only after the matching committed generation.
5. Compile approved semantics into deterministic, provider-specific restricted SQL and a signed execution graph.
6. TRANSFORM raw data into a conformed product and publish its authoritative metadata to OpenMetadata.
7. Answer an in-scope stakeholder question under an approved answer scope policy, without per-question approval, using a compiled governed query over the product's pinned generation. Store an immutable result snapshot with schema, pagination metadata, and provenance, and reconcile its values to an independent query of the fresh source cohort.
8. Show the result as a table, permit an authorized CSV download, and provision or update a Superset dashboard through a durable receipt.
9. Apply time-bounded access, visibly expire it, revoke it at the providers, and prevent access after revocation.
10. Replay the same run without duplicate source effects, duplicate generations, duplicate dashboards, or divergent artifact digests.
11. Give ambiguous, unknown-reference, out-of-scope, stale, and over-ceiling questions their exact addendum §13.8 outcome. An ambiguous or unknown-reference candidate goes to clarification and is discarded. An existing metric outside the policy scope, a stale product, and a plan with a missing or over-ceiling estimate go to per-question review without execution. A ceiling exceeded during execution fails with a receipt.
12. Return the same answer digest through the agent interface for a delegated principal. Deny agent approval attempts and any call made after the delegation is revoked.
13. Produce an impact analysis for a metric-version change that names every validated dependent dashboard, report, answer scope policy, and recent answer.

## Delivery Milestones

| Milestone | Outcome | Calendar estimate with 3 engineers | Primary dependency |
|---|---|---:|---|
| 0 | Confirm the governed-console baseline on `main` | 2-3 days | PR #58, merged as `c67a654` |
| 1 | Approved request becomes an activated contract | 2-3 weeks | Milestone 0 |
| 2 | Live PostgreSQL and Stripe acquisition reach committed raw generations | 2-3 weeks | Milestone 1 |
| 3 | Deterministic transforms materialize a data product | 4-5 weeks | Milestone 2 |
| 4 | Governed answers are computed from warehouse facts under an answer scope policy | 3-4 weeks | Milestone 3 |
| 5 | Superset dashboards and access grants are lifecycle-managed | 3-4 weeks | Milestone 4 |
| 6 | Context-graph impact analysis and the agent interface work | 3-4 weeks | Milestone 4; overlaps Milestone 5 |
| 7 | Recovery, two-engine conformance, and witnessed acceptance pass | 3-5 weeks | Milestones 1-6 |
| Post-MVP | Scouts | 2-3 weeks | Milestone 7 and the trigger service |

The MVP milestones contain roughly 55-72 engineer-weeks. That is 10-12 more than before the answer, context-graph, and agent work was added. With three engineers on non-overlapping component boundaries, the first narrow PostgreSQL slice (Gate A, including one governed answer) should be testable in 12-16 calendar weeks. The complete two-destination MVP is a 6-9 month calendar plan. It includes Superset, access expiry, impact analysis, the agent interface, fault injection, operational docs, and witnessed acceptance. One engineer should budget 13-17 months.

## Workstream Ownership

| Workstream | Owns | Must not own |
|---|---|---|
| Request management | intent, title, clarification, approval, answer scope policy, answer intent validation, policy admission, governed answer delivery, post-MVP scout definitions | source credentials, compiler legality, warehouse state, statement text |
| Contract service | process package, activated acquisition and product contracts | execution attempts, provider calls |
| Compiler | semantic validation, restricted logical/physical plans, governed query plans | runtime timestamps, provider credentials, approvals |
| State | run identity, lease, attempt, checkpoint, replay, recovery | business semantics, SQL generation, trigger policy |
| Trigger | trigger policies, scheduled-window identities, run-intent materialization | DAG authoring, provider access, direct execution |
| dbt adapter | version-pinned invocation and manifest, test, and lineage observation of compiled models | business semantics, SQL authoring |
| Runtime | ordered execution, governed query execution receipts, result snapshots | contract approval, provider-specific error leakage |
| Warehouse control | warehouse lifecycle, authoritative bindings, `answer_runtime` principal provisioning and probes | user request state, dashboard rendering |
| Knowledge graph | context-graph projection and impact analysis | authoritative records, approvals |
| Context exposure | agent interface tools and resources for delegated principals | approvals, write authority, SQL |
| Catalog control | publication and catalog reconciliation | source-of-truth contract mutation |
| BI control (new) | dashboard desired state and provider receipts | result computation, access policy decisions |
| Access control (new) | grant proposal application, expiry, revocation, receipts | request approval, provider internals |
| Console | projections and commands against owning services | invented state, local-only authoritative labels |

Adding `services/bi-control` and `services/access-control` is deliberate: dashboards span providers but have their own desired-state lifecycle, while access spans warehouse and BI providers and must expire independently. Before creating either directory, add the required ADR, repository-layout entry, and structure-validator coverage.

`services/trigger`, `services/dbt-adapter`, `services/knowledge-graph`, and `services/context-exposure` are already declared in `docs/architecture/repository-layout.md` and `tests/repository-structure/validate.sh`, so creating them needs no new ADR. Each still needs its workspace entry in `pyproject.toml`, `uv.lock`, and a `COVERED` row in the type-check inventory.

---

## Task 1: Establish the Integrated Baseline and Acceptance Ledger

**Files:**
- Create: `docs/delivery/request-to-data-product-acceptance.md`
- Modify: `docs/console/known-gaps.md`
- Modify: `tests/type-check/check.sh`
- Test: `tests/repository-structure/test.sh`

- [ ] Fetch `origin/main` and confirm it contains the governed-console baseline, merged as PR #58 (`c67a654`). Confirm the reviewed 2026-09-11 governed-answer amendment and its ADR/repository-layout changes have also been committed, reviewed, and merged; record that exact commit in the acceptance ledger. Start implementation branches from that refreshed `main`, not from `chore/no-valid-plan-design`. This plan branch predates the squash merge, and building on it would revert the merged review fixes.
- [ ] Record the live-audit finding as a P0 acceptance-ledger row: stakeholder answers are definition strings and no warehouse query runs. Its terminal proof is Definition of Done item 7.
- [ ] Inventory every currently delivered command and projection in request management, warehouse control, catalog control, runtime, and the console. Record its owning model, endpoint/adapter, durable store, and focused test in the acceptance ledger.
- [ ] Give every directory in the type-check inventory an explicit `COVERED` or `PENDING` status. The check must fail for an unlisted service/provider/package.
- [ ] Turn every unresolved item in `docs/audit/2026-09-10-live-catalog-warehouse-ux-audit.md` into an acceptance-ledger row with one terminal proof requirement. Do not mark a row complete from a page render, health check, or seeded row.
- [ ] Run the current offline gates and record failures as baseline gaps rather than weakening checks.
- [ ] Commit: `docs: establish request delivery acceptance ledger`

**Boundary test:** create a temporary unlisted service directory and confirm `tests/type-check/check.sh` fails with its exact path; remove it and confirm the check passes.

## Task 2: Make Process Intake and Activation Contract-Owned

**Files:**
- Modify: `services/contract/src/pillarmesh_contract_service/process_models.py`
- Modify: `services/contract/src/pillarmesh_contract_service/process_service.py`
- Modify: `services/contract/src/pillarmesh_contract_service/acquisition_lifecycle.py`
- Modify: `services/contract/src/pillarmesh_contract_service/source_observation.py`
- Modify: `services/contract/tests/test_process_service.py`
- Modify: `services/contract/tests/test_acquisition_lifecycle.py`
- Modify: `services/contract/tests/test_source_observation.py`
- Modify: `apps/console/server/src/pillarmesh_console/governed_adapters.py`
- Modify: `apps/console/server/src/pillarmesh_console/routes/commands.py`
- Modify: `apps/console/server/tests/test_governed_commands.py`
- Modify: `apps/console/web/src/features/setup/process-stage.tsx`
- Modify: `apps/console/web/src/features/setup/setup-workbench.test.tsx`

Keep the existing strict UTF-8 Markdown narrative, JSON manifest, version allocator, source-byte digest, lifecycle repository, and source-observation repository. Wire those existing authorities through the governed console command, and move the activated acquisition contract model out of runtime into contract service without changing the meaning of its current fields. Remove the PDF/DOCX promise from this path until a real parsing service is designed and operated. The request-management command references the resulting contract revision; it does not store or reinterpret the package.

```python
class ActivatedAcquisitionContract(ArtifactModel):
    schema_version: Literal["2"] = "2"
    tenant_id: str = Field(min_length=1)
    contract_ref: str = Field(min_length=1)
    contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    process_package_ref: ArtifactReference
    product_intent_ref: ArtifactReference
    destination_product_ref: str = Field(min_length=1)
    source_binding_ref: str = Field(min_length=1)
    source_binding_revision: int = Field(ge=1)
    credential_revision: int = Field(ge=1)
    acknowledgement_consumer_ref: str = Field(min_length=1)
    capability_profile_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_observation_ref: str = Field(min_length=1)
    source_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    lifecycle_state: Literal["activated", "inactive"]
    acquisition_modes: tuple[AcquisitionMode, ...] = Field(min_length=1)
    object_schemas: tuple[AcquisitionObjectSchema, ...] = Field(min_length=1)
    record_ceiling: int = Field(gt=0)
    encoded_byte_ceiling: int = Field(gt=0)
    activated_by: str = Field(min_length=1)
    activated_at: datetime
```

- [ ] Add a failing test proving an activation cannot reference an unvalidated source binding, an unapproved process revision, or a different tenant.
- [ ] Add a replay test proving the same idempotency key returns byte-identical activation data and does not create a second revision.
- [ ] Implement one transaction that writes the activated contract and every validation record needed when that activation is replayed.
- [ ] Expose create/read/list commands and projections through the console server's strict schema and governed adapters, with optimistic revision checks.
- [ ] Change the console package form to accept `.md` plus a strict manifest, explain the accepted format, and show field-level validation errors.
- [ ] Confirm the focused service and UI tests fail on the old behavior, then pass after the implementation.
- [ ] Commit: `feat(contract): own acquisition activation`

**Failure test:** submit a valid contract using a source binding owned by another tenant; return a typed denial without revealing whether that binding exists.

## Task 3: Convert an Approved Request into a Typed Product Intent

**Files:**
- Create: `services/request-management/src/pillarmesh_request_management/product_intent.py`
- Modify: `services/request-management/src/pillarmesh_request_management/models.py`
- Modify: `services/request-management/src/pillarmesh_request_management/service.py`
- Create: `services/request-management/tests/test_product_intent.py`
- Modify: `apps/console/server/src/pillarmesh_console/contracts.py`
- Modify: `apps/console/server/src/pillarmesh_console/governed_backend.py`
- Modify: `apps/console/server/src/pillarmesh_console/routes/commands.py`
- Modify: `apps/console/server/tests/test_governed_commands.py`
- Modify: `apps/console/web/src/features/inbox/decision-workspace.tsx`
- Modify: `apps/console/web/src/features/inbox/decision-workspace.test.tsx`

Use a closed vocabulary for the approved intent. An AI-assisted interpreter may populate a candidate, but an architect must approve the exact typed intent before activation.

```python
class ProductIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: RequestId
    title: NonEmptyText
    business_outcome: NonEmptyText
    grain: Grain
    measures: tuple[MeasureIntent, ...]
    dimensions: tuple[DimensionIntent, ...]
    filters: tuple[FilterIntent, ...]
    freshness: FreshnessObjective
    delivery: DeliveryIntent
```

- [ ] Add tests for an answerable request, a request missing grain, an unknown metric, an impossible freshness target, and cross-tenant approval.
- [ ] Implement deterministic validation that produces either `ApprovedProductIntent` or an attributable clarification/`No Valid Plan` decision.
- [ ] Persist the exact approved intent revision and its digest with the request event. A later request edit must require a new intent revision.
- [ ] Add a console review panel showing source coverage, grain, measures, dimensions, filters, freshness, outputs, and unresolved constraints before approval.
- [ ] Prevent “approve” when required fields or source authorization are unresolved.
- [ ] Commit: `feat(requests): approve typed product intent`

**Boundary test:** ask for hourly freshness from a source contract whose minimum supported interval is daily; the service must return `No Valid Plan` with the source constraint and must not activate work.

## Task 4: Publish Source Observations and Compose Live PostgreSQL and Stripe Acquisition

**Files:**
- Modify: `services/contract/src/pillarmesh_contract_service/source_observation.py`
- Modify: `services/contract/src/pillarmesh_contract_service/service.py`
- Modify: `services/runtime/src/pillarmesh_runtime/acquisition.py`
- Create: `services/runtime/src/pillarmesh_runtime/acquisition_application.py`
- Create: `services/runtime/tests/test_acquisition_application.py`
- Modify: `providers/postgresql/src/pillarmesh_provider_postgresql/acquisition.py`
- Modify: `providers/stripe/src/pillarmesh_provider_stripe/acquisition.py`
- Modify: `providers/stripe/tests/test_snapshot.py`
- Modify: `providers/stripe/tests/test_events.py`
- Modify: `providers/stripe/tests/test_reconciliation.py`
- Modify: `apps/console/server/src/pillarmesh_console/governed_adapters.py`
- Modify: `apps/console/server/src/pillarmesh_console/routes/commands.py`
- Create: `tests/emulators/governed-stack/compose.yaml`
- Create: `tests/emulators/governed-stack/run.sh`

`AcquisitionSourceObservation` is a persisted, served contract fact. It records discoverable shape and bounded cursor capabilities without copying credentials or unrestricted samples. The runtime application resolves an activated contract, obtains a scoped binding, invokes the existing `AcquisitionRunner`, and publishes sanitized progress.

```python
def prepare_acquisition(
    *,
    intent: AcquisitionIntent,
    runner: AcquisitionRunner,
) -> AcquisitionPreparationResult:
    return runner.prepare(intent)
```

- [ ] Add a failing contract test for source observations that contain credentials, unrestricted sample values, or an object outside the binding scope.
- [ ] Add a failing application test proving an unactivated or superseded contract cannot run.
- [ ] Add a provider failure test for availability, throttling, authentication, and statement rejection; assert each becomes the correct `ProviderError` classification.
- [ ] Extend the existing durable source-observation repository with tenant-scoped list/current projections and optimistic replacement; do not introduce a second observation model.
- [ ] Compose the contract repository, connection broker, state store, artifact store, evidence store, and PostgreSQL and Stripe acquisition providers in the governed-local runtime.
- [ ] Add `run now` as an idempotent command keyed by contract revision and trigger window.
- [ ] Perform a live local PostgreSQL EXTRACT using rows inserted during the test and trace the exact run to an immutable staged segment. Do not advance the checkpoint yet.
- [ ] Perform a live Stripe test-mode EXTRACT using objects created for the run, reconcile the bounded snapshot with the subscribed event interval, and trace the exact run to immutable staged segments. Do not treat a mocked HTTP response as live evidence.
- [ ] Commit: `feat(runtime): compose governed acquisition`

**Recovery test:** kill the provider after the staged segment is written but before completion is recorded; retry must reuse or safely replace the uncommitted segment without advancing the checkpoint.

## Task 5: Implement LAND for PostgreSQL and ClickHouse

**Files:**
- Create: `packages/provider-sdk/src/pillarmesh_provider_sdk/destination.py`
- Create: `services/runtime/src/pillarmesh_runtime/landing.py`
- Create: `services/runtime/src/pillarmesh_runtime/generation_ledger.py`
- Create: `services/runtime/tests/test_landing.py`
- Create: `providers/postgresql/src/pillarmesh_provider_postgresql/destination.py`
- Create: `providers/postgresql/tests/test_destination_conformance.py`
- Create: `providers/clickhouse/src/pillarmesh_provider_clickhouse/destination.py`
- Create: `providers/clickhouse/tests/test_destination_conformance.py`
- Create: `tests/conformance/test_destination_provider.py`

```python
class DestinationProvider(Protocol):
    async def land(
        self,
        *,
        segment: StagedSegment,
        target: RawGenerationTarget,
        idempotency_key: IdempotencyKey,
    ) -> LandReceipt: ...

    async def inspect_commit(self, *, receipt: LandReceipt) -> CommitObservation: ...
```

- [ ] Write the shared conformance suite first: successful land, duplicate replay, conflicting replay, empty segment, schema mismatch, transient failure, and ambiguous commit outcome.
- [ ] Implement a generation ledger whose unique key includes tenant, contract revision, trigger window, segment digest, and destination binding.
- [ ] Implement PostgreSQL LAND in one transaction: create/load the raw generation, record row count and digest, and commit the receipt atomically.
- [ ] Implement ClickHouse LAND using a deterministic insert token and a separately reconciled commit receipt.
- [ ] Advance the source checkpoint only after `inspect_commit` proves the exact segment digest is committed.
- [ ] Add fault injection between each durable step and verify replay never produces a second committed generation.
- [ ] Run the same conformance fixture against both providers.
- [ ] Commit: `feat(runtime): land staged generations`

**Failure test:** simulate a timeout after the provider commits but before PillarMesh records success; reconciliation must discover the existing commit and must not insert the segment again.

## Task 6: Compile Typed Semantics into Restricted Provider SQL

**Files:**
- Modify: `packages/iir/src/pillarmesh_iir/models.py`
- Create: `packages/iir/src/pillarmesh_iir/product_models.py`
- Modify: `services/compiler/src/pillarmesh_compiler/models.py`
- Modify: `services/compiler/src/pillarmesh_compiler/compiler.py`
- Create: `services/compiler/src/pillarmesh_compiler/restricted_sql.py`
- Create: `services/compiler/src/pillarmesh_compiler/postgresql_sql.py`
- Create: `services/compiler/src/pillarmesh_compiler/clickhouse_sql.py`
- Create: `services/compiler/tests/test_product_compilation.py`
- Create: `services/compiler/tests/test_restricted_sql.py`
- Create: `tests/conformance/test_compiler_provider_pairs.py`

Introduce schema version 2 rather than changing the meaning of M0 version 1 artifacts. Model expressions as a closed AST; never accept SQL text from a request, model, or console.

```python
type ScalarExpression = (
    ColumnReference
    | LiteralValue
    | ArithmeticExpression
    | ComparisonExpression
    | CaseExpression
    | ApprovedFunctionCall
)

type RelationalOperation = (
    ProjectOperation | FilterOperation | JoinOperation | AggregateOperation | DeduplicateOperation
)
```

- [ ] Add tests that reject unknown functions, unbounded cross joins, missing join keys, identifier injection, nondeterministic functions, undeclared columns, and provider-unsupported types.
- [ ] Add a canonical serializer and prove identical approved inputs/compiler versions produce byte-identical IIR, physical plan, execution graph, and digests.
- [ ] Implement semantic validation before physical planning: source columns, types, grain, join cardinality, null policy, measures, and freshness feasibility.
- [ ] Keep every emittable construct in a versioned allowlist under `services/compiler/legality/`. Each construct needs per-engine semantics pinned for PostgreSQL and ClickHouse across D1-D8, a proof note, positive and negative per-engine fixtures, and independent review (ADR-0004). A construct outside the allowlist returns `No Valid Plan` naming the missing construct.
- [ ] Define the read-only query class of addendum §12.4 in the same allowlist. It admits consumption objects only, metric-pinned aggregates, approved dimensions, closed-domain filters, bounded time windows, compiled small-group suppression, ordering, and a row limit.
- [ ] Implement provider-neutral physical operators, then separate PostgreSQL and ClickHouse emitters with bound literals and quoted validated identifiers.
- [ ] Produce `No Valid Plan` with attributable constraints when no admitted provider plan exists. Do not fall back to weaker semantics.
- [ ] Run every previously admitted provider-pair fixture plus new positive and negative fixtures.
- [ ] Commit: `feat(compiler): compile restricted product SQL`

**Security test:** put quotes, comment markers, wildcard characters, and Unicode confusables into every caller-controlled identifier field; parsing or validation must reject them before SQL emission.

## Task 7: Execute TRANSFORM and Publish a Data Product

**Files:**
- Create: `services/runtime/src/pillarmesh_runtime/transformation.py`
- Create: `services/runtime/src/pillarmesh_runtime/product_materialization.py`
- Create: `services/runtime/tests/test_transformation.py`
- Create: `services/dbt-adapter/pyproject.toml`
- Create: `services/dbt-adapter/src/pillarmesh_dbt_adapter/__init__.py`
- Create: `services/dbt-adapter/src/pillarmesh_dbt_adapter/invocation.py`
- Create: `services/dbt-adapter/src/pillarmesh_dbt_adapter/py.typed`
- Create: `services/dbt-adapter/tests/test_invocation.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/type-check/check.sh`
- Modify: `services/catalog-control/src/pillarmesh_catalog_control/service.py`
- Modify: `services/catalog-control/tests/test_service.py`
- Modify: `providers/openmetadata/src/pillarmesh_provider_openmetadata/publication.py`
- Create: `tests/integration/test_product_materialization.py`

```python
class ProductMaterializationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    product_id: DataProductId
    product_revision: PositiveInt
    input_generation_digests: tuple[Digest, ...]
    plan_digest: Digest
    output_schema_digest: Digest
    output_row_count: NonNegativeInt
    provider_commit_reference: ProviderCommitReference
```

- [ ] Add tests for successful materialization, schema drift, input-generation substitution, row-count reconciliation failure, transient provider failure, and replay after an ambiguous commit.
- [ ] Run compiled models through `services/dbt-adapter` with a version-pinned dbt invocation, as ADR-0004 and addendum §12.1 require. Record manifest, test, and lineage observations in the receipt. Execute only compiler-signed models against contract-scoped schemas, and reject a plan whose contract, provider, or input digest differs.
- [ ] Materialize to a versioned product object, validate the observed output schema and checks, and atomically switch the stable consumption view only after validation passes. Keep each product generation addressable for the longest answer-scope-policy retention, so a governed answer can pin its generation (addendum §12.4).
- [ ] Publish OpenMetadata only after warehouse commit. On catalog failure, retain the product as usable-but-publication-pending and retry publication from the durable receipt.
- [ ] Reconcile OpenMetadata ownership, description, columns, lineage, freshness, and product revision from the owning contract and materialization receipt.
- [ ] Commit: `feat(runtime): materialize governed data products`

**Failure test:** force a catalog failure after warehouse success; the retry must publish the existing product revision and must not rerun the transform.

## Task 8: Add Durable Run Intent, Scheduling, Replay, and Recovery

**Files:**
- Create: `services/state/src/pillarmesh_state/run_models.py`
- Create: `services/state/src/pillarmesh_state/run_repository.py`
- Create: `services/state/src/pillarmesh_state/run_service.py`
- Create: `services/state/tests/test_run_service.py`
- Create: `services/trigger/pyproject.toml`
- Create: `services/trigger/src/pillarmesh_trigger/__init__.py`
- Create: `services/trigger/src/pillarmesh_trigger/policy.py`
- Create: `services/trigger/src/pillarmesh_trigger/materialization.py`
- Create: `services/trigger/src/pillarmesh_trigger/py.typed`
- Create: `services/trigger/tests/test_materialization.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/type-check/check.sh`
- Modify: `services/runtime/src/pillarmesh_runtime/runtime.py`
- Create: `services/runtime/tests/test_run_recovery.py`
- Modify: `apps/console/web/src/features/runs/runs-page.tsx`
- Modify: `apps/console/web/src/features/runs/runs-page.test.tsx`

Use the repository-defined state control loop, not a general workflow scheduler. A run has a stable identity and a sequence of state-owned attempts. `services/trigger` owns trigger policies and scheduled-window identities (addendum §15). State owns run identity, attempts, leases, and recovery.

```python
class RunIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_id: ContractId
    contract_revision: PositiveInt
    plan_digest: Digest
    trigger_window: TriggerWindow
    reason: Literal["scheduled", "run_now", "backfill", "retry"]
```

- [ ] Add tests for concurrent claim, lease expiry, duplicate trigger, bounded backfill, missed window, transient retry, permanent failure, superseded contract, and operator cancellation.
- [ ] Persist one run per canonical intent and one immutable record per attempt. A retry changes the attempt, not the run identity.
- [ ] Implement daily triggers, idempotent run-now, and bounded backfill in `services/trigger`. Materialize run intents keyed by `digest(contract_version, trigger_policy_version, scheduled_window)`, and apply the addendum §15 overlap and misfire policies. Reject overlapping or unbounded backfill requests.
- [ ] Resume from the last proven durable boundary: EXTRACT segment, LAND receipt, TRANSFORM receipt, catalog publication, query result, or dashboard receipt.
- [ ] Show stages, attempts, correlation IDs, last durable boundary, classified failure, and allowed operator action in the console.
- [ ] Commit: `feat(state): manage governed run lifecycle`

**Concurrency test:** start two workers against one run intent; exactly one obtains the valid epoch, and the loser cannot write a terminal transition.

## Task 9: Compute Governed Answers from Warehouse Facts

**Files:**
- Modify: `services/warehouse-control/src/pillarmesh_warehouse_control/models.py`
- Modify: `services/warehouse-control/src/pillarmesh_warehouse_control/secrets.py`
- Modify: `providers/postgresql/src/pillarmesh_provider_postgresql/warehouse.py`
- Modify: `providers/clickhouse/src/pillarmesh_provider_clickhouse/warehouse.py`
- Create: `services/request-management/src/pillarmesh_request_management/answer_models.py`
- Create: `services/request-management/src/pillarmesh_request_management/answer_policy.py`
- Create: `services/request-management/src/pillarmesh_request_management/answer_validation.py`
- Create: `services/request-management/src/pillarmesh_request_management/answer_service.py`
- Modify: `services/request-management/src/pillarmesh_request_management/service.py`
- Modify: `services/request-management/src/pillarmesh_request_management/fulfillment_models.py`
- Modify: `services/request-management/src/pillarmesh_request_management/fulfillment_service.py`
- Create: `services/request-management/tests/test_answer_validation.py`
- Create: `services/request-management/tests/test_answer_service.py`
- Create: `services/compiler/src/pillarmesh_compiler/query_compiler.py`
- Create: `services/compiler/tests/test_query_compiler.py`
- Create: `services/runtime/src/pillarmesh_runtime/query_execution.py`
- Create: `services/runtime/src/pillarmesh_runtime/result_store.py`
- Create: `services/runtime/tests/test_query_execution.py`
- Modify: `apps/console/server/src/pillarmesh_console/governed_backend.py`
- Modify: `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`
- Modify: `tests/conformance/test_specification_conformance.py`

Implement addendum §12.4 and §13.8 in order:

1. A question becomes a candidate `AnswerQuestionIntent`.
2. Deterministic validation binds it to the approved semantic version under an `AnswerScopePolicy`.
3. The compiler emits a `GovernedQueryPlan` in the Task 6 query class.
4. Policy admission records a `PolicyAdmissionReceipt`.
5. The runtime executes the plan through `answer_runtime` and writes an `AnswerExecutionReceipt`.
6. Request management delivers a `GovernedAnswer` only after verification.

Keep these as five logical commits: warehouse principal provisioning; request policy and deterministic validation; query-plan compilation; runtime execution and result storage; and verified request delivery. Run each component's focused tests before moving to the next boundary.

Result rows live in the tenant data plane's result store. Evidence holds only digests, counts, provenance, and the access-controlled result reference.

```python
class AnswerQuestionIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    intent_id: IntentId
    tenant_id: TenantId
    request_id: RequestId
    request_revision: PositiveInt
    question_digest: Digest
    intent_kind: Literal["definition", "metric_value"]
    metric_refs: tuple[MetricReference, ...]
    dimension_refs: tuple[DimensionReference, ...]
    filters: tuple[FilterCandidate, ...]
    time_window: TimeWindowCandidate | None
    ordering: tuple[OrderCandidate, ...]
    row_limit: PositiveInt
    interpreter: Literal["form", "model"]
    interpreter_ref: NonEmptyText
    created_at: AwareDatetime
```

- [ ] Add `answer_runtime` as one change covering five places: the addendum §18.1 class list, `WarehousePrincipalClass`, the secret inventory, PostgreSQL and ClickHouse provisioning, and positive and denial probes. The principal holds no write privilege. Remove the "planned" note from §18.1.
- [ ] Commit: `feat(warehouse): add answer runtime principal`
- [ ] Write a failing test for today's defect: a `metric_value` question delivered with a definition string and no execution receipt. Delivery must reject it.
- [ ] Add a validation test for every reason code in the addendum §13.8 check table, including one question that fails several checks to prove precedence:
  - admitted;
  - ambiguous alias;
  - unknown candidate reference, with the candidate discarded;
  - filter value outside the closed domain;
  - requester not entitled;
  - product not answer-enabled;
  - out-of-scope metric;
  - time window exceeded;
  - stale product;
  - quality blocked;
  - conflicting authority.

  Supply candidates through the injected interpreter port with a scripted interpreter. No test calls a model.
- [ ] Implement the `AnswerScopePolicy` lifecycle with its approval matrix, revision supersession, expiry, `restatement_confirmation`, `agent_access`, and `model_disclosure`.
- [ ] Commit: `feat(requests): validate governed answer intents`
- [ ] Compile a plan from a `metric_value` validation that is `admitted` or `review_required`. Use bound parameters, small-group suppression over `disclosure_entity` compiled into the statement, and the engine's pinned scan estimator. Route by estimate:
  - a missing estimate, one over `scan_ceiling`, or a recorded ceiling breach for the same statement digest becomes a per-question Plan 3B proposal;
  - one over `period_scan_budget` becomes a dependency on a policy revision that needs budget authority.
- [ ] Handle definition intents with no plan: the policy admission predicate marks its plan terms `not_applicable_for_definition`.
- [ ] Leave reviewed factual answers disabled until `StakeholderAnswerDraft` gains its planned plan-digest citation, which must be pinned in the addendum in the same change. Test that a reviewed factual proposal cannot reach `executing` without that citation.
- [ ] Commit: `feat(compiler): compile governed query plans`
- [ ] Implement the policy admission predicate term by term and record a `PolicyAdmissionReceipt`. In the same commit:
  - add the planned `investigating → executing` edge to the request transition table, allowed only for a request with that receipt;
  - add the edge to addendum §13.3.1; and
  - update its conformance pin.
- [ ] Execute through `answer_runtime` in an engine-enforced read-only session with a server-side timeout, row and byte ceilings, and cancellation. Pin the plan's product generation. Where the engine cannot address that generation, check the publication pointer before and after the statement and fail with `generation_unavailable`.
- [ ] Write the result snapshot to the data-plane result store with post-suppression rows, schema, digest, and `result_retention`.
- [ ] Commit: `feat(runtime): execute governed query plans`
- [ ] Before delivery, verify:
  - plan digest and generations;
  - the recomputed result digest;
  - ceilings and suppression;
  - freshness and quality dispositions;
  - the narrative check;
  - that the policy and entitlements are still valid.

  Write the `GovernedAnswer` only after the result is readable through the requester's authorization.
- [ ] Implement the narrative check. Discard any model narrative containing a number, date, or named entity absent from the verified result or cited metadata, and deliver the template narrative instead.
- [ ] Constrain Plan 3B: a factual `StakeholderAnswerDraft.answer_text` describes the computation. A reviewed factual answer still executes and verifies through this path before delivery.
- [ ] Pin every new artifact's fields and the policy admission predicate to addendum §13.8 in `tests/conformance/test_specification_conformance.py`.
- [ ] Commit: `feat(requests): deliver verified governed answers`

**Disclosure test:** set `minimum_group_size` to 5, then ask two questions. First, a metric filtered to one customer: it returns an empty result with `below_minimum_group_size`. Second, a breakdown where one group has three entities: that group is omitted, and the answer returns no total. The stored snapshot contains no suppressed value.

**Authorization test:** an authorized user obtains a result ID. A user from another tenant then requests its metadata, page, and download, and all three return the same non-enumerating denial. Separately, revoke the requester's entitlement between execution and verification: the request ends `failed`, and the result is never shown.

## Task 10: Present Result Datasets and Tables in the Console

**Files:**
- Create: `apps/console/web/src/features/results/result-page.tsx`
- Create: `apps/console/web/src/features/results/result-table.tsx`
- Create: `apps/console/web/src/features/results/result-provenance.tsx`
- Create: `apps/console/web/src/features/results/result-api.ts`
- Create: `apps/console/web/src/features/results/result-page.test.tsx`
- Create: `apps/console/e2e/request-result.spec.ts`
- Modify: `apps/console/web/src/routes/router.tsx`
- Modify: `apps/console/web/src/features/requests/my-requests.tsx`
- Modify: `apps/console/server/src/pillarmesh_console/contracts.py`
- Modify: `apps/console/server/src/pillarmesh_console/routes/read.py`
- Modify: `apps/console/schema/console-api-v1.json`

- [ ] Start with a failing component test for loading, empty, completed, expired, denied, and failed result states.
- [ ] Add a result page whose primary content is the user-owned request title, concise answer, freshness, row count, and table. Keep internal IDs behind a copyable “Technical details” disclosure.
- [ ] Fetch server-paginated rows; do not load the entire result in the browser. Preserve backend column order and render types without lossy string coercion.
- [ ] Add sorting/filter controls only when the API returns allowed operations for that field. A UI control cannot create query authority.
- [ ] Add an authorized CSV download endpoint that streams from the result store, rechecks access, records a download receipt, and uses a safe filename derived from the request title.
- [ ] Link the result to its product, catalog page, dashboard, contract revision, and run provenance using human labels.
- [ ] Show a governed answer's narrative and values with their metric version, generation, as-of time, freshness, quality limitations, and lineage. Only the architect's "Technical details" disclosure shows the statement text, validation outcome, and execution receipt.
- [ ] Add an answer scope policy review screen for the architect. It shows scope, ceilings, `agent_access`, `model_disclosure`, required approvers, and a preview of the questions the policy would admit and refuse.
- [ ] Add Playwright coverage from a newly submitted request through the delivered table, including an empty result and an expired-access result. Also cover an in-scope question answered without per-question approval and an out-of-scope question routed to the inbox.
- [ ] Commit: `feat(console): present governed result tables`

**Usability test:** the default success view must contain no raw request ID, run ID, artifact digest, revision sentence, enum token, or provider commit reference.

## Task 11: Add BI Control and Superset Dashboard Lifecycle

**Files:**
- Create: `docs/architecture/decisions/ADR-0006-bi-control.md`
- Modify: `docs/architecture/repository-layout.md`
- Modify: `tests/repository-structure/test.sh`
- Create: `services/bi-control/pyproject.toml`
- Create: `services/bi-control/src/pillarmesh_bi_control/__init__.py`
- Create: `services/bi-control/src/pillarmesh_bi_control/models.py`
- Create: `services/bi-control/src/pillarmesh_bi_control/service.py`
- Create: `services/bi-control/src/pillarmesh_bi_control/py.typed`
- Create: `services/bi-control/tests/test_service.py`
- Create: `packages/provider-sdk/src/pillarmesh_provider_sdk/bi.py`
- Create: `providers/superset/src/pillarmesh_provider_superset/provider.py`
- Create: `providers/superset/src/pillarmesh_provider_superset/__init__.py`
- Create: `providers/superset/src/pillarmesh_provider_superset/py.typed`
- Create: `providers/superset/tests/test_provider_conformance.py`
- Create: `providers/superset/pyproject.toml`
- Modify: `providers/README.md`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/type-check/check.sh`
- Modify: `tests/emulators/governed-stack/compose.yaml`

```python
class DashboardContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dashboard_id: DashboardId
    version: PositiveInt
    owner: PrincipalReference
    audience: tuple[PrincipalReference, ...]
    data_product_versions: tuple[DataProductVersionReference, ...]
    metric_versions: tuple[MetricVersionReference, ...]
    dimensions: tuple[DimensionReference, ...]
    filters: tuple[DashboardFilter, ...]
    visual_intents: tuple[VisualIntent, ...]
    drill_paths: tuple[DrillPath, ...]
    freshness_requirement: FreshnessObjective
    access_policy: AccessPolicyReference
    report_delivery_policy: ReportDeliveryPolicy | None
    acceptance_tests: tuple[DashboardAcceptanceTest, ...]
    lifecycle_state: DashboardLifecycleState
```

Field names and order follow addendum §16.3. Add a conformance test that pins them together.

- [ ] Write and approve the ADR defining BI-control authority, dependencies, lifecycle, and why provider effects do not belong in the console or request service.
- [ ] Add service and provider conformance tests for create, no-op replay, update, archive, transient failure, conflicting external mutation, and missing dataset.
- [ ] Implement deterministic stable external keys from tenant, dashboard ID, and version; never match dashboards by display title.
- [ ] Provision the Superset database connection through a secret reference, dataset over the stable consumption object, charts from approved semantic fields, and dashboard from the signed contract.
- [ ] Persist desired state and provider receipt before exposing the dashboard link. Reconcile drift without overwriting unrecognized external changes; surface conflict for operator action.
- [ ] Put Superset on the governed-local network, configure health checks, and prove the application can invoke it after cold start.
- [ ] Add a console dashboard card with title, freshness, access state, and deep link. Hide internal Superset object IDs.
- [ ] Commit: `feat(bi): manage Superset dashboards`

**Replay test:** lose the local response after Superset creates every object; the retry must find them by stable external keys and must not create duplicates.

## Task 12: Implement Access Grant, Expiry, and Revocation

**Files:**
- Create: `docs/architecture/decisions/ADR-0007-access-control.md`
- Modify: `docs/architecture/repository-layout.md`
- Modify: `tests/repository-structure/test.sh`
- Create: `services/access-control/pyproject.toml`
- Create: `services/access-control/src/pillarmesh_access_control/__init__.py`
- Create: `services/access-control/src/pillarmesh_access_control/models.py`
- Create: `services/access-control/src/pillarmesh_access_control/service.py`
- Create: `services/access-control/src/pillarmesh_access_control/py.typed`
- Create: `services/access-control/tests/test_service.py`
- Create: `packages/provider-sdk/src/pillarmesh_provider_sdk/access.py`
- Create: `providers/postgresql/src/pillarmesh_provider_postgresql/access.py`
- Create: `providers/clickhouse/src/pillarmesh_provider_clickhouse/access.py`
- Create: `providers/superset/src/pillarmesh_provider_superset/access.py`
- Create: `tests/integration/test_access_lifecycle.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/type-check/check.sh`

```python
class AccessGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: GrantId
    tenant_id: TenantId
    request_id: RequestId
    subject: SubjectReference
    purpose: NonEmptyText
    data_product_version: DataProductVersionReference
    fields: tuple[FieldReference, ...]
    classifications: tuple[Classification, ...]
    access_mode: AccessMode
    permissions: tuple[Literal["view", "query", "download"], ...]
    effective_at: AwareDatetime
    expires_at: AwareDatetime
    policy_revision: PositiveInt
    admission_receipt_ref: ArtifactReference
```

The grant carries everything addendum §13.6 binds to an access request: requester, purpose, product, fields, classification, mode, duration, and approving authority, through its admission receipt.

- [ ] Write and approve the ADR for policy authority, provider adapters, expiry, revocation, and denial behavior. The ADR must also resolve the boundary with `services/runtime`, which the repository layout currently assigns grants, and with warehouse-control principal provisioning.
- [ ] Add tests for approval, apply, replay, partial provider application, expiry, manual revocation, superseded policy, and clock skew.
- [ ] Require an approved request-management access proposal before creating a grant. Persist the grant before applying provider effects.
- [ ] Apply least-privilege roles to the consumption schema/result endpoint/Superset dashboard and record one receipt per effect.
- [ ] Drive expiry from state-owned time-based intent. A grant is unusable once expired even if provider cleanup is pending; reconciliation must finish revocation.
- [ ] Recheck the authoritative grant on every result page, download, query, and dashboard-link issuance.
- [ ] Show pending, active, expired, revocation-pending, revoked, and failed states in plain language.
- [ ] Commit: `feat(access): enforce governed grant lifecycle`

**Fault test:** make Superset revocation fail after warehouse revocation succeeds; user-facing authorization must still deny access, and reconciliation must retry only the missing Superset effect.

## Task 13: Add Operational Visibility and Safe Recovery Actions

**Files:**
- Create: `apps/console/web/src/features/operations/operations-page.tsx`
- Create: `apps/console/web/src/features/operations/recovery-action.tsx`
- Create: `apps/console/web/src/features/operations/operations-page.test.tsx`
- Modify: `services/state/src/pillarmesh_state/run_service.py`
- Create: `services/state/src/pillarmesh_state/incident_models.py`
- Create: `services/state/tests/test_incident_models.py`
- Create: `docs/runbooks/request-to-data-product.md`

- [ ] Define typed incidents for stuck lease, source unavailable, checkpoint conflict, schema drift, no valid plan, transform rejection, catalog pending, query failure, dashboard drift, and revocation pending.
- [ ] Expose only state-valid actions: retry transient attempt, cancel unstarted work, approve a compatible replan, reconcile an external effect, or supersede a contract.
- [ ] Require optimistic revision and actor reason for every recovery command. Record it in evidence without sensitive payloads.
- [ ] Display the last successful stage, exact failed stage, classification, user impact, next automatic action, and operator option.
- [ ] Add the runbook with diagnosis queries, log correlation, replay rules, provider-specific ambiguity handling, and rollback limits.
- [ ] Commit: `feat(console): expose governed operations recovery`

**State test:** attempt a generic retry after a permanent compiler rejection; reject the command and preserve the terminal `No Valid Plan` decision.

## Task 14: Project the Context Graph and Impact Analysis

**Files:**
- Create: `services/knowledge-graph/pyproject.toml`
- Create: `services/knowledge-graph/src/pillarmesh_knowledge_graph/__init__.py`
- Create: `services/knowledge-graph/src/pillarmesh_knowledge_graph/projection.py`
- Create: `services/knowledge-graph/src/pillarmesh_knowledge_graph/impact.py`
- Create: `services/knowledge-graph/src/pillarmesh_knowledge_graph/py.typed`
- Create: `services/knowledge-graph/tests/test_projection.py`
- Create: `services/knowledge-graph/tests/test_impact.py`
- Modify: `services/request-management/src/pillarmesh_request_management/service.py`
- Create: `apps/console/web/src/features/impact/impact-panel.tsx`
- Create: `apps/console/web/src/features/impact/impact-panel.test.tsx`
- Modify: `apps/console/web/src/features/inbox/decision-workspace.tsx`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/type-check/check.sh`

Implement addendum §9.5. The graph is a rebuildable projection and never holds authority.

- [ ] Add a projection test proving that the graph rebuilds byte-identically from authoritative records, and that every node and edge names its source record, digest, and validity.
- [ ] Add a traversal test for each `subject_kind`: source drift, metric-version change, contract supersession, generation failure, policy change, grant change, and retirement.
- [ ] Traverse under service authority and filter only the presented result. Test that a restricted reader sees fewer assets while the derived approval requirements stay identical.
- [ ] Derive approval requirements only from validated edges. They add to the owning service's requirements and never remove one. Inferred edges yield advisory reviewers only.
- [ ] Bind `graph_snapshot_digest` in proposals. Before admission, re-derive requirements from the cited source records, and supersede the proposal when they changed.
- [ ] Show validated and possible impacts, affected owners, and added approvers in the decision workspace.
- [ ] Commit: `feat(knowledge-graph): analyze change impact`

**Rebuild test:** delete the graph store and rebuild it; every admission decision and derived requirement must be unchanged. Then change a metric version after a proposal binds its snapshot; admission must supersede the proposal.

## Task 15: Serve the Agent Interface

**Files:**
- Create: `services/context-exposure/pyproject.toml`
- Create: `services/context-exposure/src/pillarmesh_context_exposure/__init__.py`
- Create: `services/context-exposure/src/pillarmesh_context_exposure/tools.py`
- Create: `services/context-exposure/src/pillarmesh_context_exposure/delegation.py`
- Create: `services/context-exposure/src/pillarmesh_context_exposure/app.py`
- Create: `services/context-exposure/src/pillarmesh_context_exposure/cli.py`
- Create: `services/context-exposure/src/pillarmesh_context_exposure/settings.py`
- Create: `services/context-exposure/src/pillarmesh_context_exposure/mcp_server.py`
- Create: `services/context-exposure/src/pillarmesh_context_exposure/py.typed`
- Create: `services/context-exposure/tests/test_tools.py`
- Create: `services/context-exposure/tests/test_delegation.py`
- Create: `services/context-exposure/tests/test_cli.py`
- Create: `tests/end-to-end/test_agent_interface.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/type-check/check.sh`

Implement addendum §13.9 as a separate service. Use the same `mcp` dependency and host-neutral server pattern as `services/authoring-mcp`. The tools are `search_catalog`, `describe_metric`, `ask_question`, `reply_to_clarification`, `get_answer`, `explain_answer`, `get_impact`, and `list_my_requests`.

- [ ] Add a failing test for each tool proving that every call rechecks the delegating principal's current entitlements and the policy's `agent_access`.
- [ ] Delegate `ask_question` and `reply_to_clarification` to request management's native commands. Context exposure holds no request state.
- [ ] Record the principal and the agent client on every request and evidence record.
- [ ] Return the same provenance as the native answer. `explain_answer` never returns statement text.
- [ ] Deny every approval or admission attempt, even from a principal who holds that authority. Apply per-principal rate and scan ceilings.
- [ ] Treat tool arguments, catalog metadata, and result values as untrusted data. Add a test where a question and a catalog description both contain instructions, and assert that the only effect is a normal validated question.
- [ ] Commit: `feat(context-exposure): serve governed answers to agents`

**Revocation test:** revoke the delegation between two calls. The second call is denied and creates no request.

## Task 16: Prove the Complete Journey with Fresh Live Transactions

**Files:**
- Create: `tests/integration/test_governed_answer_live.py`
- Create: `tests/integration/test_request_to_product_postgresql_live.py`
- Create: `tests/integration/test_request_to_product_clickhouse_live.py`
- Create: `tests/integration/test_request_to_dashboard_live.py`
- Create: `tests/fault-injection/test_request_to_product_recovery.py`
- Create: `tests/acceptance/run_request_to_product.py`
- Create: `tests/acceptance/test_run_request_to_product.py`
- Create: `docs/request-to-product/setup.md`
- Create: `docs/request-to-product/acceptance-run.md`
- Create: `docs/request-to-product/teardown.md`
- Modify: `docs/delivery/request-to-data-product-acceptance.md`

- [ ] Extend governed-local setup to create dedicated least-privilege source, runtime, product-reader, Superset, and test-user roles. Verify permitted and denied operations.
- [ ] For PostgreSQL destination, insert a unique fresh source cohort, submit a fresh request, approve its exact intent, run acquisition, and materialize the product. Then:
  - ask an in-scope question under an answer scope policy and reconcile its values to the cohort;
  - ask the same question through the agent interface and compare result digests;
  - attempt approval through the agent interface and verify that no approval or lifecycle transition is recorded;
  - revoke the delegation and verify that the next agent call is denied without creating or advancing a request;
  - run each refused-question case from addendum §20.8 step 13;
  - propose a metric-version change and check its impact analysis;
  - open and download the result, provision the dashboard, expire access, and verify denial.
- [ ] Repeat the same semantic fixture with ClickHouse as destination. Compare canonical product schemas and expected business results, allowing only declared provider representation differences.
- [ ] Capture sanitized correlation evidence for request revision, contract revision, staged segment digest, committed generation, plan digest, materialization receipt, catalog entity, answer validation, policy admission, query plan digest, execution receipt, result digest, dashboard receipt, and grant receipts.
- [ ] Run fault injection at every durable boundary and verify recovery from the last proven state without duplicate effects.
- [ ] Restart each scale-to-zero or cold service and actively invoke it; health-at-rest is insufficient.
- [ ] Verify backup and restore of contracts, state, result metadata, and provider receipts. Reconcile restored state against warehouse, OpenMetadata, and Superset.
- [ ] Run the full offline gates, live suites, Playwright journey, provider conformance, type inventory, and repository structure validation.
- [ ] Have an independent reviewer inspect legality changes, negative fixtures, mutation survivors, provider-pair regressions, access boundaries, and the sanitized live evidence.
- [ ] Mark an acceptance-ledger row complete only with its fresh transaction identifier and terminal evidence reference.
- [ ] Commit: `test: prove request-to-data-product delivery`

**Terminal acceptance:** a reviewer starts with a new business request and no pre-created product, then reaches a correct table and dashboard whose values reconcile to the newly inserted source cohort. An in-scope question, asked natively and through the agent interface, returns the same reconciled values with policy admission visible in its history. After grant expiry, the same user cannot query, download, or obtain a usable dashboard link.

## Task 17 (post-MVP): Run Scouts

**Files:**
- Create: `services/request-management/src/pillarmesh_request_management/scout_models.py`
- Create: `services/request-management/src/pillarmesh_request_management/scout_service.py`
- Create: `services/request-management/tests/test_scout_service.py`
- Modify: `services/trigger/src/pillarmesh_trigger/materialization.py`
- Modify: `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`
- Modify: `docs/architecture/repository-layout.md`
- Create: `apps/console/web/src/features/scouts/scout-brief.tsx`
- Create: `apps/console/web/src/features/scouts/scout-brief.test.tsx`

Start only after Gate C. Implement addendum §17.1.

- [ ] Extend addendum §15 and the trigger service's repository-layout row so the trigger service may materialize scout run intents, then implement that.
- [ ] Add tests for each condition type: a threshold, a change against the prior run, and a change against the same period last cycle. Evaluate them only over verified results.
- [ ] Execute each watched intent as a governed answer under the scout's policy, intersected with the owner's entitlements at run time.
- [ ] Suppress duplicate findings inside the deduplication window, and record each suppression.
- [ ] Suspend the scout when its owner loses an entitlement or its policy expires. Never run it with narrower scope.
- [ ] Deliver a brief, or an inbox request with an impact analysis. Apply the narrative check to any model-drafted brief.
- [ ] Commit: `feat(requests): run scouts`

**Noise test:** meet the same condition on three consecutive runs inside the window. Exactly one brief is delivered, and two suppressions are recorded.

## Required Verification at Every Implementation Commit

Run the focused failing test before the implementation and retain the failure reason in the task notes. After the focused test passes, run the affected package/component checks. Before each milestone merge, run:

```sh
uv sync --locked --all-packages
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
```

When a change implements an artifact, principal class, or table that the addendum defines, update the addendum in the same change. Add or extend its pin in `tests/conformance/test_specification_conformance.py`, and run that test. For `answer_runtime`, that one change also covers the §18.1 class list, `WarehousePrincipalClass`, the secret inventory, provisioning, and the positive and denial probes.

For console changes, also run the repository-defined package manager equivalents of type-check, unit tests, production build, and the relevant Playwright spec. For authorization, signature, recovery, and legality branches, run focused mutation testing and inspect surviving mutations.

## Release Gates

- **Gate A, PostgreSQL internal alpha:** Tasks 1-10 are complete. One fresh request produces a PostgreSQL-backed table, and one in-scope question is answered from its warehouse facts under an answer scope policy. Dashboard, access, and the agent interface may stay disabled behind explicit capability flags.
- **Gate B, governed beta:** Tasks 11-15 are complete. Dashboard and access lifecycles, impact analysis, and the agent interface work in governed-local, and recovery actions are operator-visible.
- **Gate C, MVP release candidate:** Task 16 passes for PostgreSQL and ClickHouse with fresh transactions, independent review, backup/restore evidence, and no unresolved P0/P1 findings.
- **Gate D, post-MVP scouts:** Task 17 passes its deterministic condition, deduplication, and suspension tests with fresh transactions.

Do not call the product ready because individual services pass tests. Readiness requires the witnessed terminal journey, including correct business values, durable receipts, provider reconciliation, usable presentation, and post-expiry denial.

## Critical Path and Parallel Work

After Task 1, contract/request work (Tasks 2-3) can overlap with destination provider conformance (Task 5). Task 4 depends on activated contracts. Compiler work (Task 6) can begin once the typed intent shape in Task 3 is stable and can overlap with LAND. Transformation (Task 7) requires Tasks 5-6. State and trigger lifecycle (Task 8) should integrate each durable boundary as it lands. Governed answers (Tasks 9-10) require a materialized product and the query class from Task 6. BI and access (Tasks 11-12) can proceed in parallel once product and result contracts stabilize. The context graph (Task 14) can start once contracts, publications, and answers have durable records. The agent interface (Task 15) follows Task 9's answer contract. Operational UI (Task 13) and final acceptance (Task 16) close the MVP. Scouts (Task 17) follow MVP acceptance.

Recommended staffing is one engineer on contracts/compiler, one on runtime/providers/state, and one on console/BI/access/agent interface, with a separate reviewer for legality and live acceptance. Keep file ownership disjoint during parallel work and integrate at milestone gates.

## Risks That Can Change the Estimate

- ClickHouse type and transactional differences may add 2-4 weeks if the conformance fixtures expose semantic gaps.
- Superset stable-key reconciliation or embedded authorization limitations may add 2-3 weeks; resolve these with a provider spike before Task 11 implementation.
- A requirement to ingest PDF/DOCX business-process documents adds a separate parsing, malware scanning, and evidence workstream; it is intentionally outside this Markdown-plus-manifest MVP.
- Arbitrary natural-language SQL would require a different security and correctness design. This plan supports natural language only through a deterministically validated typed intent under an answer scope policy (addendum §13.8).
- Interpretation quality decides how often questions stop at clarification instead of being answered. Track the clarification and review rates per policy. A rising rate means the approved glossary aliases or policy scope are too narrow; it does not justify loosening validation.
- ClickHouse's pinned scan estimator (`EXPLAIN ESTIMATE`) works only for MergeTree-family reads. Consumption objects it cannot estimate fail closed to per-question review (addendum §12.4). Until publications use estimable tables, more ClickHouse questions may reach the inbox.
- A read-only answer or agent overlay on a customer-managed warehouse is out of scope. It needs a new product-boundary ADR (addendum §20.10).
- Production deployment, SSO, cloud secret management, high availability, and provider account procurement are environment programs beyond governed-local acceptance and must be estimated from the chosen production topology.
