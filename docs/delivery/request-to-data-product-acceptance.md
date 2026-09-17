# Request-to-data-product acceptance ledger

This ledger is the release truth for the request-to-data-product journey. It records
capability ownership, the strongest terminal behavior proved on the integrated baseline,
the implementation task that closes each gap, and the evidence required before changing a
status.

## Baseline and status rules

- Baseline: `c67a654` on 2026-09-11.
- Release target: **Gate A, PostgreSQL internal alpha**, per
  `docs/superpowers/plans/2026-09-15-mvp-completion.md`. The product SQL legality rule is to be
  activated for PostgreSQL only, on independent approval (ADR-0004 amendment 2026-09-15); nothing is
  activated yet.
- **Engine version of native live evidence.** The native PostgreSQL live journeys that start a
  cluster from local binaries (`_fresh_postgresql_cluster`, configured by
  `PILLARMESH_TEST_POSTGRES_BIN_DIR`) run on whatever PostgreSQL those binaries are -- 14.17 on the
  machine where this was checked on 2026-09-16 -- not the digest-pinned 18.6 image, and CI does not
  run them. They prove the behaviour on that engine only. Evidence that must hold for the pinned
  engine uses the Docker image instead, as the product SQL conformance, checked SUM evidence and
  compiled product journey do.
- **The addendum MVP is not this release and remains unpassed.** Addendum sections 20.2, 20.9,
  and 22 define the MVP as a two-engine portability proof requiring PostgreSQL and ClickHouse to
  produce equivalent canonical results. Gate A does not satisfy that and must not be described as
  the MVP. The addendum MVP is reached when ClickHouse activation completes.
- Stripe acquisition is out of Gate A scope; PostgreSQL source acquisition carries the first
  witnessed journey. Stripe returns with ClickHouse parity.
- Architecture prerequisite: the reviewed governed-answer and agent-access amendment from
  `9ea2413` is included on this implementation branch as `b4452e1`. It must still be reviewed
  and merged with this branch before Milestone 0 closes.
- `DELIVERED` means a fresh transaction reached its terminal success state through the
  product and the durable owning record was read back.
- `PARTIAL` means a real boundary or durable record exists, but the user journey cannot yet
  reach the required terminal state.
- `BLOCKED` means the owning transaction or required composition does not exist.
- Existing rows, fixtures, provider health, a rendered page, or an HTTP success response do
  not prove delivery. Every promotion to `DELIVERED` requires a new correlated transaction
  and sanitized evidence from every relevant hop.
- Interaction ideas in the governed-answer, agent-access, impact-analysis, and scout direction were
  inspired in part by embrasure.ai. PillarMesh owning services remain authoritative for contracts,
  approvals, legality, execution, and durable state; this note does not claim affiliation or shared
  implementation.

## Capability ledger

