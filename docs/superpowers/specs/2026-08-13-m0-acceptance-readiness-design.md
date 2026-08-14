# PillarMesh M0 Acceptance-Readiness Design Addendum

**Status:** Approved for implementation

**Date:** 2026-08-13

**Applies to:** `docs/superpowers/specs/2026-08-13-m0-python-thin-thread-design.md` and Task 12 of `docs/superpowers/plans/2026-08-13-m0-python-thin-thread.md`

## 1. Purpose

The M0 implementation proves the deterministic thin thread with fake providers, but a preflight review found that supplying real credentials would not yet produce the independently reviewable acceptance evidence required by the M0 exit gates. This addendum defines the smallest cross-component correction required before a witnessed PostgreSQL-to-Snowflake run.

This addendum refines the artifact-lifecycle, live-acceptance, fault-injection, and evidence-package requirements of the approved M0 design. It does not widen the supported contract, add a provider, introduce distributed execution, create a scheduler, or authorize production data.

## 2. Decisions

1. Compiler verification returns and persists a complete content-addressed artifact bundle, not digest-only references to discarded intermediates.
2. Canonical evidence artifacts contain semantic and attributable evidence only. Raw local paths, raw source key values, and the raw acceptance key are runtime-private state and never enter canonical artifacts, evidence events, logs, authoring output, run records returned by the authoring surface, or the sanitized package.
3. Runtime-private state remains in the existing local SQLite boundary for M0, in one table that no read API projects. It is not an evidence artifact and is never exported.
4. Multi-row writes whose partial application would produce unbacked evidence are performed through one explicit store transaction. The store owns that primitive; callers do not compose transactions out of individually committing methods.
5. Schema version 2 is the only supported evidence-store schema. A version 1 database is refused at open with an explicit diagnostic rather than migrated, because no release, production data, or external operator predates this addendum.
6. The sanitized package contains exact canonical bytes for every exportable artifact. It does not redact canonical bytes after hashing. Privacy is enforced by artifact design and a fail-closed export allowlist.
7. Gate dispositions are recorded only in the committed report. They are not a package payload, because the package-verification gate cannot be an input to the package it reports on.
8. One checked-in acceptance harness owns the bounded live transaction, assertions, resource inventory, package export, verification, and cleanup scheduling. The three provider integration tests remain diagnostic harnesses and do not constitute M0 acceptance.
9. The automated acceptance uses the pre-authorized CLI fallback and records that decision. A real desktop MCP-host run remains an optional additional observation and cannot replace the checked-in acceptance harness.
10. The current legality rule is not widened. Its proof note cannot move to reviewed until a reviewer other than the rule author verifies the rule, fixtures, type correspondence, commit-ledger assumption, and mutation evidence.

## 3. Scope

### 3.1 In scope

- Persisted canonical IIR, physical plan, admitted legality decision, signed graph, and all existing runtime evidence artifacts.
- A versioned separation between safe canonical artifacts and runtime-private materialization state.
- A deterministic package index, exporter, verifier, and sensitive-value scanner.
- A single live acceptance harness covering fresh absence, post-start insertion, execution, independent visibility, replay, a legality-negative case, evidence verification, and resource tracking.
- Exact operator setup, variable inventory, denial probes, CLI-fallback record, per-run cleanup, retention, and second-operator instructions.
- The missing runtime-before-provider, crash, evidence-store, corruption, permission, and tamper tests required by the approved design.
- A recorded independent legality review.

### 3.2 Out of scope

- Production secrets or customer data.
- A general artifact service, remote evidence store, scheduler, workflow engine, or `services/state` split.
- Encryption or signing of the entire package. M0 relies on canonical digests, the signed execution graph, and a hash-chained event trace; the limitation that the local operator can delete or replace a complete package remains explicit.
- Automatic provisioning or destructive account teardown.
- A schema migration framework. M0 has one supported schema version and refuses older stores (§4.3.1); a stepwise migration path is Phase 1 work, first needed when a store exists that someone other than its author cannot discard.
- CDC, deletes, multiple segments, additional mappings, additional providers, concurrent workers, or autonomous AI decisions.

## 4. Artifact and State Boundaries

### 4.1 Compiler artifact bundle

`compile_contract` returns a `CompilationBundle` containing:

- the canonical `IntentIR`;
- the canonical `PhysicalPlan`;
- the canonical admitted `AdmittedPlan` legality decision; and
- the `SignedExecutionGraph`.

