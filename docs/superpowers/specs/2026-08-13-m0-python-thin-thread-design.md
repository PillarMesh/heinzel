# PillarMesh M0 Python Evidence Thin Thread

> **Acceptance-readiness addendum:** The artifact persistence, privacy boundary, live harness, cleanup, and fault-matrix refinements in `2026-08-13-m0-acceptance-readiness-design.md` govern where this original design is incomplete or conflicting.

- Status: Proposed for implementation
- Date: 2026-08-13
- Governing specifications: EDC Foundational Architecture v0.3 and Revenue-to-Cash MVP Implementation Plan v1.4
- Scope: Phase 0 / Milestone 0 only
- Team: 4-6 engineers for six weeks, matching Implementation Plan v1.4 §33.3

## 1. Decision

PillarMesh M0 will be a uniform Python implementation that executes one real PostgreSQL-to-Snowflake snapshot contract from a desktop MCP client and produces an independently reconstructable evidence trace.

M0 is a modular monolith. Architectural components remain separate Python packages under their canonical repository boundaries, but they execute in one local process and share one local durable control store. This preserves the permanent contract, compiler-owned IIR, disposable execution graph, runtime, provider, and evidence boundaries without introducing service APIs, distributed queues, or deployment topology before they are needed.

This architecture is preferred over a throwaway script because M0 must validate durable artifact boundaries, and over separate services because network distribution would add failure modes without contributing to the M0 proof.

## 2. Objective and Acceptance Claim

M0 proves the following bounded claim:

> Given an explicitly approved immutable contract, PillarMesh can deterministically compile one legal plan, execute one bounded PostgreSQL snapshot through a signed graph, commit it idempotently to Snowflake, verify terminal visibility, and reconstruct the result from append-only evidence.

The proof uses dedicated real PostgreSQL and Snowflake accounts with synthetic, non-sensitive data. A passing unit test, responsive MCP server, successful SQL statement, existing Snowflake row, or healthy process is not sufficient. The acceptance run must create a fresh source row during the run and trace that row to a newly verified destination result.

M0 validates architectural boundaries and provider semantics. It does not validate the full MVP, production scale, high availability, continuous replication, conversational authoring quality, or autonomous planning.

## 3. Scope

### 3.1 Included

- One desktop-accessible MCP server using the official Python MCP SDK.
- One fixed-shape Integration Contract created through an MCP tool.
- Canonical contract serialization, versioning, digesting, verification, and explicit activation.
- One semantic IIR operation: deterministic relational projection with the fixed `status` to `order_status` rename.
- One explicit, versioned legality rule for PostgreSQL snapshot to Snowflake materialization.
- `No Valid Plan` for any absent rule, failed precondition, or Unknown required fact.
- One immutable, content-addressed, Ed25519-signed Execution Graph.
- One synchronous, single-process runtime with no parallel workers.
- One PostgreSQL provider that reads a bounded consistent snapshot.
- One Snowflake provider that stages, commits, resolves ambiguous commit outcomes, and verifies visibility.
- One append-only, hash-linked evidence trace stored durably in local SQLite.
- One reconstructed run report linking contract, IIR, legality decision, graph, source boundary, manifest, Snowflake receipt, and terminal visibility.
- Unit, property, mutation, integration, failure-injection, and live end-to-end tests appropriate to the thin thread.

### 3.2 Excluded

- PostgreSQL logical CDC or snapshot-to-CDC handoff.
- Salesforce, dbt, reconciliation, governed context, or finance data-product semantics.
- Multiple source or destination providers.
- Cost optimization, plan ranking, a general constraint solver, or minimal unsatisfiable cores.
- General conversational interpretation or model-authored executable semantics.
- Multiple runtime workers, distributed leases, a managed work queue, autoscaling, or a scheduler.
- Customer Relay, private-network deployment, or managed regional runtime cells.
- OAuth, multi-user identity, tenant isolation, production authorization, or production secrets.
- Operator console, deployment manifests, infrastructure selection, or production SLOs.
- Arbitrary SQL, filters, casts, joins, aggregation, masking, user code, or destination DDL.

### 3.3 Deviations from Implementation Plan v1.4

Two deliberate deviations from plan Table 17 are recorded here so they are reviewable rather than discovered.

**Desktop MCP authoring is pulled forward from Phase 1.** Plan Table 17 scopes Phase 0 as "one PostgreSQL order row to Snowflake" and places authoring surfaces in Phase 1 ("versioned six-contract bundle, dual-host baseline"). This design includes a real desktop MCP client flow in M0 because the activation-authority boundary — that client prose cannot confer approval and only exact verified digests can — is an artifact boundary M0 exists to validate, and retrofitting it later would revisit contract lifecycle, activation, and evidence.

The cost is the densest week in the plan. **The authoring surface is therefore the designated schedule release valve.** If the Week 5 gate is at risk, M0 falls back to a local CLI that submits the identical structured contract fields through the same contract service and activation path, and the MCP server moves to Phase 1. This fallback is pre-authorised: it requires no design review, only a recorded decision and an update to §16 gate 3. The acceptance claim in §2 is unchanged by the fallback because it names no authoring transport. Exercising the fallback does not weaken any other gate.

**The verification depth exceeds a literal reading of Phase 0.** Mutation coverage of every legality precondition, the fault-injection campaign, and second-engineer reproduction are heavier than "one row moves." They are retained because M0's purpose under plan §33.2 is to validate artifact boundaries, and an unmutated legality rule or an unexercised crash path validates nothing.

### 3.4 Parallel partner track

Plan Table 17 Phase 0 also carries "partner data/process inventory begins," and plan §17 starts partner sourcing in week one against a 12-week stop trigger. That work is out of scope for this document and is **not** paused during M0; it is tracked separately and owned outside the engineering team. Six weeks of engineering with no partner contact would consume half the screening clock. The Week 6 funding review reports partner-track status alongside the M0 gates.

## 4. Environment and Safety Boundary

M0 runs only against dedicated non-production resources:

- A PostgreSQL database containing a dedicated source schema and fixture table.
- A Snowflake account with a dedicated database, schema, warehouse, stage, target table, commit-ledger table, user, and least-privilege role.
- Synthetic rows containing no customer, employee, credential, or other sensitive data.
- Credentials supplied through environment variables or an ignored local environment file; credentials never enter contracts, graphs, evidence, logs, MCP results, fixtures, or Git.
- A dedicated Ed25519 M0 signing key stored outside the repository. The public key identifier may appear in artifacts; private key material may not.

