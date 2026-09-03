# Data Architect Console Design

**Date:** 2026-09-01
**Status:** Draft for review
**Scope:** First substantive `apps/console` implementation for the architect-centered MVP

## 1. Purpose

PillarMesh already has backend boundaries for managed warehouse lifecycle, managed catalog and
semantic formation, architect-inbox proposals, and bounded source acquisition. Those capabilities
are not yet available as one product experience. A data engineering architect cannot currently
establish a workspace, review generated meaning, approve a governed data product, or operate daily
requests without calling Python interfaces and acceptance harnesses directly.

This design creates the first PillarMesh console. It turns the approved architect journey into a
coherent product surface while preserving the existing authority boundary:

```text
Integration Contract -> Semantic IIR -> Physical Plan -> Execution Graph
```

The console is not a second control plane. It presents role-filtered projections, gathers explicit
decisions, invokes narrow commands, and displays committed outcomes from the services that already
own them.

The design has two delivery outcomes, implemented through the reviewable milestones in section 13:

1. a production-shaped, fixture-backed console that produces reviewable screenshots and a complete
   clickable architect walkthrough; and
2. a governed-local adapter that replaces fixture projections with currently implemented service
   reads and commands without changing the browser contract or page structure.

This sequence makes the first visible product useful immediately without creating a disposable
prototype.

## 2. Governing product decisions

This design refines the architect MVP in
`docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md` and uses the
approved repository boundary in `docs/architecture/repository-layout.md`.

The following decisions were approved in the design conversation:

1. A first-time architect enters guided setup before seeing the operating command center.
2. Guided setup uses an **architect workbench**, not a sequence of isolated forms. Progress,
   generated artifacts, lifecycle state, and approval boundaries remain visible.
3. Setup has three immutable, role-bound approval gates:
   - meaning and ontology;
   - generated data product; and
   - activation.
4. Returning users enter an **architect decision workspace**, not a generic ticket table or Kanban
   board. PillarMesh performs bounded investigation and presents the smallest safe decision with
   evidence, limitations, and authority requirements.
5. PillarMesh owns the review experience. OpenMetadata and Apache Superset provide managed catalog,
   lineage, dashboard, and rendering capabilities through contextual previews and authenticated
   deep links. Their complete application interfaces do not define PillarMesh navigation.
6. The implementation uses a React and TypeScript browser application with a thin Python Starlette
   adapter. Existing services remain authoritative.
7. Fixture and governed-local modes implement the same versioned console API. Fixture content is
   persistently labeled and can never be represented as a managed effect.
8. The browser never marks an approval, provision, acquisition, grant, report, or other managed
   effect complete until the owning service returns a committed authoritative projection or
   evidence reference.

## 3. User and product outcome

The primary user is one data engineering architect who may hold several organizational roles but
must still act through distinct role-scoped authorities.

The console succeeds when that architect can:

1. choose PostgreSQL or ClickHouse and approve an immutable managed warehouse binding;
2. observe warehouse, OpenMetadata, and Superset setup without administering those systems;
3. connect the approved PostgreSQL and Stripe source classes without seeing stored credentials;
4. upload the versioned business-process package;
5. review extracted process, ontology, ownership, identity, lifecycle, constraint, metric, and
   classification candidates;
6. complete the meaning, data-product, and activation approval gates;
7. observe acquisition and downstream capability state without confusing accepted work with a
   completed effect;
8. operate stakeholder questions, access requests, incidents, and platform proposals from one
   prioritized decision workspace; and
9. inspect the evidence and lifecycle history that supports every decision and outcome.

A requester, who is not the primary user, must additionally be able to submit a typed request,
answer a business-meaning question, and accept a clarified outcome, because without those three
actions no stakeholder request can reach an admitted decision at all. Section 5.4.1 bounds that
surface.

The first fixture-backed walkthrough proves the interaction model. Governed-local mode proves only
the capabilities that have real service support. It must explicitly identify unavailable later
capabilities rather than simulate completion.

## 4. Scope

### 4.1 Included

- one authenticated-product shell with tenant, workspace, actor-role, and mode context;
- the first-run architect workbench;
- warehouse selection and immutable-binding confirmation for PostgreSQL and ClickHouse;
- managed-service and source-connection status surfaces;
- business-process package upload and extraction status;
- meaning, data-product, and activation review surfaces;
- a returning-user command center and prioritized decision inbox;
- a bounded requester surface: typed request intake, the clarification conversation, and
  clarified-outcome acceptance;