The bundle also exposes the contract digest and verification time needed by the activation summary. Digests are derived from the contained canonical objects rather than stored as independent, caller-supplied values. Validation fails if any graph parent digest differs from the digest of its bundled object.

`CompilationBundle` replaces `CompilationResult`. The `compilation_result` artifact kind is retired: it is no longer written, is not on the export allowlist, and is not a digest source for any other artifact. A store containing `compilation_result` rows from earlier development is a version 1 store and is refused under §4.3.1.

`AdmittedPlan` gains an explicit `physical_plan_digest` field bound to its embedded `PhysicalPlan`. Without it the admitted-decision-to-plan edge of §4.4 has no digest field to verify and the verifier would have to recompute a parent link from a child object it is trying to authenticate. Construction fails if the field and the embedded plan disagree.

`ContractService.verify` persists these artifact kinds before publishing the activation summary:

| Kind | Identity | Parent or consumer |
| --- | --- | --- |
| `integration_contract` | contract digest | IIR and activation summary |
| `provider_observation` | observation digest | legality decision and graph |
| `intent_ir` | IIR semantic digest | graph |
| `physical_plan` | canonical plan digest | admitted legality decision and graph |
| `legality_decision` | canonical decision digest | graph |
| `signed_execution_graph` | canonical signed-envelope digest | activation summary and run |
| `activation_summary` | summary digest | activation |

The signed-envelope digest identifies the complete signed artifact; its embedded `graph_digest` separately identifies the unsigned graph content and must verify against it. All writes are content-addressed and immutable.

The integration contract, provider observations, complete compiler bundle, activation summary, and summary reference are written through one §4.5 multi-artifact publication transaction, so no summary can become visible without every exact parent. Before any write, the transaction validates every payload against its declared digest. During writes, immutable conflict checks reject a digest already bound to different bytes. The activation summary and its reference are written last, then every expected parent, summary, and reference row is queried inside the transaction before `COMMIT`. Any missing or mismatched row, write failure, or commit failure rolls back the publication. Existing draft storage is not treated as sufficient publication evidence.

`No Valid Plan` continues to persist only the contract, observations, and rejected legality decision and still creates no run.

### 4.2 Safe segment evidence

`SegmentManifest` advances to schema version `2` and contains only:

- batch identity;
- deterministic segment filename, fixed to `segment.csv` for M0;
- segment digest and row-set digest;
- row count and encoded byte count;
- destination schema digest;
- source-boundary digest; and
- acceptance-value digest.

It does not contain a local path or raw acceptance key. The filename is a provider-independent logical name, not a filesystem locator.

`VisibilityProof` advances to schema version `2` and contains batch identity, acceptance-value digest, provider query identifier, and verification time. It does not contain the raw acceptance key.

`SourceBoundary` advances to schema version `2` and replaces the raw `key_min` and `key_max` bounds with a single `key_range_digest` over the ordered pair. The acceptance key is the primary key of a row inserted into the source table during the run, so on nearly every acceptance run the raw maximum bound *is* the raw acceptance key. Exporting version 1 boundaries would therefore publish the value §4.2 removes from the manifest, and would be caught by the §5.3 scanner as a failure of every otherwise-successful run. The digest preserves the reviewable property — that two runs covered the same or different key ranges — without publishing a key. Row count, snapshot identity, query-shape digest, and both timestamps are unchanged; they carry no key value.

### 4.2.1 Provider protocol changes

Removing `segment_path` and `acceptance_key` from the manifest changes the provider contract, not only the artifact:

- `RuntimeDestination.stage(segment: Path, manifest: SegmentManifest)` keeps receiving the local path as its own argument. The Snowflake stage path is derived from `manifest.segment_filename`, not from a path inside the manifest.
- `RuntimeDestination.commit_or_resolve(manifest: SegmentManifest)` is unchanged in signature and derives its staged-file reference from `manifest.segment_filename`.
- `RuntimeDestination.verify_visibility(manifest: SegmentManifest, acceptance_key: int)` takes the key as an explicit argument, bound as a query parameter. The runtime supplies it from private state; it is never read back out of an artifact.
- `SegmentEncoder` keeps its existing `acceptance_key` argument and returns a version 2 manifest.

