# Live catalog and warehouse product audit — 2026-09-10

## Verdict

**NO-GO for end-to-end product testing against live managed data.** The configured
PostgreSQL warehouse and OpenMetadata catalog both execute real provider transactions,
but the console's core request journey does not use either one. It stops at
`execution_ready`, withholds the answer from the requester, and cites a deterministic
acceptance scenario created in memory.

The live infrastructure is useful for provider testing today. It is not yet connected
into a complete user job.

## Severity-ranked findings

### P1 — Request fulfillment does not use the configured catalog or warehouse

The only admitted question is an exact local string match. Its semantic publication,
contract, answer, freshness, and evidence come from `published_repository()` and
`ScenarioAnswerProvider`, rather than the configured OpenMetadata and PostgreSQL
bindings (`tests/acceptance/run_console_governed.py:264-290` and `:430-450`).

A fresh browser request completed clarification, proposal, requester acceptance,
architect approval, and admission. It stopped at `execution ready`. The requester page
then said the answer remained withheld until a verified delivery receipt exists. The
documented contract confirms that answer text delivery and destination effects are
undelivered (`docs/console/known-gaps.md:47-79`).

**User impact:** a user can finish every visible approval step without receiving an
answer or causing a warehouse or catalog effect.

**Fix:** compose fulfillment from the tenant's active catalog publication and warehouse
binding, execute the approved operation, record a terminal delivery receipt, and project
the delivered answer to the requester.

### P1 — Data access requests can be created but cannot be processed

A fresh high-risk data-access request was accepted and appeared in the architect queue.
Its detail exposed no clarification, preparation, denial, or cancellation action, so it
remained permanently `submitted`. The governed backend explicitly returns no preparation
actions for every request that is not a stakeholder question
(`apps/console/server/src/pillarmesh_console/governed_backend.py:966-980`).

**User impact:** the UI offers a product request that no user can complete.

**Fix:** wire access candidate generation and its authority resolver, then add apply,
expiry, and revocation transactions. Until those exist, label data access as unavailable
at intake rather than accepting requests into a dead end.

### P1 — Catalog and data-product routes do not show the live objects they advertise

`/catalog` and `/data-products` render the same capability-state ledger. They do not list,
search, or open catalog assets or data products
(`apps/console/web/src/routes/router.tsx:198-217` and
`apps/console/web/src/components/capability-summary-page.tsx:39-65`). A governed API read
can return the admitted `product-revenue` artifact, but the page has no list or link to
it. The console catalog read returns `503 capability_not_delivered` even for references
that were freshly published and read back directly from OpenMetadata.

**User impact:** configuring the live services produces no inspectable catalog or
data-product experience inside PillarMesh.

**Fix:** add tenant-scoped catalog and data-product list/detail contracts backed by the
owning publication and product services, then route from the summary pages to those
records.

### P1 — The local catalog stack is not stable under the available Docker memory

OpenMetadata's ingestion container and Elasticsearch had both exited with code 137.
Elasticsearch was restarted during this audit and recovered to a healthy container with
all 76 primary shards active. The cluster remains yellow because a single-node cluster
cannot allocate its replicas. The ingestion container remains exited, so metadata
ingestion and source acquisition are unavailable. This matches the console's explicit
`Source acquisition — Not delivered` state.

**User impact:** catalog search degraded silently after several hours, while the console
continued to say the managed catalog was ready.

**Fix:** give the catalog deployment a tested memory budget, keep core services under
supervision, and make catalog readiness include search health. Run ingestion only after a
source acquisition path exists and its memory requirement is provisioned.

### P2 — OpenMetadata logs an error for glossary-term lineage that it still persists

Both fresh lineage writes logged `Unsupported Entity Type glossaryTerm for column
lineage`. The API nevertheless returned success, and fresh reads verified the exact edge
and graph. This is not data loss in this run, but an error-level log for a successful
provider operation makes operational alerting unreliable.

**Fix:** use a lineage representation OpenMetadata supports without error, or classify
and document the provider behavior with an explicit compatibility test.