- stakeholder-answer, access-preview, incident, semantic-change, and platform-proposal review
  presentations;
- contextual OpenMetadata evidence and Superset dashboard/report previews;
- product, run, catalog, dashboard, and evidence summary routes;
- authoritative command submission with exact revision, digest, role, tenant, and idempotency
  binding;
- admission of a fully approved proposal to execution;
- visible stale-state, denial, `No Valid Plan`, transient-failure, ambiguous-outcome, and
  not-delivered states;
- deterministic revenue-to-cash demo fixtures;
- an injected governed-local backend over public service interfaces;
- keyboard navigation, responsive desktop layouts, reduced-motion support, and WCAG 2.2 AA color
  contrast for included journeys;
- browser screenshots and automated interaction coverage; and
- local development and build instructions.

### 4.2 Not included

- a SQL editor, notebook, arbitrary query workbench, or unrestricted result browser;
- a DAG canvas, workflow authoring surface, arbitrary cron editor, or general scheduler;
- full OpenMetadata or Superset application embedding, and the analyst surface that will own
  governed Superset embedding, draft editing, and personal exploration under addendum sections 16.4
  and 18;
- catalog authoring that bypasses semantic and authority review;
- dashboard or report authoring beyond reviewing PillarMesh-generated candidates;
- provider credential display or direct infrastructure administration;
- production SSO, tenant creation, billing, deployment, or internet exposure;
- WebSocket or server-sent-event infrastructure;
- mobile-first workflows;
- a generic design-system package before a second application consumes the primitives;
- automatic approval, semantic repair, access widening, or effect inference; or
- claiming unimplemented transformation, destination, Superset, answer-delivery, or access-grant
  behavior as live.

## 5. Experience architecture

### 5.1 First entry and route selection

The server-issued workspace projection determines the landing route:

- an incomplete foundation opens `/setup` at the earliest incomplete or blocked stage;
- a completed foundation with a pending activation review opens that review;
- an activated workspace opens `/inbox`; and
- an unavailable workspace opens a typed recovery page rather than falling back to fixture data.

The browser must not infer setup completion from local storage. Local storage may preserve visual
preferences and unsubmitted comments only.

### 5.2 Architect workbench

The setup workbench keeps a stable left-hand foundation rail and a focused main workspace. It
contains seven stages:

1. **Foundation:** organization context, PostgreSQL or ClickHouse, supported region, fixed capacity,
   role assignments, estimate, and immutable-binding review.
2. **Managed services:** warehouse, OpenMetadata, and Superset operation status with validation,
   backup, monitoring, and authority summaries.
3. **Sources:** PostgreSQL and Stripe connection intents, least-privilege checks, intended probes,
   denial probes, and readiness state.
4. **Business process:** versioned package upload, immutable source-content identity, extraction
   status, and attributable candidate summary.
5. **Meaning review:** process model, ontology candidates, owners, identities, lifecycle,
   constraints, classifications, metrics, unresolved questions, and OpenMetadata evidence.
6. **Data-product review:** Integration Contract, generated warehouse model, quality rules,
   reconciliation, daily schedule, access policy, dashboard/report candidates, and cost estimate.
7. **Activation:** exact approved versions, feasibility, source and destination boundaries,
   unresolved constraints, final digest, and activation action.

Stages may be revisited, but a material edit produces a new server revision and invalidates every
downstream approval whose exact input changed. The interface shows that invalidation before the
architect submits the edit.

### 5.3 Three approval gates

The console groups review work into three comprehensible gates while preserving independently
typed authority decisions underneath:

| Gate | User-facing decision | Bound authority and artifacts |
| --- | --- | --- |
| Meaning | Approve the business meaning used to design the product | process version, authority observations, ontology, ownership, identity, lifecycle, constraints, classifications, and metric definitions |
| Data product | Approve the product PillarMesh proposes to operate | Integration Contract, warehouse model, quality and reconciliation rules, schedule, access policy, dashboard/report candidates, and estimate |
| Activation | Approve execution of one exact feasible version | prior approvals, provider observations, immutable warehouse binding, source bindings, feasibility result, and activation digest |

One person may satisfy several roles in the MVP, but the server records one role-scoped decision per
required authority. A single visual confirmation may submit several explicit role decisions only
when the authenticated actor currently holds every named role and the review page enumerates them.

### 5.4 Returning-user command center

The persistent product navigation contains:

- Inbox;
- Data products;
- Runs;
- Catalog;
- Dashboards; and
- Evidence.

