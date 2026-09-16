# PillarMesh Processing Model Design

- Status: Proposed for review
- Date: 2026-08-27
- Governing specifications: EDC Foundational Architecture v0.3, Revenue-to-Cash MVP Implementation Plan v1.4, Managed Data Engineering Platform Addendum v0.1
- Related decisions: ADR-0004 (compilation target), ADR-0005 (data acquisition and load)
- Question: what data processing framework does PillarMesh need so that one data engineer can run a real data estate?
- Reconciled against `main` before merge: §7.5's account of the staged segment format now covers the canonical-JSONL acquisition encoder that shipped with Plan 4A after this document was drafted. No decision changed.

## 1. Answer

PillarMesh should own **no distributed compute engine** and **no user-facing transformation language**. It should own a *processing model*: three narrow execution modes, immutable generations, publish-on-verify, and a compiler that lowers its existing IIR to a restricted SQL subset the warehouse executes.

Apache Spark, Flink, Trino, Hive, and Iceberg are evaluated below. None is adopted for the MVP. Each receives an explicit adoption trigger (§10) so a future adoption is evidence-driven rather than a default.

This is consistent with Addendum v0.1 §3.4, which already excludes a new database engine, and §12.1, which makes transformations compiler-generated artifacts. This document supplies the processing model those sections assume but do not specify.

The reasoning is in §4: for a one-person team the engine is not the bottleneck, and choosing one of these five would consume the person it was meant to free.

## 2. The five systems are not peers

They are routinely compared as alternatives. They are not; they occupy four different layers, and conflating them is the first mistake a small team makes.

| System | Layer | What it actually is | Operational surface |
| --- | --- | --- | --- |
| Hive | Fused catalog + table layout + engine | Directory-as-table, a metastore, and SQL over MapReduce/Tez | Metastore, HDFS-era assumptions, cluster |
| Iceberg | Table format only | Snapshot metadata, manifests, schema and partition evolution. No compute at all. | Catalog service, file maintenance, compaction |
| Spark | Batch and micro-batch compute | DAG scheduler and DataFrame API over a cluster | Cluster, executors, shuffle, skew, memory tuning |
| Flink | Streaming dataflow | Event time, keyed state, checkpointing, two-phase-commit sinks | Cluster, state backend, savepoints, rescaling, watermarks |
| Trino | Interactive MPP query | Federating SQL across sources. No storage, no durable state. | Coordinator and workers, memory limits, connector configs |

Two of these are formats and catalogs. Three are engines. Only Iceberg and Hive persist anything.

## 3. What each one teaches, and what it costs

**Hive — the cautionary tale.** Hive welded the table format, the catalog, and the execution engine into one thing. Changing the engine changed table semantics, so "the same table" meant different things depending on what read it. This is precisely the semantic-and-physical coupling EDC v0.3 §3.1 identifies as the root failure of pipeline-shaped systems. Hive is not a candidate. Its value here is as evidence for the thesis.

**Iceberg — the architectural ancestor.** Iceberg's contribution is separating the table format from the engine, so a table's meaning survives a change of compute. That is the same move PillarMesh makes one layer up, separating the Integration Contract from the physical plan. Iceberg is therefore the system this design has the most in common with, and the one whose *pattern* is adopted (§8) without adopting the dependency.

**Spark — the right API, the wrong operational bill.** The DataFrame model is excellent and Spark is the correct answer for genuinely large batch work. Its cost is the cluster: executor sizing, shuffle behaviour, partition skew, driver memory, and version-dependent failure modes. A one-person team that adopts Spark acquires a second full-time job.

**Flink — enormous power, catastrophic small-team cost.** Flink is the only system here that treats event time, keyed state, and exactly-once sinks as first-class. If a contract genuinely needs sub-minute freshness over keyed state, nothing else is close. The cost is state: backend selection, checkpoint tuning, savepoint compatibility across upgrades, rescaling, and watermark debugging. This is the least appropriate system for one operator and the most likely to be chosen for prestige reasons.