The PostgreSQL role is read-only for the runtime. A separate fixture role creates the fresh acceptance row. The Snowflake role can use the dedicated warehouse, stage files under the M0 prefix, merge into the dedicated target, record commit receipts, and query visibility. It cannot administer the account or access unrelated databases and schemas.

The first implementation remains a local developer-operated process. This is an explicit M0 limitation, not evidence of a production deployment model.

### 4.1 Provisioning prerequisite

Environment provisioning is a **calendar dependency with a named owner, completed before Week 1 begins**, not an engineering task inside Week 1. Obtaining a Snowflake account, creating least-privilege roles, and clearing any procurement or IT review are queue-bound activities; if they land inside Week 1 the entire six-week plan cascades.

The prerequisite is complete when:

- Both PostgreSQL and Snowflake resources exist with the roles, objects, and grants in §4.
- **Two independent operator credential sets exist**, not one. Gate 12 requires a second engineer to reproduce setup and the acceptance run; a single shared credential cannot demonstrate that.
- The Ed25519 signing key is generated and stored outside the repository, with a documented rotation and revocation step.
- A written teardown procedure exists for every provisioned object.

Week 1 begins only when this prerequisite is signed off. If it is not met, the six-week clock has not started, and the delivery plan reports the slip rather than absorbing it.

### 4.2 Fixed fixture schema

M0 admits one source-table shape and its lossless Snowflake representation:

| Semantic field | PostgreSQL source | Snowflake destination |
| --- | --- | --- |
| Order identity | `order_id BIGINT PRIMARY KEY` | `order_id NUMBER(19,0) NOT NULL` |
| Customer reference | `customer_ref VARCHAR(65535) NOT NULL` | `customer_ref VARCHAR(65535) NOT NULL` |
| Amount | `amount NUMERIC(18,2) NOT NULL` | `amount NUMBER(18,2) NOT NULL` |
| Currency | `currency VARCHAR(3) NOT NULL` | `currency VARCHAR(3) NOT NULL` |
| Status | `status VARCHAR(65535) NOT NULL` | `order_status VARCHAR(65535) NOT NULL` |
| Observation time | `updated_at TIMESTAMPTZ NOT NULL` | `updated_at TIMESTAMP_TZ(6) NOT NULL` |

The currency column is deliberately `VARCHAR(3)` and **not** `CHAR(3)`. PostgreSQL `CHAR(n)` is blank-padded on storage and strips trailing spaces during comparison but preserves them on output, while Snowflake `VARCHAR(3)` does not pad. A padded source value against an unpadded destination value would produce a keyed value-digest mismatch that surfaces as a visibility integrity failure with a cause several layers removed from the symptom. Any future rule admitting a blank-padded character type must declare an explicit padding normalization in its canonical encoding rules before the type is allowed.

Customer reference and status use the same explicit 65,535-character bound on both providers.
Unconstrained PostgreSQL `TEXT` is not lossless into Snowflake's bounded string domain and is not
admitted. The provider row model enforces the same bound before encoding as a defense in depth.

The rename from `status` to `order_status` is the one M0 projection rename. Timestamps are serialized as UTC instants at microsecond precision. Currency and status are copied as values; M0 does not validate business vocabularies. Any additional selected column, nullable field, different precision, different key type, blank-padded character type, or incompatible destination definition is outside the rule and produces `No Valid Plan`.

The runtime ceiling is 10,000 rows, 64 MiB of deterministic uncompressed encoded data, one staged segment, and 15 minutes of runtime. These are safety limits, not performance objectives or production SLOs. Crossing any limit fails before destination commit with a stable `M0_RESOURCE_LIMIT_EXCEEDED` diagnostic.

M0 evidence, control state, and synthetic target rows have a 30-day local retention period. Successful staged segments are deleted within 24 hours after terminal verification. Failed or indeterminate segments are quarantined for seven days for diagnosis and then deleted. The witnessed acceptance package may be retained beyond 30 days only after its secret and row-value scan passes.

## 5. Technology Baseline

- Python 3.13, with the exact patch release recorded in `.python-version`.
- `uv` for workspace management, dependency locking, command execution, and reproducible environments.
- Pydantic v2 for closed, typed external and persisted models.
- Official MCP Python SDK v2, pinned to an exact patched stable release in `uv.lock`; no prerelease dependency is permitted.
- Psycopg 3 for PostgreSQL snapshot access.
- Snowflake Connector for Python for staging, transactional merge, query identifiers, and receipt verification.
- `cryptography` for Ed25519 signatures.
- Python `sqlite3` with explicit SQL migrations for local contract, activation, run, and evidence persistence.
- `pytest`, Hypothesis, and `mutmut` for test, property, and mutation verification.
- Ruff for formatting and linting and mypy in strict mode for static type checking.
- Structured JSON logging using the standard logging package. Logs aid diagnosis but never substitute for evidence.

Libraries are accessed behind PillarMesh-owned interfaces. Pydantic models define domain artifacts; driver-specific objects do not cross provider boundaries.

## 6. Repository Placement

M0 creates only component directories containing substantive implementation or tests:

```text
services/
  authoring-mcp/
  compiler/
    legality/
      fixtures/
      proof-notes/
      rules/
  contract/
  evidence/
  runtime/
providers/
  postgresql/
  snowflake/
packages/
  contract-model/
  execution-graph/
  iir/
  provider-sdk/
tests/
  integration/
  fault-injection/
  end-to-end/
```

Each directory is an independently importable Python distribution in one `uv` workspace. Dependencies point inward toward shared models and interfaces; provider packages never import the compiler, and the compiler never imports provider implementations.

No `common`, `utils`, generic connector, source/destination taxonomy, scheduler, or workflow package is introduced.

### 6.1 Declared components not created by M0

The repository component map declares `services/state` as the owner of epochs, partitions, leases, checkpoints, and cutover markers. **M0 deliberately collapses that responsibility into `services/runtime`.** The M0 runtime is single-process and single-worker, so run state, phase transitions, batch identity, and checkpoints have exactly one writer and one reader; extracting a separate component would violate the repository's own rule that shared code moves only after two real consumers demonstrate a stable boundary.

This collapse is recorded rather than silent, and it reverses on a stated trigger: `services/state` is created when the first of these arrives — a second concurrent runtime worker, a lease requiring fencing against another process, cross-process epoch ownership, or live migration admission. Any of those makes state a contended resource with more than one writer, which is the point at which the boundary earns its own component.