### P2 — Governed-local time and actor labels look like internal test data

Every transaction created on 2026-09-10 displayed `2026-08-31T12:00:00Z`, because the
harness injects a fixed clock. Conversation messages also show `requester-a`, since no
display-name directory owns historical author labels
(`docs/console/known-gaps.md:217-226`).

**User impact:** users cannot establish when work happened and still encounter internal
principal labels.

**Fix:** use the wall clock in the interactive harness and introduce an owning actor
display-name projection for conversation history.

## Fixed during the audit

Two running console processes loaded `bindings.json` only at startup. After the architect
configured PostgreSQL, the architect saw an active workspace while the requester still
saw setup blocked. The persisted binding adapter now reloads before reads and writes and
publishes updates with atomic replacement
(`tests/acceptance/run_console_governed.py:322-372`). A regression test starts two live
deployment objects and proves one immediately observes the other's binding
(`tests/acceptance/test_run_console_governed.py:310-321`). The test failed before the fix
and passed after it. Restarting only the requester server then changed its projection from
`setup / warehouse blocked` to `active / warehouse ready`.

## Live capability evidence

| Capability | Fresh transaction | Terminal evidence | Result |
| --- | --- | --- | --- |
| PostgreSQL TLS | New PostgreSQL TLS negotiation | TLS 1.3, `TLS_AES_256_GCM_SHA384`, server certificate presented | Pass |
| Warehouse ingestion role | Inserted a new row into the governed raw probe | Committed and read by transformation role | Pass |
| Warehouse transformation role | Read raw and wrote conformed and two consumption probes | Customer and BI roles read their separate outputs | Pass |
| Warehouse denials | Tried customer→raw, BI→customer table, ingestion→raw read, catalog→row read, backup→write, administration→unrelated schema | Every operation was denied by PostgreSQL | Pass |
| Warehouse cleanup | Deleted every audit row as administrator | Fresh probe row count returned zero | Pass |
| OpenMetadata publication | Published semantic version 2 with two newly named entities | Six objects read by a fresh client; namespace, glossary terms, classifications, and lineage all matched | Pass with log concern |
| Catalog replay | Replayed the identical publication | Same receipt returned | Pass |
| Catalog tenant isolation | Read a fresh reference as another tenant | Provider denied the read | Pass |
| Supported stakeholder request | Created a new request and completed every visible approval | Reached `execution ready`; no delivered answer | Fail |
| Unsupported stakeholder request | Created a new forecast request and attempted preparation | Closed as `No Valid Plan` with safe explanation and required change | Pass |
| Revised request | Started from the refusal and submitted corrected prefilled content | New request created with a user-facing title | Pass |
| Conversation | Submitted on a stale revision, reloaded, and resubmitted | Visible conflict message first; durable message after reload | Pass |
| Data access | Created a new query-access request | No architect action exists; remained `submitted` | Fail |
| Runs | Opened live route and API | Empty; no acquisition can create a run | Blocked |
| Acquisition receipts | Opened live route and API | Empty; no acquisition runtime is composed | Blocked |
| Catalog UI | Opened live route after fresh provider publication | Capability ledger only; live objects inaccessible | Fail |
| Data products UI | Admitted a product reference and opened route | Capability ledger only; API record has no UI entry point | Fail |
| Dashboards | Opened route and detail API | Explicitly not delivered; detail API returned 503 | Blocked |
| Evidence | Read the admission evidence API | Evidence exists, but the route is only a capability ledger | Partial |
| Process package | Inspected configured active workspace | Explicitly not delivered | Blocked |
| Operation retry | Inspected configured active workspace | Explicitly not delivered | Blocked |

## Coverage

All 15 React Router states were exercised: root, setup, review detail, inbox, inbox
detail, requests, request detail, data products, runs, acquisition receipts, catalog,
dashboards, evidence, recovery, and wildcard recovery. Both available personas were
used: requester and data architect. Direct role-inappropriate routes failed closed or
showed the server's safe error.