**Trino — the cheapest engine to operate, but not a pipeline engine.** Statelessness is the feature: nothing to checkpoint, nothing to restore. It federates well and answers interactive questions well. It has no durability, no checkpoints, and no incremental state, so it cannot own a pipeline. It is the most plausible *first* adoption of the five, for federated read.

## 4. Why the engine choice is not the lever

The stated aim is one data engineer running a real estate. The question is therefore not "which engine is fastest" but "what consumes the engineer's week."

In practice, a small data team's time goes to:

1. connector and upstream schema drift;
2. diagnosing failures, usually at an inconvenient hour;
3. backfills and reprocessing;
4. answering "is this number right?";
5. onboarding a new source; and
6. unexplained cost increases.

Spark, Flink, Trino, Hive, and Iceberg address item 3, partially. They address none of items 1, 2, 4, 5, or 6. Several make items 2 and 6 materially worse by adding a cluster whose failures are unrelated to the data.

The lever for a one-person team is the human loop: how often a person is required, and how long each involvement takes. That is what this processing model optimizes, and it is why the engine question resolves to "own none."

## 5. Execution modes

Exactly three modes exist. A contract compiles to a sequence of them and nothing else.

**EXTRACT.** Bounded, ordered, streaming read from a source through a Capability Provider, in the PillarMesh runtime process. Rows are streamed to an immutable staged segment; the dataset is never held in memory. Row, byte, segment, and wall-clock ceilings are compiled into the graph and enforced before commit, as in the M0 thin thread. Acquisition strategy is in §7.

**LAND.** Idempotent write of one staged segment into the raw evidence layer as a new immutable generation, keyed by batch identity and recorded in a commit ledger. Replay of the same batch identity is a no-op that returns the original receipt. This is the mechanism proven by the M0 design, and it is never delegated to a third party.

**TRANSFORM.** Set-based SQL executed *by the warehouse*, from a model the compiler generated, version-pinned, and tested. PillarMesh never moves rows through its own process for transformation. The warehouse engine — PostgreSQL or ClickHouse per Addendum v0.1 §6.1 — is the compute.

There is no fourth mode. No PillarMesh-owned join engine, shuffle, aggregation runtime, streaming operator, or user-supplied code path. A workload that cannot be expressed in these three modes is not "hard"; it is a trigger under §10.

A governed answer query (Addendum v0.1 §12.4, amended 2026-09-11) is not a mode either. It is a read-only statement the warehouse executes, as a Superset query is, with aggregation and small-group suppression compiled into the SQL. The runtime streams the bounded, already-aggregated result to a result snapshot and never aggregates or joins rows itself. The query writes to no warehouse layer, and its result is never an input to a mode.

## 6. Compilation target

Three decisions are routinely conflated and are separated here:

| Concern | Decision | Owner |
| --- | --- | --- |
| Authoring surface | Integration Contract; no transformation language | EDC v0.3 |
| Internal representation | Semantic IIR | EDC v0.3 |
| Emitted artifact | **Restricted SQL subset, per-engine lowering** | This document, ADR-0004 |

Because PillarMesh *generates* transformations (Addendum §12.1), no human writes transformation code in the normal path. The ergonomic argument for a friendly DSL therefore has almost no user, while the reviewability argument — a human must approve generated output where meaning changes — argues directly for SQL, which the data engineer already reads.

### 6.1 Why not a user-facing DSL

A DSL means a parser, type system, semantics, error messages, documentation, tooling, versioning, and migration: a multi-year effort delivering a language nobody knows, so the one engineer cannot hire help or search an error. The historical record is consistent — Pig Latin, Cascading, Scalding, Crunch, and Summingbird either died or converged on SQL, and Spark itself moved from RDDs to DataFrames and SQL.

### 6.2 Why not general SQL

Arbitrary SQL is not analyzable for the properties the compiler must prove. Three-valued logic, implicit casts, collation-dependent comparison, non-deterministic functions, correlated-subquery edge cases, and window-frame semantics make history containment undecidable in the general case. Dialect divergence between PostgreSQL and ClickHouse is silent and severe.

### 6.3 The restricted subset

The compiler emits only constructs on a declared, versioned allowlist whose semantics are pinned **per engine**, covering exactly the D1-D8 dimensions where dialects differ silently: null and missing, precision, collation, locale, and timezone.