`services/contract` **is** created, because contract lifecycle and persistence have no other owner: `packages/contract-model` explicitly excludes lifecycle state (§9.1), and `services/authoring-mcp` delegates all domain work (§9.2). Every other component declared in the repository map — connection broker, provider registry, context exposure, reconciliation, dbt adapter, knowledge graph, relay — is out of M0 scope and is not created.

## 7. Artifact Model

### 7.1 Common identity rules

All authoritative artifacts are serialized as canonical JSON. Canonicalization fixes field names, ordering, number representation, timestamps, enum values, and omission rules. SHA-256 over the canonical bytes produces the artifact digest. Any semantic field change therefore produces a different digest.

Every artifact includes:

- `schema_version`
- its stable domain identifier where applicable
- the digest or identity of its authoritative parent
- `created_at` as a timezone-aware UTC timestamp
- the producer component and producer version

Unknown fields are rejected when reading an artifact unless its schema version explicitly defines forward-compatible extension fields. Runtime code never silently defaults a missing semantic field.

### 7.2 Integration Contract

The M0 contract is permanent and user-owned. Its semantic fields are:

- Contract identifier and monotonically increasing version.
- PostgreSQL connection handle, schema, table, and integer primary-key column.
- Explicit selected source columns and destination names.
- Snowflake connection handle, database, schema, target table, and stable destination key.
- Materialization mode fixed to `snapshot`.
- Commit behavior fixed to idempotent key-based upsert.
- Deletion behavior fixed to `not_observed`; M0 makes no deletion claim.
- Freshness objective recorded for evidence but not continuously enforced.
- Data classification fixed to synthetic/non-sensitive.
- Evidence retention label fixed to the M0 policy.

Connection handles are opaque configuration identifiers. They resolve to local settings outside the artifact and never contain credentials.

### 7.3 Verification, observation freshness, and activation

Verification compiles the exact contract version and produces an immutable activation summary containing:

- Contract digest.
- Resolved PostgreSQL and Snowflake object identities.
- Observed source and destination schemas and observation timestamps.
- IIR digest.
- Legality rule identifier and version.
- Execution Graph digest and signing-key identifier.
- Defaults and explicit limitations, including absent deletion and CDC semantics.
- Expected source and destination effects.

**Two distinct time bounds apply, and they govern different moments.** They are stated together here because treating them as one rule produces either a vacuous freshness check or an unreachable graph lifetime.

1. **Observation freshness, bounded at ten minutes, applies at verification.** The provider observations that legality is evaluated against must be no older than ten minutes at the moment the legality rule is evaluated. This bounds how stale the evidence behind an admission decision may be.
2. **Graph expiry, fixed at thirty minutes after verification, applies at activation.** It is the outer bound on how long an approved but unactivated graph remains usable.

Activation therefore requires all of the following, checked against durable state:

- The caller supplies the exact verified contract digest and activation-summary digest.
- The graph has not expired.
- The server **re-observes** the minimum provider facts and compares them field by field against the observations pinned at verification. Any difference in a legality-relevant fact is drift: activation is rejected and a new verification is required. Re-observation does not reset the ten-minute bound in rule 1, which is a property of the completed verification, not of activation.
- The draft is unchanged, the contract version is not superseded, and the digests are known.

Repeating the exact activation request returns the original run identifier and current state without creating another run. Client prose cannot stand in for approval.

#### 7.3.1 Connectivity and authorization admission

M0 separates configuration validity, provider connectivity, legality evidence, and execution
authorization. A successful TCP or driver connection is not sufficient evidence for execution,
and a successful verification is not a durable grant that may be reused after its observations or
graph expire.

The product path applies the following ordered gates:

1. **Configuration validation performs no provider I/O.** It requires every process-local setting,
   validates identifiers and the Ed25519 key, and resolves each opaque contract connection handle
   to exactly one configured provider. Credentials remain outside artifacts and are never included
   in a diagnostic.
2. **Verification performs metadata-only provider observations.** Authentication or network
   failure prevents publication of an activation summary. PostgreSQL observation proves that the
   declared object is a base table, records its exact schema and key, and requires effective
   `SELECT` with no effective table- or column-level write privilege. Snowflake observation proves
   that the target and commit ledger are accessible through metadata queries and records their
   exact kinds, columns, keys, and schema digests.
3. **Legality distinguishes a disproven fact from an unavailable fact.** A known schema,
   capability, key, or permission mismatch is `Unsatisfied`; metadata that cannot be established is
   `Unknown`. Either result produces `No Valid Plan`, creates no run, opens no source snapshot, and
   performs no destination mutation. A transport or authentication failure that prevents a usable
   observation fails verification rather than manufacturing `Unknown` evidence.
   Declared capabilities are exempt from the `Unknown` rule for the reason given in §7.3.2; they are
   adapter declarations, not environment observations.
4. **Activation re-observes both providers.** It rejects changed identities, schemas, keys,
   capabilities, or other legality-relevant facts before run creation. The comparison is against
   the exact observations and fingerprints pinned by the activation summary.
5. **Runtime verifies authority-bearing artifacts before credentials are resolved.** It verifies
   graph digest, signature, signing-key identifier, expiry, schema version, and artifact linkage,
   then performs the source drift probe of §11 before opening the snapshot.
6. **Provider operations remain fail-closed.** Authorization and permanent errors are not retried.
   Retryable or throttled reads receive only the bounded retry policy of §9.8. An ambiguous
   Snowflake commit is resolved through the commit ledger and is never treated as permission to
   repeat a write blindly.

The core M0 product observation deliberately does not execute a write-shaped Snowflake probe: such
a probe would mutate the destination or stage before explicit activation. It relies on metadata,
declared provider capabilities, and the legality rule. The witnessed harness strengthens this
boundary by attesting exact roles, ownership, grants, positive reads, and expected denials before
its first provider mutation; those requirements live in the acceptance-readiness addendum
§6.1.1. A permission removed after admission therefore still fails the affected provider operation
without false success, but M0 does not claim that product verification predicts every later
authorization failure.

M0 also has no second destination metadata probe immediately before stage or commit. Activation
re-observation bounds that drift window, and a later destination schema or permission failure is
terminal or resumable according to its provider classification. A reusable product preflight
artifact, explicit connection and statement budgets, normalized PostgreSQL driver-error
classification, active non-mutating Snowflake capability probes, and a pre-commit destination
drift probe are Phase 1 hardening requirements rather than hidden M0 claims.