The setup and semantic-review mutation controls could not be re-executed in this already
active workspace without provisioning another environment. Their completed persisted
state was inspected; this audit does not count that inspection as a fresh transaction.
Owner, viewer, guest, and external personas do not exist in this local deployment.

The static action sweep found 13 files with click handlers and no confirmed empty,
console-only, placeholder, or silently swallowed action handler. Browser interaction
covered request creation and validation, queue selection, clarification, proposal,
submission, both approvals, admission, conversation conflict/retry, refusal, and revised
request creation. The remaining setup/review handlers are covered by the repository's
automated browser suite rather than a new live mutation here.

## Automated verification

- `npm run check:contracts`, `npm run lint`, `npm run typecheck`, and `npm run build` passed.
- Vitest passed 209 tests in 14 files.
- Playwright passed 46 tests; 6 repository-owned scenarios remain explicitly skipped.
- The complete offline Python suite passed 3,492 tests with 14 live tests deselected.
- `uv lock --check`, Ruff lint, Ruff format, mypy over 164 source files, and repository
  structure validation all passed.

## Implementation plan

1. Make fulfillment resolve the tenant's current semantic publication and warehouse
   binding. Replace the local exact-string scenario at the console composition boundary.
2. Add an execution transaction that runs the approved query or definition retrieval,
   records terminal evidence, and publishes a requester-visible delivery receipt.
3. Deliver access proposal preparation, then applied grant, expiry, and revocation. Gate
   data-access intake until this path exists.
4. Add catalog and data-product list/detail read contracts and build real pages around
   them. Include search health in catalog readiness.
5. Compose the acquisition runtime with a live source binding so new runs and receipts can
   be produced through the product.
6. Deliver dashboards, catalog previews, process-package upload, managed deep links, and
   retry authority from their owning services.
7. Size and supervise the OpenMetadata stack, then repeat this matrix from an empty state
   with fresh setup, source acquisition, fulfillment, and delivery transactions.

## Remediation status — 2026-09-10

The severity-ranked findings above are remediated in the governed-local product:

- Stakeholder questions resolve one unambiguous term from the active catalog
  publication. Policy, entitlement, classification, product, semantic, contract, and
  lineage references derive from that same approved publication rather than the Plan 3B
  scenario. Admission checks the recorded state of the warehouse binding (`ready`) and of
  the publication round trip, then records a terminal delivery receipt and publishes the
  approved answer to the requester. **Correction, 2026-09-10 live audit:** this check reads
  persisted records only; it does not query the warehouse or re-read the catalog at
  delivery time, so the product no longer describes the delivery as verified.
- Data-access intake is visibly unavailable and is rejected server-side while grant
  application, expiry, and revocation remain uncomposed. It can no longer create a dead
  request.
- Catalog and data-product list/detail pages read tenant-scoped records from the owning
  publication and policy repositories. Opaque provider digests are projected as a
  governed classification status rather than shown to users.
- Catalog readiness now requires a healthy search projection. The local OpenMetadata
  core services have explicit memory limits, restart policies, and health checks;
  ingestion is opt-in until a source-acquisition path exists.
- The OpenMetadata 1.13.3 glossary-term lineage response is covered by an explicit
  compatibility test: the provider accepts the error-level upstream message only when
  the exact edge and graph round trip still verify.
- Interactive timestamps use the wall clock. Historical authors resolve through the
  deployment actor display-name directory.

A fresh request, `Live governed catalog answer verification 2026-09-10`, used the newly
published `Customer audit 20260910081713` term. It completed clarification, proposal,
requester acceptance, architect approval, admission, execution verification, and
delivery. Request-management durably recorded revision 7 in `delivered` state and one
delivery receipt. The requester page rendered the approved definition and governed
references. The receipt cites the live warehouse binding, semantic version, integration
contract, `live-catalog` product, and lineage observation. The PostgreSQL container was
healthy and accepting connections; OpenMetadata server, Elasticsearch, and MySQL were
healthy, and Elasticsearch reported all primary shards assigned.

Source acquisition, dashboards, process-package upload, and retry authority remain
explicitly `not_delivered`. They were blocked items in the original matrix, not silent
or falsely ready product paths.