The `provider-sdk` protocols, both real providers, and every fake used in unit, end-to-end, and fault-injection tests move together. This is a cross-cutting change: 78 references to `segment_path` or `acceptance_key` exist across 21 Python files, and the enumeration of those sites — not the passing of any one test — is what proves the change complete.

The Snowflake provider's canonical receipt uses a hashed opaque ledger identity rather than a qualified object name. No raw SQL parameter is logged or persisted.

### 4.3 Runtime-private state


The evidence store adds a `run_private_state` table containing, at most:

- `run_id`;
- raw acceptance key; and
- local segment path.

This table supports restart and is not addressable through `load_artifact`, trace APIs, CLI/MCP resources, or package export. It is reachable only through two narrow store methods, one that writes it inside a §4.5 transaction and one that reads it for resume.

The `runs.acceptance_key` column is removed and the key moves to this table. Relocating the storage is not sufficient on its own: `RunRecord` currently carries `acceptance_key`, and the authoring surface returns a serialized run record from both `activate` and `get_run`, so the raw key is printed as CLI and MCP JSON output today. `RunRecord` therefore drops the field, and the authoring surface exposes no substitute for it. `Runtime.resume` reads the key from `run_private_state` rather than from the run record. Without this removal the §5.3 CLI/MCP-output scan fails every acceptance run.

Writes that establish an extracted segment persist the safe manifest, its source boundary, the private path, and the extraction checkpoint and event in one §4.5 transaction. A restart therefore cannot observe a checkpoint whose required safe or private state is absent.

### 4.3.1 Schema version and version 1 refusal

The evidence store advances to schema version `2`, comprising the version 1 tables less `runs.acceptance_key`, plus `run_private_state`.

There is no version 1 to version 2 data migration. Version 1 canonical manifests and visibility proofs embed raw paths and raw keys, so their runs could never be exported; migrating their private values would produce a database whose evidence is still unexportable. No release, production data, external operator, or second machine predates this addendum, so the only version 1 databases that exist are local development scratch state. A version 1 database is refused at open with a diagnostic naming the found version, the required version, and the instruction to start from a fresh state path.

This keeps the existing single-checksum migration gate intact: the schema script and its checksum change together, version 1 fails the equality check, and the only new work is turning that failure into an explicit versioned diagnostic instead of a generic checksum mismatch. It also removes the migrate-keys path, the non-exportable-run branch in the exporter, and their tests.

### 4.4 Artifact graph

The package verifier reconstructs this required graph:

```text
Integration Contract
  -> Intent IR
  -> Provider Observations
  -> Admitted Legality Decision -> Physical Plan
  -> Signed Execution Graph
  -> Activation Summary
  -> Source Boundary -> Segment Manifest
  -> Commit Receipt -> Visibility Proof
  -> Continuous Evidence Event Chain
```

Every arrow is verified through a digest field on the child artifact or an attribute of an evidence event. Two arrows require the fields introduced by this addendum rather than existing ones: the admitted-decision-to-plan arrow uses the new `AdmittedPlan.physical_plan_digest` of §4.1, and the boundary-to-manifest arrow uses the existing `SegmentManifest.source_boundary_digest` against the version 2 boundary of §4.2. The verifier never authenticates a parent by recomputing a digest from an object it has not already verified.

A missing artifact, unknown kind, digest mismatch, broken parent link, discontinuous event sequence, invalid event digest, broken previous-event digest, or absent terminal visibility proof fails package generation and verification.

### 4.5 Multi-artifact transaction

The store exposes one primitive that applies, in a single SQLite transaction: a set of content-addressed artifact writes, an optional `run_private_state` write, and an optional checkpoint compare-and-set with its evidence event.

This primitive is required, not stylistic. `save_artifact` currently runs in autocommit and `advance_checkpoint_with_event` opens its own `BEGIN IMMEDIATE`, and SQLite raises on a nested `BEGIN` — so the atomicity §4.1 and §4.3 depend on cannot be composed by a caller out of the methods that exist. The existing single-purpose methods are re-expressed in terms of this primitive so there is one transaction boundary rather than two conventions.

The "after safe manifest/private-state persistence" fault point in §8.3 is meaningful only once this boundary exists; injected failures target the transaction, and the assertion is that the store contains either all of its rows or none.

## 5. Evidence Package

### 5.1 Layout

The exporter writes to a temporary sibling directory and atomically renames it to `output/m0/<run-id>/` only after verification succeeds:

```text
package.json
artifacts/<kind>/<digest>.json
trace/events.json
verification/result.json
operations/resources.json
operations/limitations.json
```

The package carries no gate dispositions. An earlier layout placed `operations/gates.json` inside the package while also hashing it into `package.json`, which cannot be built: the package-verification gate is decided by verifying the package, so its disposition cannot also be one of the inputs that verification covers. Gate dispositions live only in the committed report of §9, which references the package by digest and is therefore free to record the outcome of verifying it.

`package.json` contains the format version, run identifier, commit SHA, `uv.lock` digest, Python and MCP protocol versions, operator pseudonym, host pseudonym, transport decision, artifact inventory, artifact-parent edges, trace head digest, and SHA-256 of every payload file except `package.json` and `verification/result.json`. This avoids a self-referential digest. `verification/result.json` records the digest of `package.json` after verifying the listed payloads. Neither file contains a local absolute path, account identifier, connection string, credential, raw acceptance key, or source row value.

`operations/resources.json` records only opaque resource digests, creation state, retention deadline, cleanup status, and the exact approved cleanup operation category. It never records credentials or qualified provider identifiers.

### 5.2 Export allowlist

Only these artifact kinds may be exported:

- `integration_contract`;
- `provider_observation`;
- `intent_ir`;
- `physical_plan`;
- `legality_decision`;
- `signed_execution_graph`;
- `activation_summary`;
- `source_boundary` version 2;
- `segment_manifest` version 2;
- `commit_receipt`; and
- `visibility_proof` version 2.

Unknown kinds and older privacy-incompatible schemas fail closed. The retired `compilation_result` kind of §4.1 is not on this list and never becomes exportable. The private-state table, raw segments, SQLite database, environment, provider payloads, and logs are never copied.

The exporter is per-run and indexes artifacts reachable from one run identifier. The `No Valid Plan` path of §6.2 step 9 creates no run, so it produces no package; its rejected `legality_decision` is persisted and inspectable in the store, and the gate is evidenced by the store contents and the absence of a run, not by an exported package. `legality_decision` remains on the allowlist because the admitted decision of a successful run is exported.

### 5.3 Sensitive-value scanning

The acceptance process supplies credential canaries and synthetic row-value canaries through inherited environment variables or standard input, never command-line arguments or output. Scanning covers:

- tracked and untracked repository files, excluding Git object storage and approved virtual-environment caches;
- structured application logs captured for the run;
- CLI/MCP output;
- exportable artifact bytes;
- SQLite evidence-event and artifact payloads;
- and the complete temporary package.

The scanner checks exact canaries and common encoded forms, raw acceptance-key representations, the synthetic customer reference, connection-string schemes, private-key markers, local absolute path prefixes, and file URIs. It reports only rule identifiers, file-relative locations, and counts. It never echoes the matched value. Any finding fails export.

The acceptance-key rule is the reason §4.2 digests the source-boundary key bounds. A scanner rule that fires on the raw key is a last line of defence against an artifact design mistake; it is not a substitute for one, and an artifact that predictably trips it is a design defect rather than a scan failure to be waived.

### 5.4 Verification result

`verification/result.json` is generated only after all structural, digest, signature, chain, linkage, allowlist, and scan checks pass. It records the verifier version, the named checks and their outcomes, and the digest of `package.json` — nothing derived from provider data, credentials, or run values.

Because it is written after the scan of §5.3 completes, it is the one package file the scanner does not cover. That is acceptable only because its content is constructed from a fixed vocabulary of verifier-owned strings: check identifiers, the verifier version, and hex digests. The exporter asserts this directly, rejecting any result document whose values fall outside that vocabulary, and the scan is then re-run over the renamed package as a final confirmation before the run is reported. A finding at that point fails the run and removes the package.

Re-running verification on the completed package must be deterministic except for the external command timestamp, which is not included in package digests.

## 6. Witnessed Acceptance Harness

### 6.1 Placement and invocation

The harness lives under `tests/acceptance/` because it is M0 verification infrastructure rather than a production service. It invokes the installed `pillarmesh-m0` CLI in subprocesses with secrets inherited through the environment. The raw acceptance key is delivered to activation through standard input; it never appears in process arguments, JSON output, or the contract identifier. Contract and fixture labels use opaque random identifiers generated independently from the acceptance key. The checked-in transport decision records use of the approved CLI fallback and why desktop-host automation is not part of the deterministic gate.