The landing page is the inbox. Summary counts support orientation but never replace the underlying
request or operation state.

### 5.4.1 The requester surface, and why it is in scope

This design is architect-centered, but it cannot be architect-only. Two committed facts make a small
requester surface a precondition rather than a later nicety.

Addendum section 13.1 states that "the MVP includes native UI/API intake"; Slack and email are the
deferred channels, not the in-product one. Without intake, the governed-local inbox is permanently
empty, because nothing in this product can create a request.

Plan 3B, already on `main`, makes the requester an approving authority. Its policy compiler emits a
requirement whose `authority_ref` is the requesting principal and whose reason code is
`clarified_outcome_acceptance`, and admission refuses until that requirement has an exact approving
binding. An architect working alone in this console therefore cannot drive a stakeholder answer to
an execution-ready admission, no matter how complete the review screens are.

The requester surface is deliberately minimal:

- **Submit:** a typed intake form per admitted request type - stakeholder question and data-access
  request in the first milestone - carrying purpose and the fields the payload model requires.
- **Clarify:** the conversation thread for one request. Addendum section 13.5 requires PillarMesh to
  ask business-meaning questions "directly to the requester", and states that the engineer "must not
  become a manual message relay", so this is the requester's own surface, with the architect able to
  observe, intervene, or take over.
- **Accept:** the clarified outcome statement - restated request, purpose, in-scope and out-of-scope
  summaries - with an explicit acceptance action bound to its digest.
- **Follow:** own request status, own decisions, and, after a later verified delivery, the answer.

It excludes everything else. A requester never sees a candidate subject, an unapproved answer, an
effective access scope, approval metadata beyond their own decisions, or any evidence projection.
Plan 3B's read service already enforces that boundary; this surface consumes it rather than
re-deriving it.

The `clarifying` request state is part of this surface. A request sits there while its clarified
outcome is unsettled, and no proposal may be built from it, so the architect's queue shows it as
blocked on the requester rather than as work the architect can advance.

### 5.5 Decision workspace

The inbox uses a three-part review layout:

1. a prioritized queue ordered by server-issued risk, deadline, and dependency state;
2. a focused proposal review with requester purpose, proposed outcome, changes, limitations, and
   available actions; and
3. a contextual evidence area with governed datasets, metric versions, as-of time, freshness,
   quality, lineage, authorization, required roles, and immutable references; and
4. the request's clarification conversation, where the architect observes, intervenes, or takes over
   a business-meaning question that section 13.5 routes to the requester.

A request whose clarified outcome is unaccepted shows the missing requester acceptance as an
outstanding required authority, not as an architect action. The queue marks it blocked on the
requester so the architect is never presented with a decision that cannot be admitted.

Approval is not delivery. Recording every required approval leaves the request `awaiting_approval`;
admission is the separate owning transaction that carries the proposal into execution, and the
console offers it as its own action once the approvals it can see are complete. The console does
not judge the approvals - `FulfillmentService.admit` re-checks every requirement against the exact
proposal - it only declines to submit a command whose precondition it can already see is unmet,
because the service reports a missing authority as not-visible and a plainly visible request must
not answer with a `404`.

The queue supports request-type and state filtering. It does not permit user-authored lifecycle
columns or arbitrary workflow transitions.

The same review composition serves stakeholder answers, effective access previews, semantic
changes, integration changes, incidents, maintenance proposals, and activation decisions. Each
type supplies a narrow typed content component rather than a generic dictionary renderer.

### 5.6 Managed OpenMetadata and Superset integration

PillarMesh renders the decision context natively:

- catalog entity names, definitions, owners, classifications, and lineage summaries;
- exact catalog publication and round-trip evidence references;
- metric and dashboard versions;
- dashboard and report render previews; and
- managed-service health and capability status.

An authenticated deep link may open the corresponding OpenMetadata or Superset object when expert
inspection is useful. The server produces the link only after tenant and role authorization. The
browser does not construct provider URLs or receive private infrastructure identifiers.

The console does not iframe either full application in this design. That avoids duplicated
navigation, cross-application approval context, third-party session ambiguity, and accidental
exposure of provider-local administration functions.

That is a scoping decision about *this* surface, not a rejection of embedding, and the addendum has
to be answered on the point. Section 20.10 defers only "full native BI authoring beyond governed
Superset embedding", and section 18 requires that "catalog and BI embeds use PillarMesh identity and
authorization; public links are disabled by default" - both read embedding as present in the MVP.
Section 16.4's draft editing and personal exploration are analyst activities in a bounded workspace.