The asymmetry that makes this affordable: **generating a constrained subset is far cheaper than parsing and analyzing a general language.** PillarMesh never has to understand arbitrary SQL, only the SQL it produced. This removes the hardest part of the DSL argument while keeping its analyzability benefit.

The construct allowlist is governed exactly like the legality rule table: curated, versioned, sound but deliberately incomplete, expanded only with a proof note, per-engine conformance fixtures, and an independent reviewer. One mechanism, two uses. A construct outside the allowlist produces `No Valid Plan` with a counterfactual naming the missing construct.

### 6.4 Why not compile to Spark

Compiling the IIR to a Spark logical plan is a better idea than adopting Spark as an engine, and its strongest argument is real: one semantic target instead of two, with ANSI mode giving stricter and better-documented coercion rules than either warehouse.

It does not hold, for one decisive reason. **Warehouse SQL semantics are unavoidable regardless.** Superset queries the warehouse directly (§16.1), the four integrity layers live there (§12.2), and continuous constraints are evaluated there (§12.3). Compiling transforms to Spark therefore adds a third semantic target rather than retiring one, and the best argument for it evaporates.

Three further objections: data already resident in the warehouse would move out over JDBC and back, discarding indexes, sort orders, and statistics; the legality proof does not improve, because Catalyst's rewrites determine what actually runs; and generated Spark plans are a harder human review than generated SQL.

Split by mode, Spark helps EXTRACT above a volume threshold, does nothing for LAND, and is strictly worse than pushdown for TRANSFORM. That is too narrow to justify a cluster in the mandatory stack.

The payoff of owning the IIR is that a Spark emitter can be added later as a purely additive act, with no contract change and no semantic migration. That optionality is free only while it is unspent.

## 7. Data acquisition

SQL answers TRANSFORM. EXTRACT and LAND are a different problem.

### 7.1 The constraint

EDC v0.3 §21 requires each capability to *declare* semantics that conformance tests verify: ordering scope, duplicates, deletion behaviour, replay, and idempotency. Off-the-shelf connectors do not supply this. Airbyte declares a sync mode but not whether deletes are observable, what a mid-sync retry does to the duplicate envelope, or what ordering it guarantees. Singer tap quality varies tap by tap.

Embedding third-party connectors therefore buys reach and forfeits the proof for those sources. Plan v1.4 §21.4's conformance tiers exist to express that trade honestly rather than to hide it.

### 7.2 LAND is never delegated

The write path into the warehouse — batch identity, commit ledger, immutable generations, publish-on-verify — is PillarMesh code permanently. It carries the correctness claims, it is engine-specific per Addendum §11.3, and no third party implements it the way the evidence model requires.

### 7.3 EXTRACT is tiered

| Tier | Mechanism | Planner eligibility |
| --- | --- | --- |
| **Certified** | Own provider on the native protocol or API, full conformance suite | Any risk tier; eligible for legality substitution |
| **Provisional** | Embedded `dlt` source, or an imported Airbyte low-code manifest lowered to a PillarMesh declaration | Snapshot and cursor acquisition only; maximum T1; no substitution; untested semantics remain Unknown |
| **Opaque-wrapped** | Third-party connector observed only by measurement | Only contracts that explicitly tolerate the resulting Unknown |

**MVP sources stay Certified.** PostgreSQL via psycopg3 and Stripe via its REST API, per Addendum §11.2. Two sources do not justify a framework, Stripe's API is well-behaved, and these are the sources the revenue-to-cash reconciliation invariants depend on.

**The long tail is `dlt`, embedded.** It is a Python library that runs inside the existing single-process runtime with no container runtime, no cluster, and no second deployment artifact. It supplies schema inference, evolution, and incremental state; it does not supply delete or ordering guarantees, which is precisely why it lands at Provisional.

**Airbyte low-code YAML manifests are the most promising import path.** A manifest declares auth, pagination, record selection, primary key, and cursor field — structurally closer to a capability declaration than connector code is. Importing and lowering a manifest reaches Provisional far more cheaply than building a provider, without claiming semantics the manifest does not state.