| Capability | Owning boundary | Current branch terminal behavior | Status | Plan task | Evidence required to close |
| --- | --- | --- | --- | --- | --- |
| Request title and intake | request-management | A supplied title and canonical request digest are validated, stored, and projected; invalid digests fail before identity allocation. | DELIVERED | 2 | Fresh titled request read back with the same canonical content; mismatch denied with no durable request. |
| Clarification, conversation, and revision conflicts | request-management and fulfillment | Conversation roles and messages persist; stale revisions fail and can be retried against the live revision. | DELIVERED | 2 | Fresh stale-write denial followed by a successful append, with durable actor, role, and revision. |
| Unsupported request refusal | compiler legality and fulfillment | Unsupported questions terminate as `No Valid Plan` before candidate generation. | DELIVERED | 6 | Fresh unsupported request with attributable constraints and no execution or destination record. |
| Business process package | process-intake owning service | A fresh governed-browser submission stored the exact UTF-8 Markdown and strict manifest, returned a durable receipt, projected the process name and digest on reread, and kept internal operation references collapsed. Invalid manifests and unsupported media types are denied. | DELIVERED | 2 | Re-run the fresh browser transaction against each release candidate and retain its sanitized receipt evidence. |
| Typed product intent and activation | request-management and contract-service | Request-management owns strict candidate and approval records with exact replay, revision, and tenant checks. Contract-service owns activated acquisition authority and rejects conflicting replay. No composed interpreter yet resolves candidate constraints from live catalog and source authority into one end-to-end activation. | PARTIAL | 2-3 | Fresh approved request produces one replay-stable activated intent from current catalog and source authority; invalid and cross-tenant bindings are denied. |
| PostgreSQL and Stripe acquisition | runtime acquisition plus provider boundary | Production composition now resolves activated contract authority, binding, checkpoint, state, artifact, evidence, and provider ports; stale and inactive authority fail closed. No fresh PostgreSQL or Stripe transaction has crossed the composed path. | PARTIAL | 4 | Fresh source rows/events produce observations, artifacts, receipts, and a checkpoint; replay and denied credentials are proved. |
| PostgreSQL and ClickHouse LAND | runtime destination plus provider SDK | The provider-neutral LAND runner resolves a tenant-scoped ready destination binding, routes by engine, and constructs credential-scoped PostgreSQL and ClickHouse stores. Fresh PostgreSQL and ClickHouse emulator transactions each committed one batch, returned an exact no-op replay, and reconciled a simulated lost provider response without duplicate rows or receipts. Offline conformance covers conflict, empty input, schema mismatch, and provider failure classification. The composed acquisition checkpoint journey remains open. | PARTIAL | 5 | Fresh batch lands once in both engines; replay is a no-op; conflict, empty input, schema mismatch, provider failure, and ambiguous commit are classified. |
| Restricted provider SQL compiler | compiler | Schema-v2 product IIR and safe PostgreSQL and ClickHouse emitters validate identifiers and bind literal values. Provider boundaries can produce Ed25519-signed SQL observations, and compiler provenance passes only for a fresh matching verified envelope; raw observations remain untrusted. With strict owning-service source and target bindings, the compiler composes deterministic JSON decoding and one SHA-256 generation into a revalidated `ProductPhysicalPlan` candidate and satisfies precondition 13. Runtime derives cardinality from committed LAND receipts, signs and persists the exact envelope, and the compiler can satisfy precondition 14 only when that signed evidence matches every candidate parent and numeric bound. It does not admit, sign, or execute that candidate. Governed metric queries separately compile into signed, generation-pinned plans with SQL-level disclosure suppression and scan routing. Product compilation remains `No Valid Plan`. The PostgreSQL statement guards every landing value and resolves every name in `pg_catalog`. A live journey on the pinned PostgreSQL 18.6 image (`tests/integration/test_postgresql_compiled_product_journey_live.py`) produces every compiler input from its owning service -- acquisition, LAND, a signed landing observation, a compiler-composed plan and runtime-signed cardinality -- and the compiler leaves only the governed preconditions 15, 17 and 18; the guarded statement then materializes through dbt and reconciles to the source rows, under an execution authorization explicitly derived from the refusal. The D1-D8 evidence, mutation regression and review packet are complete and await independent review. | PARTIAL | 6 and 9 | Independent legality review of the packet; after approval, the per-engine activation record, admission and a signed execution authorization bound to the admitted plan; compose the same authorities into the request-intake journey and production runtime; integrate ClickHouse magnitude evidence with a committed generation and answer reader. |
| Transform and materialization | runtime execution and dbt adapter | A signed-model dbt adapter verifies compiler and authority digests before invocation. The materialization runner durably records transform, test, lineage, and provider receipts before switching a stable view, and resumes catalog publication without rerunning the transform. PostgreSQL re-observes signed Decimal57/9 output checks, rejects null or out-of-range values, binds zero violations into its commit reference, and the current answer reader rechecks that exact authority before returning rows. A fresh live PostgreSQL source-to-answer transaction reached its HTTP result with two exact rows. ClickHouse now has a provider-owned component that verifies compiler-signed checks and observes exact exclusive bounds in one read-only query; it is not yet bound to ClickHouse materialization, publication, or answer authority and has no fresh live evidence. This is not complete request-to-product delivery evidence. | PARTIAL | 7 | Fresh landed data transforms under the admitted physical plan, magnitude checks pass in both engines, the product is committed, catalog publication round-trips, and the answer reader consumes the exact authority. |
| Run intent, trigger, replay, and recovery | state service and trigger boundary | State owns canonical run intents, exclusive expiring leases, epoch fencing, replay-stable completion, transient-only retry, and operator cancellation. Trigger owns deterministic daily, run-now, and bounded aligned backfill policies and materializes one state-owned run for each canonical intent. Durable-boundary resume and the full operator projection remain incomplete. | PARTIAL | 8 | Fresh due intent runs once; lease loss, crash, replay, and resumed execution preserve epoch and checkpoint invariants. |
| Governed factual answers | request-management, compiler, and governed answer runtime | A fresh native PostgreSQL question is policy-admitted, compiled into a signed generation-pinned plan, executed through the read-only `answer_runtime` principal, persisted as an exact result snapshot and execution receipt, delivered by request-management, and read back through HTTP. Revoked current authority makes the same result unavailable. | DELIVERED | 9 | Re-run the fresh native transaction for each release candidate; ClickHouse parity remains part of Task 16. |
| Result datasets, tables, and CSV | result service and console projection | The console reads a durable typed result snapshot through current answer authority, paginates bounded rows, renders a governed table, and records policy-checked CSV downloads. A fresh native PostgreSQL result rendered two reconciled rows and downloaded exact CSV; revoked and view-only authority denied downloads. | DELIVERED | 10 | Re-run the native Playwright result and exact-download journey for each release candidate; the full source-to-product journey remains Task 16. |
| Catalog publication, list, and detail | catalog-control and publication repositories | Approved semantic objects can be published, listed, and opened with tenant isolation. | DELIVERED | 7 | Fresh publication round-trip and cross-tenant denial; Task 7 must repeat this for the newly materialized product. |
| Superset dashboard lifecycle | BI control service and Superset provider | BI control owns signed dashboard contracts, strict desired state, durable provider receipts, stable dataset identity, and session-bound expiring link authority. A fresh Superset 4.1.1 transaction created and read back one governed database, dataset, dashboard, and two attached charts, preserved exact membership across replay and archive, verified least-privilege warehouse reads, and cleaned every test resource. The native UI lists the provider-receipted dashboard with governed freshness and access status without exposing Superset identifiers. Authenticated browser launch remains blocked on tenant Superset SSO identity binding. | PARTIAL | 11 | Compose the link authority into the console, validate the exact tenant Superset origin, and prove an SSO-authenticated requester loses launch access immediately after grant revocation. |
| Access proposal, grant, expiry, and revocation | access-control service | A fresh governed-local request progresses through approval, admission, result and warehouse effects, delivery, immediate expiry denial, and reconciliation to revocation. Requesters and architects can revoke an active grant against its authoritative revision with a persisted reason; partial cleanup retries only missing effects while authorization remains denied. Fresh PostgreSQL, ClickHouse, and Superset transactions prove least-privilege apply, replay, revocation, and post-revocation denial. A composed dashboard-mode request and active grant now exposes its exact dashboard through the requester API, and authoritative revocation immediately removes it. One correlated external transaction across every surface, and owned provisioning of Superset grant roles and principal membership, remain open. | PARTIAL | 12 | Drive one admitted grant through live warehouse, result, and Superset effects with correlated receipts; prove partial cleanup recovery, and provision grant-scoped Superset identity through an owning lifecycle. |
| Operation retry and recovery UI | state service, owning operation services, and console projection | State owns typed append-only incidents and evidence-bound transient retry and unstarted cancellation. The architect console lists tenant-scoped incident context through opaque handles, requires a current revision and reason, and exposes only actions admitted by current state. Native query incidents are wired into the Operations page. Other incident producers and recovery actions remain incomplete. | PARTIAL | 13 | A fresh transient provider failure must expose an admitted action, execute once through the UI, and reach terminal success without duplicate effects. |
| Context graph and impact analysis | knowledge-graph projection service and owning authority services | A deterministic rebuildable projection records attributable nodes and edges, and impact traversal covers all seven subject kinds while preserving authority-derived approval requirements under visibility filtering. Request-management's graph-backed adapter is composed in governed-local from the approved semantic version and integration contract, binds authoritative source records, adds validated approval requirements, and supersedes stale proposals before admission. A fresh proposal projected safe impact labels in the browser and an authority change produced proposal revision 2 with three actual requirements. Production authority adapters and a retained evidence package remain absent. | PARTIAL | 14 | Compose production graph sources and visibility authority, retain the correlated evidence, and repeat supersession and filtered presentation against current service records. |
| Agent interface | context-exposure and owning request/answer services | Context exposure exposes all eight host-neutral MCP tools and rechecks delegation, current entitlements, answer policy, purpose scope, agent access, disclosure, and per-principal ceilings on every call. A real MCP call now creates a request-management record that atomically binds the human principal and agent client; clarification and request listing use the same owner adapter. Production current-authority, catalog, answer, and impact adapters are not configured, so the stdio entrypoint fails explicitly rather than starting with partial authority. | PARTIAL | 15 | Compose durable production authority and read adapters, then prove the agent request reaches the same answer receipt as the UI; prompt injection, over-broad scope, revocation, and approval attempts are denied. |
| Complete live PostgreSQL journey (Gate A) | cross-component acceptance suite | An offline request-first gate starts with an empty product-publication authority, durably records a fresh titled request, exact typed candidate, and approval, and then stops at compiler-owned `No Valid Plan` with no execution or publication. Signed observations and deterministic generation-scoped SQL are tested component capabilities, not a composed journey. No fresh user request currently reaches acquisition, LAND, transform, magnitude-authoritative product publication, governed query, result table, and dashboard in one correlated transaction. Blocked until the product SQL rule is activated for PostgreSQL; `compile_product_iir` returns `NoValidPlan` by type and has no success path. | BLOCKED | 16 | One empty-state live PostgreSQL journey reaches terminal delivery with sanitized evidence from every hop, plus backup and restore and independent review. |
| Complete live ClickHouse journey and two-engine parity | cross-component acceptance suite | Deferred by the 2026-09-15 scope decision. ClickHouse LAND, governed query compilation, and access provisioning are separately proved. The ClickHouse product-materialization provider ships inert: its consumption switch fails closed with `permanent_configuration` because `MaterializationObservation` carries no exact ClickHouse commit reference, and a test pins that. No ClickHouse activation of the product SQL rule exists. | BLOCKED | plan 2026-09-15 M8 | ClickHouse runtime magnitude authority, its own D1-D8 fixtures, a live cross-engine equivalence run, a second independent review, and the witnessed journey repeated on ClickHouse. Closing this row is what passes the addendum MVP. |
| Scouts | post-MVP scout service | No deterministic scout condition, deduplication, or suspension transaction exists. | BLOCKED | 17 | Fresh deterministic condition emits once, duplicate input does not duplicate work, and suspension prevents further execution. |