#### 7.3.2 What a declared capability and a read-only admission do and do not prove

Two admission inputs are weaker than their names suggest, and M0 states both limits rather than
letting the gate names imply more.

**Declared capabilities are adapter declarations, not environment observations.** The capability
tuple in a provider observation names the behavior the adapter implements — snapshot read, stable
key order, drift probe, stage write, idempotent merge, commit ledger, visibility query — and is a
constant of the adapter, not a fact established against the account. It cannot be otherwise for the
destination while the product path declines to issue a write-shaped probe, which is the deliberate
decision above. Precondition 9 therefore proves that the resolved adapter implements the operators
the physical plan will run; it proves nothing about whether the configured principal is permitted to
run them. Authority evidence for the destination comes from the witnessed attestation of §6.1.1 and,
at execution time, from the operations themselves failing closed. A capability declaration is never
recorded as `Unknown` on the grounds that it was not probed, because probing is out of scope, not
unavailable; a declaration that a future adapter cannot make honestly must be removed from its
tuple rather than downgraded.

**Read-only admission is scoped to the effective role, not the session.** The PostgreSQL source
check evaluates effective privileges for `current_user`, which correctly accounts for role
inheritance and object ownership. It does not constrain the session: a login that owns the table and
has issued `SET ROLE` to a read-only role satisfies the check and can return to its own authority
with `RESET ROLE`. The product path accepts this because it authenticates with whatever principal it
is configured with and cannot know the operator's intent. The witnessed harness closes the gap for
the M0 acceptance claim by requiring `session_user` and `current_user` to both equal the declared
runtime principal, so an owner login that assumed a role is refused. A session-level constraint in
the product path — checking `session_user`, rejecting superusers, and rejecting principals able to
assume a writing role — is Phase 1 hardening.

### 7.4 IIR

The M0 IIR contains a single-source relation and one ordered projection node. Each projected field binds a source field identity to a destination field name without changing value semantics. The IIR excludes connection details, SQL text, staging locations, batch identifiers, and provider driver options.

IIR semantic identity is derived from the normalized source relation, selected fields, destination names, and declared snapshot semantics. Recompiling the same contract against identical pinned observations must produce byte-identical IIR.

### 7.5 Physical plan and legality decision

M0 has one physical-plan shape:

1. Read one bounded PostgreSQL snapshot in primary-key order.
2. Apply the IIR projection.
3. Encode one immutable staged segment.
4. Commit the segment to Snowflake by stable key.
5. Verify the committed batch and fresh-row visibility.

The physical plan declares its **required evidence set**: the event types that must exist for the run to be claimable, and the redaction class of each. This set is part of the plan, is evaluated by the legality rule, and is copied into the graph at compilation.

The physical plan remains compiler-owned and is not a shared package. The legality decision is a separate retained compiler artifact and is not inferred from successful execution.

### 7.6 Execution Graph

The compiler lowers an admitted physical plan to one immutable graph containing:

- Contract, IIR, physical-plan, and legality-decision digests.
- Provider declarations and minimum compatible versions.
- Deterministic operator sequence.
- Source relation and destination relation bindings.
- Projection mapping.
- Retry classifications and idempotent commit protocol identifier.
- The required evidence set carried from the admitted physical plan.
- Graph expiry fixed at 30 minutes after verification.
- The fixed M0 ceilings for rows, encoded bytes, staged segments, and elapsed runtime.

Graph compilation asserts one post-admission invariant before signing: the emitted graph requires exactly the evidence set the legality rule admitted, and no required event type carries a secret or a row value. Failure to hold is a compiler defect, not a `No Valid Plan`, and aborts compilation with a distinct diagnostic.

The compiler signs the canonical graph bytes using Ed25519. The runtime verifies the digest, signature, key identifier, expiry, artifact linkage, and supported schema version before accessing either provider.

The graph contains opaque connection handles but no credentials, raw SQL, natural language, or mutable execution state.

## 8. M0 Legality Rule

The only admitted rule is `M0-PG-SNAPSHOT-SNOWFLAKE-001`. It admits the physical plan only when every precondition is proven:

1. The source is a PostgreSQL base table accessible through the declared read-only handle.
2. The source has a non-null integer primary key with a stable total order.
3. Every selected column and destination column exactly matches the fixed M0 fixture mapping in section 4.2, including the exclusion of blank-padded character types.
4. Projection is the only semantic operation; destination names are unique.
5. The destination table and commit-ledger table already exist and match their declared schemas.
6. The destination key represents the projected source primary key without narrowing or coercion.
7. Snapshot, idempotent upsert, and `not_observed` deletion semantics exactly match the contract.
8. The provider observations this evaluation depends on are no older than ten minutes at the time of evaluation.
9. Both providers report the exact required M0 capabilities.
10. The physical plan's required evidence set is satisfiable by declared event types whose redaction classes exclude secrets and row values.

Precondition 10 is a property of the physical plan and its required evidence set, both of which exist before graph compilation. It is deliberately not a statement about the graph, which does not yet exist when legality is evaluated; the corresponding graph-level check is the post-admission invariant in §7.6.

Each precondition resolves to `Satisfied`, `Unsatisfied`, or `Unknown`. Only ten `Satisfied` results admit the plan. `Unsatisfied`, `Unknown`, an absent rule, or stale evidence produces `No Valid Plan` with:

- Rule identifier if one matched.
- Failed and Unknown preconditions.
- Evidence identifiers and observation ages.
- The smallest known contract or environment change that could permit recompilation.
- An explicit statement that no execution occurred.

The rule is shipped with a reviewed proof note, positive and negative fixtures, a mutation test for every precondition, and regression fixtures for all admitted provider-version pairs. Merge requires an independent reviewer as mandated by the repository legality policy.

## 9. Component Responsibilities

### 9.1 Contract model

Owns contract types, canonicalization, validation, compatibility, and digest calculation. It does not discover provider metadata, decide legality, compile graphs, or persist lifecycle state.

### 9.2 Contract service

Owns the contract lifecycle and its durable state: draft creation and mutation, immutable version promotion, verification and activation summaries, activation admission, and run creation. It enforces the digest, expiry, drift, and supersession checks in §7.3 and the single-active-run rule in §10.