One command performs one operator run. It requires a unique operator pseudonym and distinct `PILLARMESH_STATE_PATH` and `PILLARMESH_OUTPUT_DIR`. It refuses paths inside the repository, an existing run state, a non-empty output directory, missing cleanup authorization metadata, or a source or destination object outside the declared dedicated acceptance schema.

The last refusal is about the boundary of the acceptance environment, not about the two operators. Operators share the dedicated tables by design (§6.3); what the harness refuses is a provider object belonging to any schema other than the declared acceptance one, so that no run can read or mutate an object the environment owner has not designated as disposable.

### 6.2 Transaction sequence

The harness performs and records these steps in order:

1. Validate the exact environment-variable inventory without printing values.
2. Connect read-only with the runtime roles and execute positive access probes and expected-denial probes.
3. Generate a new acceptance key in memory and query Snowflake under a fresh statement to prove absence.
4. Insert one synthetic PostgreSQL row using only the fixture credential, after step 3's absence proof and before the contract of step 5 is created.
5. Create the exact fixed-shape contract, verify it, and activate it through CLI subprocesses.
6. Require terminal success and independently query Snowflake under a new connection and statement.
7. Match the independently computed destination value digest to the manifest and visibility-proof linkage.
8. Repeat the exact activation and require the original run identifier, a replayed receipt or already-terminal run, unchanged ledger cardinality, and no second target effect.
9. Verify a contract with one unsupported precondition and require `No Valid Plan`, no run record, no source read, unchanged ledger cardinality, and unchanged target state.
10. Export and independently verify the package and run sensitive-value scans.
11. Write a private local cleanup ledger with the exact provider identifiers required for cleanup, retention deadlines, and cleanup state. This ledger is stored outside the repository and evidence package. Export only the corresponding opaque resource digests and dispositions to `operations/resources.json`.
12. Write the fourteen gate dispositions to the committed report of §9, referencing the package by digest.

Steps 3 and 4 carry the whole of gate 1 and their order is load-bearing: the key must be proven absent in Snowflake before the row that carries it exists anywhere. No run record exists at step 4 — the first run is created by the activation in step 5 — so the harness records its own start marker, run identifiers, and step timestamps in the private local ledger, and the gate report cites that ledger for ordering.

The harness does not catch an exception and continue to a success report. A failed assertion produces a failed gate and preserves only the minimum local resource ledger needed for authorized cleanup.

#### Proving no source read in step 9

"Unchanged source-read counter" is not a usable assertion. PostgreSQL's `pg_stat_*` counters are cumulative, updated asynchronously, and advanced by autovacuum and by any other session touching the table, so an equality check on them makes gate 9 flaky in the one direction that matters — an intermittent failure on a correct run.

Step 9 instead asserts, in order:

1. that the CLI returned `No Valid Plan` with the expected unsatisfied precondition and `execution_occurred` false;
2. that no run record, no evidence event, and no `run_private_state` row were created in the operator's store;
3. that no local segment file exists under the operator's output directory; and
4. that the source runtime role opened no backend during the step, observed by sampling `pg_stat_activity` for that role's `backend_start` before and after, which is a per-session fact rather than a shared cumulative statistic.

Assertions 2 and 3 are the load-bearing ones: reading source rows in M0 requires opening a snapshot, encoding a segment, and advancing a checkpoint, none of which can occur without leaving durable local evidence the harness owns exclusively. Assertion 4 is corroborating; if the environment cannot grant visibility of `pg_stat_activity` for that role, it is recorded as unavailable rather than silently skipped, and the gate rests on 1 through 3.

### 6.3 Two-operator rule

Operator 1 and Operator 2 use distinct PostgreSQL runtime roles, distinct Snowflake users, distinct signing key identifiers, distinct local SQLite files, and distinct output directories. They may use the same dedicated tables and the same shape of runtime role grants, but each run uses a new acceptance key and batch identity. Sharing the dedicated tables is what the §6.1 refusal permits; it refuses objects outside the dedicated acceptance schema, not objects the other operator also uses.

Operator 2 starts from a clean checkout and follows only committed instructions. The gate report records whether any undocumented step was needed. The second operator must be a person or independently accountable reviewer who did not author the runbook; a second process or subagent controlled by the same author does not satisfy gate 14.

### 6.4 Resource retention and cleanup

Every created resource is registered before or immediately after creation in the private cleanup ledger. Cleanup operates only on those exact identifiers and requires explicit owner authorization. The sanitized package receives only their opaque digests and dispositions.