The reconciliation is by audience. Governed Superset embedding belongs to the analyst surface, which
is a separate design with its own authorization model; when it arrives it inherits section 18's rule
that an embed uses PillarMesh identity and authorization and that public links stay disabled. The
architect surface designed here needs to *judge* a dashboard candidate, not author or explore it,
and a preview plus an authenticated deep link is the smaller sufficient mechanism. The
implementation plan must record the analyst embedding surface as an explicit outstanding MVP
obligation rather than letting the deferral pass silently.

A preview is a still image, not a live application. The adapter requests a render from the managed
Superset deployment using the tenant's server-side credentials, strips it of provider-local chrome,
and serves it from the console's own origin under an opaque, tenant-scoped, short-lived reference.
The browser therefore needs no Superset origin at all, and the Content Security Policy in section 9
keeps `img-src` and `frame-src` at `'self'`. A render that fails produces the preview's own
unavailable state under section 8, never a broken image or a cross-origin request.

## 6. Technical architecture

### 6.1 Placement

The implementation creates the already-declared `apps/console` boundary:

```text
apps/console/
  README.md
  package.json
  package-lock.json
  vite.config.ts
  tsconfig.json
  web/
    index.html
    src/
      api/
      components/
      features/
        setup/
        inbox/
        products/
        runs/
        catalog/
        dashboards/
        evidence/
      routes/
      styles/
      test/
  server/
    pyproject.toml
    src/pillarmesh_console/
      app.py
      auth.py
      backend.py
      contracts.py
      errors.py
      fixture_backend.py
      governed_backend.py
      routes/
    tests/
```

`apps/console` owns product composition, presentation contracts, route handling, and browser assets.
It must not own compiler rules, semantic validity, lifecycle authority, credentials, provider
implementations, execution state, or evidence truth.

`apps/console` is not new to the repository's governance. It already has a row in
`docs/architecture/repository-layout.md`, already appears in the `validate_components apps`
allowlist in `tests/repository-structure/validate.sh`, and `apps/` with its README already exists.
The Structural Change Rule fires when a governed component is first *declared*, not when a declared
one is first implemented, so this work needs neither a new ADR nor a validator edit, and writing one
would imply a boundary change that is not happening.

What it does need, and what the validator stays silent about:

- `apps/console/server` added to `[tool.uv.workspace] members` in the root `pyproject.toml`, which
  currently has no `apps/*` member at all;
- `pillarmesh_console` added to `[tool.mypy] packages`;
- `uv.lock` regenerated with `uv lock`; and
- `node_modules/` and the browser build output added to `.gitignore`.

One placement detail is load-bearing. The root allowlist in the structure validator enumerates every
permitted top-level entry and does not include `.node-version`; a root-level pin would be reported
`UNEXPECTED` and fail the gate. The Node pin therefore lives at `apps/console/.node-version`, beside
the `package.json` it governs. Nested files under a valid component are not validated, so the
browser toolchain is otherwise invisible to that gate.

### 6.2 Browser stack

The browser application uses:

- React and TypeScript;
- Vite for development and production builds;
- React Router for explicit product routes;
- native `fetch` behind one typed API client;
- Ajv validation of every server response against the committed console JSON Schema;
- CSS custom properties and feature-local styles; and
- Vitest, Testing Library, and Playwright for browser-facing verification.

The implementation pins an active Node LTS in `apps/console/.node-version` - not at the
repository root, for the reason in section 6.1 - and exact dependency resolution in
`package-lock.json`. It uses `npm` because no JavaScript package manager exists in the repository
and npm ships with the selected Node runtime.

The first implementation does not add a general UI framework, remote font, state-management
library, charting library, or data-fetching framework. Ajv is justified at the untrusted network
boundary; TypeScript types alone cannot reject malformed runtime data. The authenticated console
does not need server-side rendering or search-engine optimization. Native CSS and small inline
SVGs are sufficient for the approved screens and keep the first dependency boundary reviewable.

### 6.3 Python application adapter

The Python process uses Starlette, Pydantic, and Uvicorn as direct dependencies. In development,
Vite proxies `/api` to the loopback-bound Python process. A production-shaped local build serves
the compiled static assets and API from one Starlette origin.

The adapter owns:

- authentication-context translation;
- tenant and role propagation;
- strict parsing and serialization of console contracts;
- role-filtered projection composition;
- mapping typed domain errors to console errors;
- dispatch of explicit commands to owning services; and
- static asset delivery.