It does not define contract types (that is `packages/contract-model`), compile or evaluate legality, access provider data, or execute operators. It is the only component that admits an activation, and therefore the only component that can create a run.

### 9.3 Authoring MCP server

Exposes the minimum host-neutral surface:

- Create a draft from structured contract fields.
- Read a draft and its validation diagnostics.
- Verify an exact draft version.
- Read an immutable activation summary.
- Activate exact verified digests.
- Read run state and the reconstructed evidence trace.

MCP handlers delegate domain work to the contract service and contain no contract-lifecycle, compiler, provider, or runtime logic. Structured results are authoritative; Markdown is a presentation derived from them.

M0 uses the local `stdio` transport. Its sole authorization boundary is the operating-system user who launches the server with the dedicated M0 configuration; it makes no multi-user or remote-authentication claim. Activation still requires exact verified digests so client language cannot confer authority. Correctness cannot depend on conversation history, hidden prompts, client-local files, or a model interpreting a successful tool response. The acceptance run records the desktop host and MCP protocol version as non-authoritative evidence attributes.

Because this component is the designated schedule release valve under §3.3, it holds no state and no domain rule that the CLI fallback would need to reimplement. Any logic that would have to be duplicated by the fallback belongs in the contract service instead.

### 9.4 Compiler

Resolves the contract against versioned provider observations, lowers it to IIR, evaluates the one legality rule, creates the physical plan, compiles and signs the graph, and records deterministic diagnostics. Compilation has no provider data access and causes no destination mutation.

Given identical canonical inputs and compiler version, compilation must reproduce identical semantic outputs and digests. Timestamps and trace identifiers live in surrounding evidence, not in reproducibility-sensitive compiler artifacts.

### 9.5 Provider SDK

Defines narrow typed protocols for:

- Capability declaration and observed-schema facts.
- Opening and closing a bounded snapshot.
- Iterating ordered row batches.
- Writing an immutable staged segment.
- Committing or resolving a batch by identity.
- Verifying destination visibility.
- Classifying errors as retryable, throttled, authorization, permanent, or ambiguous.

It also owns reusable conformance-test helpers. It contains no retry loop, persistence, scheduling, secrets, or provider-specific code.

Provider observation is distinct from provider execution. An observed fact — object kind, columns,
key, schema digest, effective source privileges, commit-ledger metadata — reports only what the
provider actually established, and must be `Unknown` rather than inferred from a successful
connection. The capability tuple is not an observed fact: it is the adapter's declaration of the
operators it implements, is constant for a given adapter, and is exempt from the `Unknown` rule
under §7.3.2. The two must not be conflated, and a declaration must be removed from the tuple rather
than downgraded to `Unknown` if it ever stops being honest.

Driver exceptions may not leak across a provider boundary once that provider adopts the common error
contract. The error classification says whether an operation may be retried; it never grants
authority and never weakens legality.

### 9.6 PostgreSQL provider

The provider opens one `REPEATABLE READ`, read-only transaction, observes and records the PostgreSQL snapshot identity, resolves the table and column identities, determines the primary-key bounds, and reads rows in stable primary-key order.

It also exposes a cheap **drift probe** used before extraction: the relation object identity and observed schema digest, retrievable without opening the snapshot transaction, so drift can be detected before a snapshot is held.

The source boundary contains the server and database identity in redacted form, table object identity, schema digest, snapshot identity, ordered key bounds, query-shape digest, row count, and observation timestamps. It contains no row values or credentials.

The provider never synthesizes SQL from unchecked identifiers. Identifiers come from verified metadata and are safely composed with Psycopg identifier primitives. Values are always bound parameters.

The metadata observation also evaluates effective access for the configured principal. M0 admits
the source only when `SELECT` is effective and table- and column-level write privileges are not.
The evaluation uses the effective-privilege functions, which account for role inheritance and object
ownership; a grant listing that enumerates only explicit grants would miss owners and superusers and
is not sufficient. The check is scoped to `current_user` and its limits are stated in §7.3.2.

The witnessed harness separately proves the exact `session_user`, `current_user`, ownership, grant
inventory, and expected-denial boundary because those operator-environment facts do not belong in
the portable provider observation artifact.

### 9.7 Snowflake provider

The provider writes a deterministic UTF-8 CSV segment under a run- and batch-scoped prefix. The format has a fixed column order, header, LF line endings, RFC 4180 quoting, decimal scale, UTC timestamp representation, and no nullable values. Extraction streams bounded row batches to the segment; it does not retain the complete table in memory. The manifest records the segment byte digest, ordered row-set digest, row count, schema digest, source-boundary digest, and a keyed value digest for the acceptance row. Hash inputs use domain-separated canonical encodings so concatenation cannot create an ambiguous digest. The target and commit-ledger tables are provisioned before execution; runtime DDL is prohibited.

Commit uses one Snowflake transaction to merge the batch into the target by the stable source key and insert the batch identity and manifest digest into the commit ledger. Replaying the same batch is a no-op or produces the same terminal receipt. A conflicting manifest under an existing batch identity is a permanent integrity failure.

If the client loses the commit response, the provider reconnects and queries the commit ledger by batch identity. Presence of the identical manifest resolves the outcome as committed; absence permits a bounded retry; conflicting data is indeterminate and fails closed. The receipt records batch identity, manifest digest, affected-row observations, Snowflake query identifiers, commit-ledger identity, and verification timestamp.

Terminal visibility is established by querying the destination under a fresh Snowflake statement and proving that the acceptance row's stable key and deterministic value digest match the source manifest. The evidence contains the keyed digest, not the row values. A successful merge response alone is not terminal proof.

The product observation authenticates with the configured account, user, role, warehouse,
database, and schema and reads target and ledger metadata. It does not stage a probe file or issue a
test merge before activation, so its `stage_write` and `idempotent_merge` capabilities are
declarations under §7.3.2 rather than proven authority. The witnessed harness therefore owns the
exact current-user/current-role, role-membership, object-ownership, `SHOW GRANTS`, positive-read,
and expected-denial attestations required for the M0 live claim.

### 9.8 Runtime

The runtime verifies the graph before resolving connection handles. It then runs the pre-extraction drift check in §11 step 8. It executes operators sequentially with bounded batches and writes durable state at the source-boundary, manifest, commit-receipt, and visibility checkpoints.

Under §6.1 the runtime also owns M0 run state: phases, attempts, batch identities, and checkpoints. This ownership is provisional and reverts to `services/state` on the triggers listed there.