- Failed pre-commit fixture rows and local segments are eligible for immediate cleanup.
- Successful evidence packages and synthetic source/target rows are retained for 30 days.
- Successful staged files are removed within 24 hours using the exact batch prefix.
- Failed or indeterminate staged files are quarantined for at most seven days.
- Cleanup confirms absence through fresh provider queries and updates the local resource ledger.

Tests use `try/finally` to record cleanup requirements even when interrupted. They do not drop shared schemas, tables, roles, stages, warehouses, or databases. Full-environment teardown remains a separate owner-authorized procedure.

## 7. Configuration Contract

`.env.example` and `docs/m0/setup.md` list every required variable name, grouped as:

- runtime PostgreSQL;
- fixture-only PostgreSQL;
- runtime Snowflake;
- signing;
- private local state/output;
- evidence scan canaries; and
- acceptance operator metadata.

The runbook uses explicit placeholders for the PostgreSQL database identifier rather than the literal `current_database_name`. It supplies parameterized positive and denial probes, a fixed contract example, pre/post visibility queries, replay checks, stage cleanup, row-retention cleanup, and separate Operator 1 and Operator 2 examples. Examples never put passwords, DSNs, private keys, or row values in command arguments.

Configuration validation reports missing variable names together and never includes values. The harness rejects credentials that resolve to the owner or fixture principal where a runtime principal is required.

## 8. Test and Fault Matrix

### 8.1 Artifact and package tests

- Compiler bundle parent digests match canonical contained objects, including `AdmittedPlan.physical_plan_digest` against its embedded plan.
- All compiler artifacts persist before activation-summary publication.
- Save failure leaves no published summary and no partially written bundle, asserted by injecting the failure inside the §4.5 transaction and reading the store back.
- Segment, visibility, and source-boundary version 2 canonical bytes contain no path, raw key, or raw key bound.
- An exhaustive field-level assertion over every exportable artifact type fails if any field can carry a raw path, key, qualified provider identifier, or source row value. This enumeration, not the per-type tests above, is what proves the privacy property; it is updated whenever an exportable model gains a field.
- No API reachable from the CLI, MCP server, or `load_artifact` returns the acceptance key, including the run record returned by `activate` and `get_run`.
- Runtime resumes using private state while exported evidence remains safe.
- Opening a version 1 database fails with a diagnostic naming both versions and creates no tables.
- Package export and verification are byte-reproducible from the same store.
- Missing, swapped, modified, unknown, or orphaned artifacts fail verification.
- Broken event order, previous digest, event digest, or terminal linkage fails verification.
- Exact, encoded, path, file-URI, connection-string, private-key, and row-value canaries fail scanning without appearing in diagnostics.
- A verification result containing a value outside the fixed verifier vocabulary of §5.4 is rejected by the exporter.

### 8.2 Runtime-before-provider tests

Separate call-counter tests prove that modified, expired, wrongly signed, and incompatibly versioned graphs fail before source or destination resolution. Activation-expiry and activation-drift tests continue to prove that no run is created.

### 8.3 Offline fault injection

Injected process-stop or write-failure points cover:

- before extraction;
- after safe manifest/private-state persistence;
- during destination commit;
- after commit but before receipt persistence;
- after receipt but before visibility verification;
- before and after each evidence/checkpoint transaction; and
- restart from every resumable checkpoint.

Additional faults cover corrupt staged bytes, modified contract, graph, and provider observation artifacts, a conflicting ledger digest, and lost commit response. Each case asserts a deterministic resumable or terminal state, continuous evidence chain, stable batch identity, and no false success.

### 8.4 Live provider faults

Using only the dedicated environment, the witnessed campaign revokes and restores PostgreSQL read, Snowflake stage write, and Snowflake merge permissions one at a time. Each denial is verified before the fault run, and restoration is verified afterward. These operations require the environment owner and are recorded separately from runtime credentials.

Because §6.3 lets both operators hold the same shape of runtime grants, a revocation aimed at one operator can break the other's run in a way that reads as a product defect. The campaign therefore runs in an exclusive window: it is the only acceptance activity against the environment for its duration, it revokes only the grants of the operator role it is testing, and it verifies restoration before the window closes. The gate report records the window and the roles touched. If an exclusive window cannot be arranged, the campaign uses a third role provisioned for fault injection alone and neither operator's grants are modified.