**CDC, when it arrives, is Debezium Server wrapped as a provider.** Replication slots, LSN handling, retention pressure, snapshot-to-stream handoff, and failover are a multi-year problem already solved and hardened. Debezium Server runs standalone without Kafka, which matters because plan v1.4 §9 deliberately declined Kafka.

**Fivetran-category SaaS is rejected as an acquisition path.** Vendor-asserted semantics that cannot be verified are incompatible with a product whose thesis is proof, and it places a third party inside the evidence chain.

### 7.4 Why tiering serves the one-person claim

Connector maintenance is the largest recurring time sink for a small data team, because upstream APIs change constantly. The tier decides who absorbs that churn: Certified means PillarMesh does, per source, forever; Provisional means the community does and PillarMesh absorbs only the semantic gap. A product promising one engineer cannot own fifty connectors. It can own five and wrap forty-five, provided it never misrepresents which is which.

### 7.5 Staged segment format

Two segment encodings already exist, and neither is chosen per destination. M0 fixes a deterministic CSV segment (`segment.csv` in `pillarmesh_provider_sdk.models` and `pillarmesh_runtime.runtime`), which is correct for a PostgreSQL `COPY` fast path and wrong for ClickHouse, which ingests Parquet and native formats far better. Plan 4A's acquisition path instead writes canonical JSONL through `CanonicalJsonlSegmentEncoder`, which is engine-neutral and therefore fast for neither. Since Addendum §11.3 already permits different physical mechanisms per destination, the segment format becomes a per-destination writer concern over a canonical in-memory representation, and the two existing encoders become two writers rather than two defaults. `CanonicalJsonlSegmentEncoder` already separates `content_digest` from `record_set_digest`; that separation is what makes this affordable, because the record-set digest is the engine-independent one. The manifest digest stays canonical and engine-independent so evidence remains comparable across warehouses.

## 8. Generations and publish-on-verify

This is the core of the design and the element that makes the operating model work.

Every processing step writes a **new immutable generation** rather than mutating a visible table. A generation is verified against the contract's constraints. Only after verification does an atomic pointer flip make it the version consumers read.

```text
  write            verify                    publish
staged  ──▶  generation N+1  ──▶  constraints, reconciliation,  ──▶  atomic
segment      (not visible)        freshness, row-count deltas        repoint
                                          │
                                          ├─ pass ─▶ consumers now read N+1
                                          └─ fail ─▶ N remains live; incident opens
```

Implementation is deliberately unremarkable: on PostgreSQL, generations are schemas or suffixed tables behind a view repointed in one transaction; on ClickHouse, partition-level `REPLACE`/`ATTACH`. No Iceberg dependency and no new storage engine, satisfying Addendum §3.4.

This is Iceberg's write-audit-publish pattern, which is the specific idea worth taking from the five systems.

What it buys, in the terms of §4:

- **Bad data never becomes visible.** Verification precedes publication, so the failure mode is a stale-but-correct dataset with an open incident, never a wrong-but-fresh one. This is the largest single reducer of "is this number right?" work.
- **Backfill and reprocessing become ordinary.** Write a new generation over the corrected range, verify, publish. A reprocessing generation has its own identity and evidence, and consumers never see a partial result.
- **Rollback is a repoint.** The previous generation still exists. Recovery is seconds and requires no restore.
- **Time travel comes free** by retaining N generations, bounded by the tenant retention profile.
- **Evidence is anchored.** Every generation identity appears in the evidence chain, so "which data did this dashboard read on Tuesday" is answerable exactly.

Generation retention is a declared contract field. Retention is the cost knob, and it is visible rather than emergent.

## 9. The diagnosis contract

For a one-person team, mean time to *understand* dominates mean time to fix. Every failure in any mode must produce, without investigation:

1. the contract and the generation that failed;
2. the last known-good generation and whether it is still published;
3. whether any consumer read affected data, and which;
4. the failure class, from the fixed taxonomy, not a stack trace;
5. the smallest action that changes the outcome; and
6. whether PillarMesh already attempted remediation and what happened.

If a failure cannot produce all six, that is a defect in the processing model, not an operator problem. This is the concrete form of Addendum §4.3's supervised autopilot: the inbox is only credible if every item arrives complete.