M0 permits retries only for operations classified as retryable or for an ambiguous Snowflake commit whose ledger query proves the batch absent. A retrying operation receives at most three total attempts, fixed delays of one and five seconds, and a two-minute wall-clock budget. The 15-minute run ceiling remains authoritative. The runtime never retries an ambiguous write blindly. Runtime restart resumes from durable checkpoints using the same run and batch identities; it does not create a new semantic run implicitly.

The runtime cannot reinterpret the contract, modify the graph, choose another plan, weaken a precondition, create provider-local retries, or report success when required evidence is missing.

### 9.9 Evidence service

Evidence is a typed append-only event stream distinct from logs and mutable run state. Each event includes tenant-fixed M0 scope, contract and run identities, event type, producer, producer version, UTC occurrence time, parent artifact digests, redacted attributes, previous event digest, and its own digest.

SQLite triggers and repository code reject updates and deletes from evidence tables. Per-run hash linkage exposes accidental mutation or truncation. M0 does not claim external tamper resistance because the signing key and SQLite store are locally operated; the run report states this limitation.

**The hash chain is per run and continues across process restart.** A resumed run appends to the existing chain from the last durably committed event; it never starts a second chain or re-anchors. Because each event is appended in the same SQLite transaction that advances its checkpoint, a crash mid-append leaves neither a partial event nor an advanced checkpoint. A run whose chain fails verification is non-conforming regardless of destination state, and the failure is reported against gate 2 rather than repaired.

Required event types cover draft creation, verification, legality decision, graph signing, activation, graph verification, drift revalidation, snapshot opening, extraction completion, manifest creation, commit attempt, commit resolution, visibility verification, terminal success or failure, and trace reconstruction.

## 10. Durable Local State

One SQLite database stores:

- Draft and immutable contract versions.
- Verification and activation summaries.
- Provider observations used by compilation.
- Compiler artifacts and their canonical bytes.
- Runs, phases, attempts, batch identities, and checkpoints.
- Append-only evidence events.

State transitions use explicit transactions and compare-and-set predicates. A terminal run cannot return to a non-terminal state.

Only one active M0 run is permitted per store, because the runtime is single-process and single-worker. Repeating the exact activation is idempotent and returns the existing run; activation of a different contract version while that run is active receives a deterministic busy response rather than being silently queued. **The constraint is a property of a store, not a global lock**: each integration, fault-injection, and end-to-end test provisions its own SQLite database and its own run identifiers, so the test suite parallelises normally and the operator flow remains single-run. Tests never share the developer's operational store.

SQL migrations are ordered, checksummed, and applied explicitly. The database refuses to start against a newer unsupported schema. Migration code never rewrites evidence payloads.

## 11. End-to-End Data Flow

1. The acceptance fixture command inserts a uniquely identified synthetic order row through the PostgreSQL fixture role and records its primary key outside PillarMesh evidence.
2. The desktop client calls the MCP draft operation with the fixed M0 contract shape.
3. The contract service validates and persists immutable contract version 1.
4. Verification connects to both providers for metadata-only observations, establishes the product
   connectivity and permission facts of §7.3.1, lowers the contract to IIR, evaluates
   `M0-PG-SNAPSHOT-SNOWFLAKE-001` against observations no older than ten minutes, creates the
   physical plan, signs the graph, and emits an activation summary.
5. The client displays the exact summary and activates by passing both approved digests.
6. The contract service rechecks digests, graph expiry, supersession, and configured operator identity, **re-observes the minimum provider facts and rejects any drift against the pinned verification observations**, then creates one run.
7. The runtime verifies the graph digest, signature, key identifier, expiry, artifact linkage, and schema version before resolving credentials.
8. The runtime runs the pre-extraction drift check: it reads the PostgreSQL drift probe — relation object identity and schema digest — without opening the snapshot, and compares it to the graph's pinned source observation. A mismatch is a terminal failure before any snapshot is held or data is read.
9. PostgreSQL opens a bounded consistent snapshot and emits its source boundary.
10. The runtime projects rows in primary-key order and creates one deterministic staged segment and manifest.
11. Snowflake uploads the segment and transactionally merges it with a batch-ledger receipt.
12. A fresh Snowflake query verifies the acceptance row and its value digest.
13. The evidence service emits terminal success only after all mandatory evidence exists and its hash chain verifies.
14. The MCP trace resource reconstructs the complete lineage from contract through terminal visibility.

Steps 6 and 8 are both drift checks and both are required. Step 6 is cheap, occurs before a run exists, and prevents activating against a changed environment. Step 8 occurs after the run and graph verification, closes the window between activation and extraction, and is the last point at which failure costs nothing.

## 12. Failure Semantics

| Failure | Required behavior |
| --- | --- |
| Missing or malformed local configuration | Reject before provider construction or resolution; identify names, never values. |
| Provider network or authentication failure during verification | Publish no activation summary and create no run; preserve a value-independent diagnostic. |
| PostgreSQL effective access is not read-only | Return `No Valid Plan`; open no source snapshot and perform no destination mutation. |
| Required provider metadata is unavailable | Record the dependent legality fact as `Unknown` when an observation exists; otherwise fail verification without fabricating evidence. |
| Snowflake authorization failure during an operation | Do not retry as transient; preserve the authorization classification and never report success. |
| Invalid or changed contract | Reject before compilation; preserve diagnostics and immutable prior versions. |
| Unsupported type or legality precondition | Return `No Valid Plan`; create no run and access no source data. |
| Stale activation digest | Reject activation; require a new verification summary. |
| Expired graph at activation | Reject activation; require a new verification summary. |
| Provider drift detected at activation | Reject activation; create no run; require a new verification summary. |
| Provider drift detected before extraction | Terminal failure before the snapshot opens; no source read and no destination mutation. |
| Invalid graph signature, expiry, or linkage | Fail before resolving provider credentials. |
| PostgreSQL connection or snapshot failure | Record terminal failure; create no manifest or destination mutation. |
| Staged-file or manifest integrity failure | Quarantine the batch and prohibit commit. |
| Retryable Snowflake staging failure | Apply the three-attempt, two-minute retry budget using the same batch identity. |
| Lost Snowflake commit response | Resolve through the commit ledger before retrying. |
| Conflicting committed batch identity | Mark integrity failure and stop; never overwrite the receipt. |
| Visibility mismatch | Mark run non-conforming even if commit succeeded; retain evidence for diagnosis. |
| Evidence append failure before commit | Stop before destination commit. |
| Evidence append failure after destination commit | Resolve destination state, retain run as non-terminal/non-conforming, and never claim success. |
| Evidence hash chain fails verification | Mark run non-conforming regardless of destination state; do not repair the chain. |
| Process crash | Restart from the last durable checkpoint with the same run and batch identities, appending to the existing evidence chain. |