## Evidence record

Each closing run must retain a stable correlation identifier; tenant-safe request, contract,
plan, run, artifact, result, publication, and dashboard references as applicable; canonical
input and artifact digests; provider and compiler versions; state transitions with UTC
timestamps; policy and authority decisions; terminal outcome; and sanitized failure details.
Secrets, source row values outside the approved result, raw credentials, and provider account
identifiers do not belong in the evidence package.

For a capability that spans services, the evidence must demonstrate continuity of identity and
digests across every hop. A later read of the terminal owning record is required so that a log
line or transient response cannot be mistaken for durable success.

## Current branch evidence

- `tests/acceptance/test_run_request_to_product.py` proves the Task 16 request-first ordering against
  disposable owning stores: no product publication exists before request intake, before compilation,
  or after compilation; request management durably owns the exact candidate and approval; mismatched
  approved semantics are rejected; and compiler legality stops with `No Valid Plan` before execution.
  The procedure and its current live blockers are recorded in
  `docs/request-to-product/acceptance-run.md`.
- `tests/acceptance/test_run_console_governed.py` proves the process-package HTTP command,
  exact original-byte and manifest readback, durable receipt row, unsupported media denial,
  product-intent candidate ownership, approval replay, stale revision, and cross-tenant denial.
- `services/runtime/tests/test_landing.py` and provider destination conformance tests prove the
  shared LAND contract, exact replay, schema denial, and ambiguous-commit classification.
  On 2026-09-14, `providers/postgresql/tests/test_destination_live.py` and
  `providers/clickhouse/tests/test_destination_live.py` passed against fresh containers. Each
  committed two distinct generations, returned byte-identical replay for the first, reconciled a
  simulated lost response for the second, and retained exactly two rows and two receipts.