It does not read service database tables directly when a public repository or service interface
exists. It does not reinterpret a domain rejection. It may compose several immutable read models
for one screen, but every field retains source provenance and no composed projection becomes
execution or evidence authority.

### 6.4 `ConsoleBackend` boundary

Routes depend on an injected `ConsoleBackend` protocol. Two implementations ship:

`FixtureConsoleBackend`
: Loads a committed, privacy-safe revenue-to-cash scenario into process-local demo state. It
  supports the complete clickable journey, deterministic reset, stale-decision and failure cases,
  PostgreSQL and ClickHouse selection, and no external effects.

`GovernedConsoleBackend`
: Calls public Plan 2, Plan 3A, Plan 3B, Plan 4A, state, runtime, and evidence interfaces. It returns
  a capability as unavailable when no real owning implementation exists. It never falls back to
  fixture state after a real read or command fails.

The backend mode is fixed at process start. Every response includes `data_provenance` with either
`demo_fixture` or `governed_local`; the application shell shows a persistent mode badge. Demo mode
uses a visually distinct banner and cannot produce or display a real evidence reference.

### 6.5 API contract

The API is versioned under `/api/v1`. Pydantic models are strict, reject unknown fields, use closed
enums, and generate a committed JSON Schema snapshot. TypeScript interfaces are generated from
that schema during development and checked for drift in CI. The API client validates response
payloads with Ajv before returning them to a feature. The frontend does not hand-maintain a second
authoritative field vocabulary or render a partially valid response.

Read routes include:

```text
GET /api/v1/session
GET /api/v1/workspace
GET /api/v1/setup
GET /api/v1/reviews/{review_id}
GET /api/v1/inbox
GET /api/v1/inbox/{request_id}
GET /api/v1/requests/mine
GET /api/v1/requests/{request_id}/conversation
GET /api/v1/requests/{request_id}/clarified-outcome
GET /api/v1/data-products/{data_product_id}
GET /api/v1/runs
GET /api/v1/catalog/{asset_ref}
GET /api/v1/dashboards/{dashboard_ref}
GET /api/v1/evidence/{evidence_ref}
GET /api/v1/operations/{operation_id}
```

Initial command routes include:

```text
POST /api/v1/setup/warehouse-binding
POST /api/v1/setup/process-packages
POST /api/v1/reviews/{review_id}/decisions
POST /api/v1/inbox/{request_id}/decisions
POST /api/v1/requests
POST /api/v1/requests/{request_id}/conversation
POST /api/v1/requests/{request_id}/clarified-outcome/acceptance
POST /api/v1/operations/{operation_id}/retry
POST /api/v1/demo/reset
```

The demo reset route is registered only in fixture mode. It is absent, not merely unauthorized, in
governed-local mode.

Replay safety comes from domain identity, not from the header. The owning services already define
it: Plan 3B admits under `(tenant_id, request_id, source_request_revision)` and returns the existing
receipt only after canonical equality, and Plan 3A and Plan 4A define equivalent identities for
operations and batches. The adapter does not keep an idempotency store, because a second replay
authority can disagree with the first, and the one that is not the service is the one that is wrong.

`Idempotency-Key` is therefore a correlation value with one narrow local job: the adapter uses it to
recognize a retry of a command whose response was lost and to serialize concurrent submissions of
the same key, so a double-click cannot become two in-flight calls. It never decides whether an
effect already happened; the expected revision and exact digest do that, inside the owning service.

Every mutating request carries:

- an `Idempotency-Key` header;
- the expected resource revision;
- the exact reviewed artifact or proposal digest;
- the selected actor role when more than one role is held; and
- only the fields permitted for that command.

The server derives tenant and actor identity from trusted request context. It never accepts either
as command authority from browser-supplied JSON.

### 6.6 Capability presentation

The workspace response includes a typed capability manifest. Each capability has one of these
states:

- `ready`: the backend can perform and evidence the capability;
- `blocked`: an attributable dependency or lifecycle condition prevents it;
- `degraded`: reads remain available but a documented operation is impaired; or
- `not_delivered`: no real owning implementation is wired into governed-local mode.

The interface disables unsupported actions and shows the exact dependency or delivery boundary.
It must not use a generic green health indicator for a capability that has not completed a new
terminal transaction.

## 7. Authoritative action and operation flow

An approval or other decision follows this sequence:

1. The browser loads a role-filtered projection containing the server revision and reviewed digest.
2. The architect reviews the proposal, evidence, limitations, and required authority.
3. The browser submits the decision with the expected revision, exact digest, role, and idempotency
   key.