No exception handler may convert a provider or evidence failure into success. Every terminal failure has a stable diagnostic code, causal event, and last confirmed checkpoint.

## 13. Verification Strategy

### 13.1 Static and unit verification

- Ruff formatting and linting.
- Strict mypy checks across every workspace package.
- Contract and artifact schema tests.
- Canonicalization golden vectors and digest stability across repeated processes.
- Ed25519 signing, verification, tamper, wrong-key, expiry, and incompatible-version tests.
- SQLite transition and append-only enforcement tests.
- Provider error-classification branch tests.

### 13.2 Property and mutation verification

- Property tests generate supported contract projections and verify canonical round trips and deterministic IIR/graph output.
- Negative property tests generate duplicate destination names, missing keys, narrowing types, extra fields, blank-padded character types, and unsupported semantics and require rejection.
- Each of the ten legality preconditions has at least one negative fixture.
- Mutation testing must demonstrate that negating, removing, or bypassing any legality precondition causes a test failure.
- A dedicated test asserts the §7.6 post-admission invariant: a graph whose required evidence set diverges from the admitted plan's fails compilation rather than signing.

### 13.3 Provider integration verification

- PostgreSQL tests use a real compatible server to verify snapshot repeatability, stable ordering, identifier safety, schema drift detection through the drift probe, permission denial, and connection interruption.
- Snowflake tests use the dedicated real account to verify staging integrity, duplicate-batch replay, conflicting manifest rejection, transactional ledger behavior, ambiguous commit resolution, and fresh visibility queries.
- Integration tests isolate their schemas or run identifiers, use their own SQLite store per §10, and clean up only resources they created.
- Product observation tests cover unreachable endpoints, invalid credentials, unavailable metadata,
  effective PostgreSQL read-only access, and Snowflake target/ledger metadata access without a
  pre-activation write.
- Acceptance-preflight tests cover exact identities and grant shapes, assumed-role rejection,
  unexpected table- and column-level privileges, object ownership, positive reads, expected
  denials, and concurrent provider-admission refusal.

### 13.4 Authoring-surface verification

These apply to the MCP server, or to the CLI fallback if §3.3 is exercised:

- Tool or command schemas reject malformed and extra fields.
- Draft/read/verify/activate/observe operations work from a real desktop MCP host.
- Activation with an altered contract digest or summary digest fails.
- Activation after graph expiry fails.
- Activation after induced provider drift fails and creates no run.
- Repeated activation with the same verified digests returns the original run identifier and terminal state; it never creates another effect.
- Output contains no connection strings, tokens, private keys, or source row values.

### 13.5 Fault injection

- Kill or interrupt execution before extraction, after manifest persistence, during Snowflake commit, after commit before receipt persistence, and after receipt before visibility verification.
- Corrupt staged bytes after manifest creation.
- Revoke PostgreSQL read permission and Snowflake stage/merge permission independently.
- Inject an evidence-store write failure before and after destination commit.
- Modify the graph, contract, and provider observation independently after signing.
- Restart a partially completed run and verify that the evidence chain continues rather than re-anchoring.

Every injected failure must end in a deterministic recoverable or terminal state with no false success and no unexplained destination divergence.

### 13.6 Live acceptance run

The release candidate must:

1. Start from an empty run identifier and a destination with no row bearing the new acceptance key.
2. Insert a fresh synthetic PostgreSQL row after the test begins.
3. Author, verify, and activate the contract through a real desktop MCP client, or the CLI fallback if §3.3 was exercised.
4. Reach terminal Snowflake visibility for that exact key.
5. Reconstruct and validate the evidence hash chain.
6. Show links among the contract digest, IIR digest, legality decision, graph digest, source snapshot, manifest, batch receipt, Snowflake query identifiers, and visibility proof.
7. Replay the same batch identity and demonstrate no duplicate destination effect.
8. Run one rule-precondition-negative contract and demonstrate `No Valid Plan` with no provider data read or Snowflake mutation.

The acceptance evidence package contains sanitized artifact JSON, the reconstructed event timeline, test results, dependency lock digest, source and destination schema digests, and a limitations statement. It contains no credentials or row values.

## 14. Operational Observability

M0 records structured logs, metrics, and evidence separately.

Minimum metrics are compile duration, provider-observation age, rows and bytes extracted, staged bytes, stage duration, commit duration, visibility-verification duration, retry count, evidence append duration, and total source-observation-to-destination-visibility time.

Every log line includes run identifier where one exists, component, event name, and diagnostic code. Secret-like fields and SQL parameter values are redacted at their source. The test suite includes a scan of logs, MCP results, evidence, and generated artifacts for configured credential canaries.

## 15. Six-Week Delivery Plan

Team is 4-6 engineers. The six-week clock starts only after the §4.1 provisioning prerequisite is signed off; provisioning time is not absorbed into Week 1. The partner track in §3.4 runs in parallel and is not staffed from this team.

### Week 1 - Foundation and environment proof

- Establish the `uv` workspace, quality gates, package boundaries, and dependency lock.
- Implement contract canonicalization, digests, SQLite migrations, and environment validation.
- Prove direct read-only PostgreSQL and scoped Snowflake connectivity from both operator credential sets without moving data.
- Exit: reproducible environment setup, passing repository structure test, and no credential leakage.

### Week 2 - Semantic and compiler spine

- Implement the contract service lifecycle, one IIR projection, provider observations, legality rule, proof note, diagnostics, physical plan with its required evidence set, signed graph, and activation summary.
- Implement the observation-freshness, graph-expiry, and drift rules in §7.3.
- Add positive/negative legality fixtures and mutation coverage.
- Exit: identical inputs reproduce identical artifacts; one unsupported contract returns `No Valid Plan` without provider data access; an induced drift rejects activation.

### Week 3 - Real provider path

- Implement PostgreSQL consistent snapshot, drift probe, and source boundary.
- Implement deterministic segment encoding, Snowflake staging, transactional merge ledger, ambiguous commit resolution, and visibility query.
- Run provider integration tests against the real accounts.
- Exit: a fixture row moves idempotently with provider receipts, independent of the authoring surface and the runtime coordinator. This is a harness result and is explicitly not the M0 claim.