- `services/runtime/tests/test_product_materialization.py` proves receipt-before-switch,
  schema mismatch refusal, authority replay conflict, catalog-only retry, and crash recovery.
- `services/state/tests/test_run_service.py` proves canonical intent replay, exclusive leases,
  epoch fencing, completion conflict handling, tenant isolation, and transient-only retry.
- `services/trigger/tests/test_materialization.py` proves daily-window identity, policy-version
  separation, exact replay, inactive-contract denial, canonical run-now, and bounded backfill.
- `services/compiler/tests/test_governed_query.py` proves deterministic signed plans, bound
  values, identifier rejection, SQL-level group suppression, scan routing, and definition
  no-plan behavior for PostgreSQL and ClickHouse.
- Warehouse lifecycle and provider suites prove `answer_runtime` can read its approved
  consumption probe while writes, raw reads, and administrative commands are denied.
- `services/request-management/tests/test_answer_validation.py` and
  `test_answer_service.py` prove the §13.8 outcome precedence, candidate discard, policy scope,
  approval matrix, expiry, supersession, entitlement, and authority boundaries without a model
  call.
- `services/runtime/tests/test_acquisition_composition.py` and
  `services/runtime/tests/test_destination_composition.py` prove owning-authority resolution,
  provider routing, stale or mismatched binding denial, and replay-safe composition.