4. The adapter validates the trusted identity context and delegates to the owning service.
5. The owning service checks lifecycle, role, tenant, revision, digest, and domain preconditions.
6. State, decision, and required evidence are committed according to the owning service's atomicity
   rules.
7. The adapter returns the new authoritative projection and evidence reference.
8. Only then does the browser display the decision as applied.

The UI may disable a button and show `Submitting decision` while the request is in flight. It may
not optimistically advance lifecycle state.

Long-running provisioning, validation, acquisition, restore, or retry work returns a durable
operation ID and an accepted state. The browser polls `GET /operations/{operation_id}` with bounded
backoff. Accepted work is not displayed as successful work.

That identifier is a console handle the adapter mints, not a service operation identity. The
services' operations are private state: Plan 3A's `PrivateWarehouseOperation` carries provider
resource handles, and Plan 4A's checkpoints and prepared batches are private for the same reason.
The adapter keeps a tenant-scoped map from its own opaque handle to the private identity and returns
only a typed status, a lifecycle phase, and any public evidence reference. Passing a private
operation identity through to the browser would defeat section 9 in the one field the browser polls
most often.

If a network failure leaves the outcome unknown, the UI preserves the idempotency key and reports
`Outcome unknown; reconciling`. It polls or safely retries the same command identity. It never
creates a new command identity merely because the response was lost.

Real-time streaming is deferred until polling creates a measured latency or load problem.

## 8. Error and recovery design

The adapter returns a typed error envelope with a stable code, safe message, correlation ID, and
permitted recovery action. It contains no raw exception, credential, provider response, private
identifier, source value, or unauthorized object existence.

| Condition | Product behavior |
| --- | --- |
| Invalid field or file | Keep the current screen, identify the exact safe field, and preserve other input. |
| Unauthenticated | Clear protected projections and require a new trusted session. |
| Unauthorized or wrong tenant | Deny without revealing whether the protected object exists. |
| Stale revision or digest | Do not apply the decision. Reload the exact new revision, offer a bounded comparison, and preserve an unsubmitted comment locally. |
| `No Valid Plan` | Show attributable constraints, affected artifact, responsible authority, and permitted next action. Do not flatten it to a generic failure. |
| Transient service failure | Keep the authoritative prior state and offer a safe retry only where classification permits it. |
| Ambiguous command outcome | Show reconciliation state and reuse the original idempotency key. |
| Long-running operation failure | Show the terminal classification, evidence or incident reference, and only the bounded recovery commands admitted by the owning service. |
| `not_delivered` capability | Show the planned capability and dependency, disable the action, and never substitute fixture behavior. |
| Malformed or unknown server payload | Fail the affected view closed, show a correlation ID, and do not render partially trusted decision content. |

An error in a catalog or dashboard preview must not discard the surrounding review. The preview
shows its own unavailable state, and the approval action is disabled whenever that evidence is a
required precondition.

## 9. Security and privacy

- Browser responses exclude credentials, private provider identifiers, endpoints, raw provider
  responses, raw source rows, unrestricted catalog metadata, and infrastructure inventory.
- The server obtains actor and tenant identity from trusted authentication context. A demo actor
  selector exists only in fixture mode and is visibly labeled.
- Every route uses deny-by-default role filtering. Reviewer and requester projections remain
  distinct; an unapproved answer or access scope never crosses into a requester projection.
- Deep links to OpenMetadata and Superset are server-issued, tenant-scoped, role-authorized, and
  short-lived or session-bound.
- Mutations require same-origin requests and a session-bound CSRF token in addition to authenticated
  session context. `GET /session` issues the safe request token; command routes require it in an
  `X-CSRF-Token` header. Development proxy behavior must preserve the origin and these checks.
- Content Security Policy disallows unapproved script, frame, font, and network origins. The MVP
  uses no third-party analytics or remote font.
- User-supplied process text, request text, answer candidates, catalog labels, and provider-derived
  strings render as text, never trusted HTML.
- Logs and browser telemetry contain correlation IDs, typed reason codes, route templates, and
  durations, not proposal text, source values, access scope, or secrets.
- Fixture data is synthetic and privacy-safe. Browser screenshots use fixture mode only.

Production SSO and deployment topology require a later design. The local server binds to loopback
by default and is not production authentication evidence.

## 10. Visual system and accessibility

The approved direction uses a deep navy navigation shell, quiet off-white work surfaces, blue
primary actions, and semantic colors reserved for lifecycle meaning. Color never carries status
alone. Every status includes text and, where useful, an icon.

