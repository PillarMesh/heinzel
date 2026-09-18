# ADR-0004: Compile to a Restricted SQL Subset, Not a DSL and Not Spark

- Status: Proposed
- Date: 2026-08-27
- Governing design: `docs/superpowers/specs/2026-08-27-processing-model-design.md`

## Context

Addendum v0.1 §12.1 makes Heinzel the authority that generates, tests, versions, and deploys transformation models, and says the MVP exports a dbt-compatible project representation. It does not state what language the compiler emits or what semantic target the compiler reasons against. That gap allows three incompatible readings — a Heinzel transformation DSL, general SQL, or a distributed engine plan — and each implies a different multi-year commitment.

Three concerns are routinely conflated. The authoring surface and the internal representation are already settled by EDC v0.3: the human declares an Integration Contract and the compiler owns the semantic IIR. Only the emitted artifact is open.

Two constraints bear on the choice. EDC v0.3's thesis requires proving that a physical plan produces only histories the contract allows, which is undecidable for arbitrary SQL — three-valued logic, implicit casts, collation-dependent comparison, non-deterministic functions, and window-frame semantics all defeat it. Separately, Addendum §6.1 offers both PostgreSQL and ClickHouse, whose dialects diverge silently on null handling, precision, collation, locale, and timezone.

Because Heinzel generates transformations, no human writes transformation code in the normal path. The ergonomic case for a DSL therefore has almost no user, while Addendum §12.1's requirement that a human approve generated output where meaning changes argues directly for an artifact the data engineer already reads.

## Decision

The compiler lowers the semantic IIR to a **restricted SQL subset**, emitted per engine, executed by the warehouse, and run and observed through the dbt adapter.

Heinzel does not build a user-facing transformation language.

The emittable constructs form a declared, versioned allowlist. Each construct has semantics pinned separately for PostgreSQL and ClickHouse across the D1-D8 equivalence dimensions. The allowlist is governed exactly as the legality rule table is: curated, sound but deliberately incomplete, and expanded only with a reviewed proof note, positive and negative per-engine conformance fixtures, and an independent reviewer. A construct outside the allowlist produces `No Valid Plan` with a counterfactual naming the missing construct.

The compiler never parses SQL it did not generate. Analyzability comes from constraining emission, not from analyzing an arbitrary input language.

Heinzel does not compile to Spark. The decisive reason is that warehouse SQL semantics are unavoidable regardless — Superset queries the warehouse (§16.1), the integrity layers live there (§12.2), and continuous constraints are evaluated there (§12.3) — so a Spark target would add a third semantic target rather than retire one. Compiling to Spark also moves resident data out over JDBC and back, does not improve the legality proof because Catalyst's rewrites determine what runs, and produces artifacts that are harder to review than SQL.

## Consequences

- The compiler proves properties only about constructs it emits, which is materially cheaper than analyzing a general language and is the reason this approach is affordable.
- Generated output is SQL, so the human approval required where meaning changes is performed in a language the data engineer already reads, and the existing dbt ecosystem runs and observes it.
- Two emitters must be maintained, and each construct requires per-engine conformance fixtures proving identical observable results. This is the main recurring cost of the decision and is the work a single-engine or Spark target would have avoided.
- The subset will be too restrictive early. Legitimate transformations will return `No Valid Plan` until the allowlist is widened. "Constructs requested but not admitted" is tracked as a product metric; a queue growing faster than it is cleared is evidence the subset is wrong.
- No transformation language has to be designed, documented, versioned, taught, or migrated, and no hiring pool is narrowed.
- Owning the IIR keeps a Spark or other emitter available later as a purely additive act, with no contract change and no semantic migration. That optionality remains only while unspent.
- If the subset proves insufficient after several expansion cycles on real tenant workloads, the fallback is to admit general SQL for a declared non-provable model class, marked in evidence as unproven and barred from autonomous plan substitution. The claim degrades honestly rather than a language being built.

## Reversal triggers