## 10. Adoption triggers

None of the five is adopted now. Each has a stated trigger, and adoption is as a Capability Provider under the existing conformance-tier model, never as a replacement for the processing model.

| System | Adopt when | Do not adopt because |
| --- | --- | --- |
| **Trino** | A contract requires federated read across the warehouse and an external system without copying, and query-in-place is legal for that contract. Likely the first of the five. | It looks like a faster warehouse. It is not a pipeline engine and cannot own durable state. |
| **Iceberg** | Raw-evidence retention exceeds what the warehouse should hold, or a second engine must read the same raw layer. Adopt for the **raw layer only**, never as the warehouse. | Table-format elegance. The generation model already provides snapshots and rollback at current scale. |
| **Spark** | A tenant's *sources* are large enough that extraction is the binding constraint — hundreds of millions of rows per generation — while the warehouse working set stays small. Measured on real tenant volumes. | Familiarity, or an anticipated future scale that has not been measured. |
| **Flink** | A contract's freshness objective is sub-minute *and* requires event-time windowing over keyed state. This is also the trigger for the event-driven `TriggerPolicy` modes deferred in Addendum §15. | Streaming is fashionable. Periodic triggers meet the great majority of stated freshness objectives. |
| **Hive** | Never. | — |

Each trigger requires measurement on real tenant data before adoption, and an ADR recording the measurement. "We will need it eventually" is not a trigger.

If Spark is ever adopted it arrives as **Spark Connect**: an unresolved logical plan sent over gRPC to a remote Spark, with no JVM and no cluster inside the PillarMesh process. That is the only shape in which Spark is compatible with the one-person promise, and it fits the existing provider and conformance-tier model unchanged.

## 11. Capacity envelope

The model is honest about where it stops. These are design expectations to be replaced by measurement at the first tenant.

| Dimension | Comfortable | Strained | Trigger fires |
| --- | --- | --- | --- |
| Rows per extract generation | to ~10M | 10-50M | >50M, or ceiling breaches become routine |
| Bytes per generation | to ~10 GB | 10-50 GB | >50 GB |
| Warehouse total | PostgreSQL to a few hundred GB; ClickHouse to low TBs | at profile ceiling | migration or §10 trigger |
| Freshness objective | hourly to daily | ~15 minutes | sub-minute with keyed state |
| Sources per tenant | to ~20 | 20-50 | >50 |

The one-person claim is made *within this envelope* and is not made outside it. A tenant beyond it needs either a different capacity profile or a §10 adoption, and either is a stated, costed decision.

## 12. Consistency with Addendum v0.1

This document adds and does not supersede. It supplies:

- the three execution modes assumed by §12.1's transformation authority;
- the compilation target §12.1 implies but does not state;
- the acquisition tiering that makes §11.1's connector-lifecycle ownership affordable beyond the two MVP sources in §11.2;
- the generation and publish-on-verify mechanism that makes §12.2's four integrity layers operable and §12.3's continuous constraints enforceable before publication rather than after;
- the reprocessing and rollback semantics that §15's bounded-backfill trigger mode requires; and
- explicit adoption triggers so that §3.4's "no new database engine" holds by argument rather than by omission.

It changes nothing about the warehouse choice, catalog, BI boundary, request workspace, or approval tiers. It refines §11.3 only in making the staged segment format a per-destination concern (§7.5).

## 13. Open questions

1. Generation retention defaults per integrity layer, and whether raw evidence and data products should differ.
2. Whether the ClickHouse partition-replace path can offer the same atomic-repoint guarantee as the PostgreSQL view path, or whether the two engines expose different publication semantics that a contract must declare.
3. Whether verification runs against the unpublished generation in the same warehouse or a separate compute path, and the cost implication at the capacity ceiling.
4. Whether a failed generation is retained for diagnosis by default and for how long, given that it contains real tenant data.
5. The initial SQL construct allowlist, and the expected rate of expansion requests during the first cohort.
6. Whether an imported Airbyte manifest can reach Provisional without a per-connector review, or whether each import requires its own conformance run.