- The integrated offline suite completed with `4723 passed, 4 skipped, 28 deselected` on
  2026-09-15. The skipped credential-gated live-provider tests remain explicit evidence gaps.
- `packages/provider-sdk/tests/test_bi_conformance.py`, Superset provider conformance, and
  `services/bi-control/tests/test_service.py` prove stable external identity, durable desired
  state and receipts, replay, archive, and ambiguous-outcome recovery without a live provider.
- On 2026-09-14, `tests/integration/test_superset_dashboard_live.py` passed against a fresh
  Superset 4.1.1 and PostgreSQL stack. BI control created and read back one database, one dataset,
  two attached charts, and one dashboard; exact chart membership survived active replay, archive,
  and archived replay; the warehouse role read the two expected aggregate rows and could not create
  schemas or databases; labeled containers, networks, volumes, and the local image were removed.
- `tests/acceptance/test_console_answer_runtime.py` and the native Playwright result journey prove
  a fresh PostgreSQL-backed answer reaches durable delivery, renders a policy-bounded table,
  downloads exact CSV, disappears after authority revocation, and exposes durable query incidents
  to an architect without private source or evidence references. The same fresh journey publishes
  a dashboard whose title, answer as-of time, freshness, workspace access, and lifecycle are visible
  without its provider object ID. The production console build, 266 web tests, contract generation,
  TypeScript, ESLint, and native Playwright journey passed on 2026-09-15.
- State incident and recovery suites plus console server and web suites prove typed incident
  revisions, action admission, optimistic concurrency, evidence-bound replay, opaque UI handles,
  and the reason-required retry and pre-start cancellation controls.