- A tenant's sources make extraction the binding constraint at hundreds of millions of rows per generation while the warehouse working set stays small, measured on real volumes. This triggers a Spark **EXTRACT** emitter, delivered as Spark Connect, not a Spark transformation target.
- The per-engine fixture burden proves unsustainable, at which point the decision to offer two warehouse engines is revisited before the compilation target is.

## Amendment 2026-09-11: governed answer query class

Addendum v0.1 §12.4 adds a second, read-only class to the same allowlist: governed queries that answer stakeholder questions. The compiler lowers a validated answer intent into a statement that may use only:

- approved consumption objects;
- metric-pinned aggregates;
- approved dimensions;
- closed-domain filters;
- bounded time windows;
- compiled small-group suppression; and
- ordering and a row limit.

Query constructs are governed exactly as transformation constructs are: per-engine semantics pinned across D1-D8, a reviewed proof note, positive and negative per-engine fixtures, and an independent reviewer. A construct outside the allowlist produces `No Valid Plan` naming it.

Governed queries are not dbt models. The runtime executes them directly through the `answer_runtime` principal and records an execution receipt. The compiler still never parses SQL it did not generate, and no AI-authored or question-derived text reaches a statement.

## Implementation dependency record 2026-09-15

`services/dbt-adapter` pins `dbt-clickhouse==1.10.2` alongside `dbt-core==1.10.13` and
`dbt-postgres==1.10.2`. The ClickHouse adapter is required because the existing subprocess boundary
selects a provider-specific dbt target and must load that target without adding provider behavior to
the compiler or runtime. Release 1.10.2 is maintained by ClickHouse, declares support for dbt Core
1.10 and ClickHouse 25.8, and is licensed under Apache-2.0. The lockfile also pins its transitive
drivers. A pinned-engine live test executes a compiler-signed table model before this dependency is
accepted as evidence for ClickHouse materialization.

## Amendment 2026-09-15: per-engine activation of the product SQL rule

The emittable-construct allowlist already pins semantics separately for PostgreSQL and
ClickHouse. This amendment records that an allowlist entry is therefore **activated per engine**,
and that activating it for one engine makes no claim about any other.

The product SQL rule `PRODUCT-SQL-V2-PROJECT-SUM-001` is to be activated for `postgresql` only, on
independent approval. Nothing is activated yet. That activation:

- rests on PostgreSQL evidence alone: the D1-D8 proof and fixtures, the mutation regression, the
  provider-owned observation, and the live checked-SUM run on the pinned engine;
- **does not claim cross-engine result equivalence.** The original wording of its precondition 17
  asserted equivalence "on both pinned engines"; under single-engine activation that precondition
  is reworded to single-engine admission that explicitly withholds the equivalence claim. An
  approval of the original wording does not authorize this activation;
- **does not admit ClickHouse.** Compiler admission checks must be keyed on the requested engine
  and on a per-engine activation record. A ClickHouse compilation must continue to produce
  `No Valid Plan` while no ClickHouse activation exists, pinned by a negative test. The activation
  record and its admission checks are built when the compiler gains a success path, after
  independent review approves this scope; until then every compilation on either engine produces
  `No Valid Plan`, and the precondition 17 wording is already keyed on the engine.

ClickHouse activation of the same rule requires its own runtime magnitude authority, its own
D1-D8 fixtures, a live cross-engine equivalence run, and a second independent review.

Restrictions that exist because the engines diverge remain in force under single-engine
activation. Excess-scale decimal input stays inadmissible although PostgreSQL alone rounds
predictably, and numeric JSON input for a declared string field stays inadmissible although
PostgreSQL alone coerces it predictably. Widening an activated rule later is ordinary governance;
narrowing one after activation invalidates evidence already accepted for it.

Consequence for the product: the addendum defines the MVP as a two-engine portability proof in
its scope rule, exit criteria, and success measure. A PostgreSQL-only product engine does not pass
that definition and is not called the MVP. It is Gate A, and the addendum MVP remains unpassed
until ClickHouse activation completes.