### Week 4 - Runtime and evidence

- Implement graph verification, the pre-extraction drift check, sequential operator execution, checkpoints, bounded retry, restart behavior, append-only evidence with cross-restart chain continuation, and trace reconstruction.
- Add crash and evidence-failure injection around every commit boundary.
- Exit: interrupted runs resume or fail deterministically, never report false success, and produce one continuous evidence chain.

### Week 5 - Authoring surface and complete thin thread

- Implement the minimal MCP tools/resources and connect one real desktop host.
- Exercise draft, verify, explicit activation, run observation, and evidence reconstruction end to end.
- Add stale-digest, expired-graph, induced-drift, malformed-input, duplicate-activation, and secret-canary tests.
- Exit: one desktop-authored fresh transaction reaches verified Snowflake visibility.
- **Release valve:** if this exit is at risk at the start of Week 5, invoke the §3.3 CLI fallback, record the decision, and continue. Do not compress Week 6.

### Week 6 - Hardening and funding evidence

- Run the full fault campaign, mutation suite, dependency/security checks, reproducibility tests, and clean-room setup rehearsal using the second operator credential set.
- Execute the witnessed live acceptance run and assemble the sanitized evidence package.
- Record measured latency, engineering effort, provider friction, architectural deviations, and unresolved risks.
- Exit: every M0 gate below passes, or the funding review receives an explicit failed gate with evidence.

## 16. M0 Exit Gates

M0 passes only when all gates are satisfied:

1. A fresh synthetic PostgreSQL row created during the acceptance run is visible in Snowflake under the expected key and value digest.
2. The run is linked end to end through contract, IIR, legality decision, graph, snapshot boundary, manifest, receipt, and visibility evidence, and its hash chain verifies as one continuous chain.
3. The contract was authored, verified, and explicitly activated through the desktop MCP client, or through the CLI fallback with the §3.3 decision recorded.
4. Identical compiler inputs reproduce byte-identical IIR and graph content.
5. The runtime rejects a modified, expired, wrongly signed, or incompatibly versioned graph before provider access.
6. Activation is rejected for an expired graph and for induced provider drift, and pre-extraction drift fails before the snapshot opens.
7. Replaying the committed batch creates no duplicate effect.
8. An ambiguous Snowflake commit is resolved through durable batch identity rather than blind retry.
9. At least one missing legality precondition returns `No Valid Plan` and causes no source data read or destination mutation.
10. Mutation tests detect bypass of every legality precondition, and the post-admission evidence-set invariant fails compilation when violated.
11. Required crash and evidence-store failures produce no false success or unexplained divergence, and a restarted run continues its evidence chain.
12. Credential-canary scans find no secrets in Git, logs, authoring output, artifacts, SQLite evidence, or the acceptance package.
13. Repository structure validation, unit, property, integration, fault-injection, end-to-end, lint, and strict type checks pass from a clean checkout.
14. A second engineer reproduces setup and the acceptance run from the checked-in instructions and locked environment, using the second operator credential set.

Passing M0 authorizes planning for Phase 1 story authoring. It does not authorize production data, customer onboarding, CDC, additional providers, distributed execution, or deployment.

## 17. Risks and Containment

| Risk | Containment | M0 decision signal |
| --- | --- | --- |
| Environment provisioning delays the start | Pre-Week-1 prerequisite with a named owner and two credential sets | Prerequisite unmet at the planned start date; the clock has not started |
| M0 scope exceeds six weeks because authoring was pulled forward | Pre-authorised CLI fallback at the Week 5 boundary | Week 5 exit at risk at the start of the week |
| Python driver behavior obscures provider semantics | Wrap drivers narrowly; retain query IDs, manifests, and conformance tests | Provider receipts cannot reproduce or resolve outcomes reliably |
| Uniform Python later limits worker throughput | Keep runtime/operator interfaces narrow and benchmark rows, bytes, memory, and latency | Measured CPU or memory behavior requires a later ADR for an isolated worker implementation |
| SQLite is mistaken for production state architecture | Name it as local M0 persistence and isolate it behind repositories | Phase 1 requires multi-process or managed durability semantics |
| Run state stays collapsed into the runtime past its trigger | §6.1 split triggers reviewed at the Phase 1 design gate | A second worker, cross-process lease, or migration admission appears |
| MCP SDK major-version churn destabilizes authoring | Pin exact patched stable release and test protocol/tool schemas | Required host cannot complete the fixed flow reproducibly |
| Snowflake transaction response is ambiguous | Use immutable batch identity plus commit ledger and positive resolution | Ledger cannot distinguish committed, absent, and conflicting outcomes |
| Evidence is locally mutable by the operator | Hash-link events and state the limitation explicitly | Production funding requires externally durable/tamper-evident storage design |
| M0 expands into the full MVP | Enforce exclusions and six-week exit gates in review | Work does not directly support a listed gate |
| Partner screening clock consumed by M0 engineering | Parallel track in §3.4 owned outside the engineering team | No partner clears screening by the plan's 12-week trigger |

## 18. Required Review and Change Control

The first implementation change must include this design, the corresponding detailed implementation plan, and an ADR selecting the Python M0 technology baseline. The ADR must state that uniform Python is the current implementation decision, while provider and runtime interfaces preserve the option for a later evidence-driven language split. It must also record the §6.1 collapse of `services/state` into `services/runtime` and its reversal triggers, and the §3.3 deviations from Implementation Plan v1.4 Table 17.

Any change that widens semantics, adds a legality rule, adds a provider, introduces a new top-level or governed component, collapses or splits a declared component, moves execution across a process boundary, or changes the acceptance claim requires explicit design review. Legality-rule changes additionally require the proof, fixture, mutation, regression, and independent-review gates in `AGENTS.md`.

Invoking the §3.3 CLI fallback is pre-authorised and requires only a recorded decision and an update to gate 3; it is not a design change.

## 19. Definition of Done

M0 is done when the witnessed acceptance run and every exit gate pass from the committed code and locked dependencies, the evidence package is independently reviewable, the repository contains no secrets or sensitive data, and the funding review records a clear proceed/stop decision for Phase 1.

Work that merely scaffolds packages, produces a successful SQL response, displays an MCP success message, or finds a pre-existing Snowflake row is incomplete.