The visual hierarchy is:

1. current decision or setup outcome;
2. risk, deadline, and required action;
3. proposal content and material changes;
4. grounding, authority, limitations, and evidence; and
5. implementation detail.

The interface targets desktop architect work first. The decision workspace uses three panes at
wide widths, collapses evidence into a drawer at medium widths, and becomes a linear queue-detail
flow at narrow widths. No included action requires hover.

The implementation must provide:

- semantic landmarks and heading order;
- keyboard access to navigation, queue items, drawers, dialogs, tabs, and actions;
- visible focus and skip navigation;
- programmatic labels, descriptions, error association, and live status announcements;
- confirmation dialogs that identify the exact immutable effect;
- reduced-motion behavior;
- WCAG 2.2 AA contrast for text, controls, focus, and status states; and
- focus restoration after dialogs, route transitions, and stale-review refreshes.

## 11. Testing and verification

### 11.1 Contract and server tests

Python tests must prove:

- strict request and response model validation;
- fixture payloads validate against the same Pydantic contracts as governed-local payloads;
- unknown fields and invalid enum values fail closed;
- actor and tenant fields cannot be supplied as command authority;
- requester, reviewer, and operator projections do not leak restricted fields;
- command revisions, digests, roles, and idempotency keys reach the exact owning call;
- replay returns the committed result without a duplicate decision or effect;
- stale, unauthorized, `No Valid Plan`, transient, ambiguous, and malformed-payload errors map to
  distinct safe envelopes;
- demo reset is absent in governed-local mode; and
- provider and service exceptions do not leak through the adapter.

### 11.2 Browser unit and component tests

Vitest and Testing Library must cover:

- first-entry route selection;
- every setup stage and all three approval gates;
- decision queue filtering and keyboard selection;
- stakeholder-answer and access-preview review components;
- evidence drawer, lifecycle timeline, capability state, and managed-tool preview;
- no optimistic completion;
- stale-review refresh with preserved draft comment;
- ambiguous-outcome reconciliation;
- fixture-mode labeling on every route;
- disabled and explained `not_delivered` capabilities; and
- focus management and reduced-motion behavior.

### 11.3 Browser acceptance

Playwright runs the complete deterministic fixture journey:

1. enter a fresh workspace;
2. select PostgreSQL, review the binding, and confirm its immutability;
3. observe managed-service and source validation states;
4. upload the revenue-to-cash fixture package;
5. resolve one semantic question;
6. approve meaning, data product, and activation;
7. enter the returning-user inbox;
8. submit a stakeholder question as the requester, answer one clarification, and accept the
   clarified outcome;
9. review and approve a governed stakeholder-answer proposal, with the requester acceptance visible
   as a satisfied required authority;
10. verify that a proposal whose clarified outcome is unaccepted is shown as blocked on the
    requester and offers the architect no admitting action;
11. review a least-privilege access preview;
12. encounter a stale proposal and safely reload it;
13. inspect a `No Valid Plan` outcome; and
14. reset the demo.

The same suite repeats warehouse selection with ClickHouse through the immutable-binding and
capability screens. It does not claim destination equivalence that the fixture cannot prove.

Automated accessibility checks run on every major route, followed by keyboard-only assertions for
the complete decision path. Screenshot tests use deterministic fonts, viewport, fixture content,
clock, and motion settings.

### 11.4 Governed-local integration

Integration tests compose the real adapter with disposable repositories and existing public
service interfaces. Each wired capability must execute a new transaction and reach its terminal
authoritative state. Existing rows, fixture content, accepted commands, or green summary cards do
not prove the capability.

Live OpenMetadata, PostgreSQL, ClickHouse, Stripe, and later Superset checks remain opt-in and use
their approved setup, acceptance, and teardown procedures. A skipped live test is reported as an
explicit gap.

### 11.5 Repository gates

The repository runs two workflows today: the Python offline suite and the path-filtered warehouse
lifecycle gate. The console adds a third, and the plan must name its shape rather than leaving it to
the implementation:

- `npm ci` against the committed `package-lock.json`, so a drifted lock fails rather than resolves;
- TypeScript typecheck, ESLint, and Vitest;
- the JSON-Schema-to-TypeScript drift check, which is load-bearing because section 6.5 makes the
  generated types the browser's only field vocabulary; a drifted schema must fail the gate, not
  regenerate silently;
- the production build, proving Starlette can serve the compiled same-origin application; and
- Playwright, including the accessibility and screenshot assertions.