- Access-control and provider-SDK suites prove proposal-bound grant persistence before effects,
  current-entitlement enforcement, provider-result validation, partial apply cleanup, ordered
  revocation, expiry denial, exact replay, and retry of only missing transient or ambiguous effects.
- PostgreSQL access-provider tests prove authority-bound field mapping, quoted identifiers,
  column-level grants, grant-scoped revocation, permanent statement rejection, transient connection
  failure, ambiguous commit classification, deterministic receipts, and sanitized driver errors.
- ClickHouse access-provider tests prove the same authority and column boundaries over its secure
  HTTP interface, including replay conformance, identifier injection denial, and conservative
  ambiguous classification after a dispatched timeout or server failure.
- On 2026-09-14, fresh PostgreSQL 18.6 and ClickHouse 25.8 transactions denied access before the
  grant, applied the exact authority-bound fields, returned deterministic receipts across replay
  and provider reconstruction, denied sensitive/raw/administrative operations, revoked access,
  replayed revocation exactly, and denied the same principal immediately afterward.
- Result access-provider tests prove atomic durable ACL and effect-receipt persistence, exact
  view/download checks, expiry denial, replay, revocation, and ambiguous post-write failure.
- Superset access-provider tests prove grant-scoped role reconciliation, stable replay, drift
  refusal, exact dashboard authority, exact post-write role readback, and provider failure
  classification. On 2026-09-14, a fresh Superset 4.1.1 transaction exposed no governed dashboard
  before apply, exposed exactly the authority-bound tenant dashboard after apply, never exposed an
  isolated tenant dashboard, and exposed none immediately after revoke using the same requester
  session. Apply and revoke replay returned identical receipts and cleanup removed every labeled
  test resource.
- `tests/integration/test_request_to_dashboard_live.py` composes those boundaries in one cold
  Superset transaction: a fresh dashboard-mode request reaches approved and admitted access, BI
  control publishes and reads back the actual provider dashboard, the requester API exposes only
  the authorized human-labeled dashboard, and authoritative revocation immediately empties the
  same API response without leaking request, grant, dashboard, provider, or origin identifiers.
- Request-management and access-control tests prove grant identity and current authority are
  persisted with the approved proposal, consumed only at the exact admitted request revision, and
  translated into surface-specific commands without allowing the console to invent grant scope.
- `tests/integration/test_access_lifecycle.py` and the governed-local acceptance suite prove a fresh
  approved request applies result and warehouse effects, is marked delivered only after exact
  receipt verification, projects no internal grant or provider identifiers, denies use at the
  expiry boundary, and reconciles the grant to revoked. The same acceptance deployment now composes
  a dashboard-mode request, active access grant, and BI publication, then proves the requester list
  contains the dashboard before revocation and is empty immediately after revocation.
- A fresh browser request titled `Regional revenue analyst access` was clarified, superseded when
  current authority changed, re-approved through separate requester, finance-owner, and policy
  sessions, then admitted and read back in state `delivered`. The requester saw only the approved
  `net-revenue` field, `query` and `view` permissions, and automatic expiry; no request, grant, or
  provider-effect identifier appeared in the delivery panel.
- Knowledge-graph, request-management impact-admission, and console impact suites prove stable
  rebuilds, source-record rederivation, additive approvals, stale-proposal supersession, role-filtered
  presentation, and a clean no-analysis state in the native console.
- A fresh governed-local proposal titled `Net revenue impact review` bound the approved metric and
  product records, rendered `Net revenue`, `Revenue data product`, and `Finance data owner` without
  internal graph or contract identifiers, then superseded to proposal revision 2 after the owner
  authority changed; the revised proposal required three recorded approvals.
- Context-exposure and end-to-end agent-interface suites prove the exact eight-tool surface,
  per-call reauthorization, inert prompt-like inputs, privacy-safe projections, atomic dual-principal
  request intake through MCP, and the absence of any approval tool or decision side effect.
- Persistent fresh native sessions are available for manual product testing at the architect
  dashboard and requester result surfaces recorded in the current handoff.