### 8.5 Legality mutation and review

Mutation acceptance is requirement-based rather than a raw percentage. For each of the ten numbered preconditions, the report identifies at least one mutation that bypasses or negates the precondition and the test that kills it. Surviving mutations are classified; any survivor capable of changing admission, evidence requirements, handle binding, or diagnostic attribution blocks review. Equivalent or cosmetic survivors may remain only with a written reviewer ruling.

An independent reviewer verifies the fixed PostgreSQL-to-Snowflake type correspondence, handle binding, freshness boundaries, destination-ledger atomicity assumption, positive and negative fixtures, mutation mapping, and post-admission evidence-set invariant. The reviewer records identity, date, commit SHA, disposition, and limitations in the proof note without adding credentials or account identifiers.

## 9. Gate Report

The committed report under `docs/m0/reviews/` is created only after a witnessed run. It contains:

- commit and lock digests;
- sanitized operator and host pseudonyms;
- CLI-fallback decision;
- package and trace-head digests;
- source and destination schema digests;
- measured timings and resource counts;
- independent legality-review reference;
- second-operator result;
- live-fault window and roles touched;
- limitations; and
- all fourteen gates marked `passed` or `failed` with a direct evidence reference.

The report is the only place gate dispositions are recorded (§2 decision 7). It cites the package by digest, so it can state the outcome of verifying the package without being part of it.

`partial`, `assumed`, and `not run` are not passing states. A failed or unverified required gate prevents the M0 completion claim but does not hide the evidence collected for other gates.

## 10. Failure Semantics

- A missing compiler parent artifact prevents activation-summary publication.
- A private-state write failure prevents extraction checkpoint advancement, because both are rows of one §4.5 transaction.
- Opening a version 1 evidence store fails the command outright; it is never upgraded, partially upgraded, or opened read-only.
- A package verification or scan failure leaves no completed package and records no passing package gate.
- An ambiguous destination commit is resolved only through batch identity and manifest digest; it is never blindly retried as a new batch.
- Cleanup failure records the exact opaque resource, due date, and failure classification and prevents a cleanup-complete claim.
- A live harness interruption never converts retained resources or an indeterminate commit into success.

## 11. Delivery Sequence

Implementation proceeds in dependency order:

1. Add the §4.5 multi-artifact transaction to the store and re-express the existing write methods on top of it.
2. Introduce safe version 2 provider artifacts, the provider protocol changes of §4.2.1, runtime-private state, and the schema version 2 refusal of version 1, with recovery tests.
3. Return and atomically persist the compiler artifact bundle with parent-link tests, retiring `compilation_result`.
4. Implement package export, verification, and fail-closed scanning.
5. Complete runtime-before-provider and offline fault-injection coverage.
6. Make setup, acceptance, retention, and cleanup procedures executable.
7. Implement the single acceptance harness and diagnostic live-test cleanup.
8. Complete legality mutation mapping and independent review.
9. Run all offline gates from a clean checkout.
10. Inject the two dedicated credential sets and run the witnessed two-operator acceptance.
11. Commit only the sanitized gate report after package verification and scans pass.

No live mutation occurs before steps 1 through 9 pass.

## 12. Acceptance Criteria for This Addendum

The acceptance-readiness change is complete when:

- every graph parent digest resolves to exact persisted canonical bytes through a digest field on the child;
- the field-level enumeration of every exportable artifact type shows no field able to carry a raw local path, raw acceptance key, raw source key bound, or source row value;
- private state supports restart without entering evidence, authoring output, or any run record returned by the CLI or MCP server;
- package export and independent verification fail closed on every tested integrity or privacy defect;
- the complete offline fault matrix produces no false success or unexplained destination divergence;
- the legality proof has an independent disposition and requirement-mapped mutation evidence;
- a clean checkout can perform setup and dry-run validation using only committed instructions;
- both live operators complete the bounded transaction with distinct credentials and local state;
- the same activation and batch produce no duplicate effect;
- the negative contract produces `No Valid Plan` with no run, no local extraction evidence, and no destination mutation, evidenced as described in §6.2;
- retained and cleanup-due resources are precisely inventoried and verified; and
- the committed report contains direct evidence for all fourteen M0 gates and no sensitive values.

Until all criteria pass, the implementation may be described as an offline M0 thin thread or acceptance-readiness work, but not as a completed M0.