Playwright needs browser binaries and is the expensive job, so it is path-filtered on `apps/console`
with an always-present gate job, following the pattern the warehouse lifecycle workflow already
establishes: an undecided detector fails the gate rather than passing it. The Python gates are
unchanged and must not be weakened to accommodate the JavaScript ones. Before commit, the relevant
focused tests and the complete offline repository gates remain required.

## 12. Screenshot and demonstration package

The first visible milestone produces deterministic fixture-mode screenshots for:

1. warehouse foundation selection;
2. managed provisioning progress;
3. meaning review with OpenMetadata evidence;
4. data-product or activation review;
5. the returning-user command center;
6. a stakeholder-answer decision;
7. a least-privilege access preview; and
8. a `No Valid Plan` recovery state.

Screenshots use a 1440 by 1024 desktop viewport and a 1024 by 768 medium viewport where the evidence
drawer behavior matters. They are generated by Playwright from committed fixtures, stored under a
documented console asset directory, and must not contain local paths, credentials, real tenant
identifiers, or customer data.

The local demonstration command starts both the loopback API and Vite development server, prints
the authenticated local URL, and identifies fixture or governed-local mode. A separate production
build command proves that Starlette can serve the compiled same-origin application.

## 13. Delivery sequence

The implementation plan should divide the work into these reviewable milestones:

1. **Console foundation:** instantiate `apps/console`, pin toolchains, add the Starlette shell,
   define strict contracts, add the fixture backend, establish CI and repository structure gates.
2. **Visual shell and setup:** implement design tokens, navigation, A2 workbench, warehouse binding,
   managed-service/source status, process upload, and the three review gates.
3. **Decision workspace:** implement Q1 inbox, typed proposal reviews, evidence drawer, lifecycle
   timeline, OpenMetadata evidence, Superset preview, and failure states.
3a. **Requester surface:** implement typed intake, the clarification conversation, and
    clarified-outcome acceptance, with the architect's observe-intervene-take-over path. This is
    not optional polish: without it the stakeholder-answer journey cannot reach admission in
    milestone 6.
4. **Fixture acceptance and screenshots:** complete the deterministic walkthrough, accessibility
   checks, responsive behavior, production build, and screenshot package.
5. **Governed-local reads:** integrate workspace, warehouse, catalog, semantic, request, acquisition,
   operation, and evidence projections that existing services can authoritatively supply.
6. **Governed-local commands:** integrate the approved warehouse, process, review, intake,
   conversation, clarified-outcome acceptance, inbox, and retry commands only where owning service
   transactions and evidence are implemented; leave every later capability visibly unavailable.
7. **Integrated acceptance:** execute new local transactions through the browser and trace them to
   terminal service state. Record remaining Superset, transformation, destination, query, access,
   and delivery gaps explicitly.

Milestones 1 through 4 deliver the clickable product and screenshots. Milestones 5 through 7 turn
the same interface into an honest integrated view of the implementation completed to date.

## 14. Acceptance criteria

The console design is implemented when:

- first-time and returning architects land in the correct server-authorized journey;
- PostgreSQL and ClickHouse are selectable through one warehouse-binding experience and the
  confirmed choice is presented as immutable;
- setup exposes generated state and approval boundaries rather than behaving as an opaque form;
- meaning, data-product, and activation decisions bind exact revisions, digests, and roles;
- the inbox presents prioritized decisions with evidence and limitations in one workspace;
- a request can be submitted, clarified, and accepted in-product, and a proposal whose clarified
  outcome is unaccepted is blocked on the requester rather than offered to the architect;
- OpenMetadata and Superset appear as contextual managed capabilities without taking over product
  navigation;
- fixture and governed-local modes share one validated API contract and remain visibly distinct;
- no accepted, pending, ambiguous, unavailable, or fixture-backed operation appears completed;
- stale, unauthorized, `No Valid Plan`, transient, and malformed states fail closed and remain
  actionable where a permitted action exists;
- browser responses, logs, screenshots, and errors contain no credentials, private provider
  identifiers, raw source values, or unauthorized proposal content;
- the complete fixture journey passes browser, accessibility, and screenshot verification;
- every governed-local capability claim is backed by a new transaction traced to terminal state;
  and
- all existing Python offline gates and new JavaScript gates pass without skipped required checks.

Passing these criteria proves a real PillarMesh product experience and an honest bridge to the
implemented control plane. It does not prove production deployment, production identity,
unimplemented data-plane effects, or the complete managed-platform MVP.
