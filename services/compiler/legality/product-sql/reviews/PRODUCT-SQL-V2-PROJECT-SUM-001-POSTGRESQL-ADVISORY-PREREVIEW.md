# Advisory AI pre-review: PRODUCT-SQL-V2-PROJECT-SUM-001, PostgreSQL activation

- **Status: advisory. This is not the independent review and approves nothing.** It does not
  satisfy precondition 18 and must never be recorded as approval. AGENTS.md forbids an author or an
  AI from certifying independent review.
- Reviewer: an AI model of a different family from the authoring agent, given only the review
  packet and the repository, with no access to the authors' reasoning, and instructed to verify
  rather than trust.
- Revision reviewed: `31c6581dc647e0a0175723c46c21ec81072ce813`
- Recommendation at that revision: **not ready**.
- Every finding below was independently checked against the pinned PostgreSQL 18.6 image or the
  code before it was acted on. Dispositions name the change and the test or evidence that shows it.

## What the pre-review verified

It ran the compiler and PostgreSQL provider suites (608 passed), reproduced the live checked SUM
evidence and both engine conformance runs on the pinned images, read every cited test and the
compiler modules, and ran about forty probes against a scratch container of the pinned image.

## Findings and dispositions

### F1 (should-fix, borderline blocking) — the observation described a relation the statement never read

Precondition 10 compared an observation of the logical IIR relation `raw.revenue_events` with the
IIR's declared columns, while the emitted statement read `raw.raw_sales` and decoded fields from its
JSON payload with `->>`. Nothing tied the two. Confirmed live: a null or missing group key produced
a `NULL` group, grouping used the database's `en_US.utf8` collation rather than the `C` collation
that was observed, and the materialized group column was nullable.

**Disposition: fixed.**

- The PostgreSQL decode guard requires every landing field to be a JSON string and refuses anything
  else with `22P02`, and the region is grouped under an explicit `COLLATE "C"`.
- Precondition 10 and the admission chain bind the observation to the authority's source relation
  and check its generation (`TEXT`, non-null, deterministic collation, `UTF8`) and payload (`JSONB`,
  non-null) columns.
- The observer describes a whole landing relation, and observes SUM semantics for the statement's
  `NUMERIC(38,9)` input.
- Not changed: the materialized output columns are still declared nullable, because
  `CREATE TABLE AS` does not carry `NOT NULL`, and the provider's output inspection still checks only
  the measure column. The guard makes a NULL group value impossible to produce; it does not change
  the declared nullability.
- Evidence: `test_landing_relation_must_carry_the_columns_the_statement_reads`,
  `test_an_observation_of_another_relation_cannot_stand_in_for_the_landing_relation`, the live
  `null_group`, `missing_group` and `binary_collation_groups` cases, and the recorded group column
  collation `C`.

### F2 (should-fix) — NaN passed every cast

Confirmed live: `{"revenue":"NaN"}` summed to `NaN`, and the Decimal(57,9) result cast accepts NaN.
The runtime magnitude check did reject NaN, but nothing tested it.

**Disposition: fixed.** The canonical decimal pattern refuses NaN and Infinity before any cast. The
live evidence records `nan_measure` and `infinity_measure` refused with `22P02`, `result_cast_of_nan`
accepted by the cast alone, and `runtime_magnitude_flags_nan` flagged by the provider's violation
predicate.

### F3 (should-fix) — the unenforced input surface was far wider than declared

Confirmed live: PostgreSQL's numeric input read `"0x10"` as 16 and accepted `"1_000"`, `"1e3"`,
`" 7 "` and `"+5"`; group keys of any JSON type were stringified; and excess-scale rounding was a
second unenforced exclusion the coverage map did not declare.

**Disposition: fixed.** The decimal field must match `^-?(0|[1-9][0-9]{0,28})([.][0-9]{1,9})?$`.
Seventeen malformed forms are refused live with `22P02`. The coverage map's unenforced exclusions are
now derived from the live conformance results instead of declared by hand
(`test_unenforced_exclusions_are_exactly_the_negatives_postgresql_still_accepts`), and currently
there are none for PostgreSQL.

### F4 (should-fix) — the mutation result could not be reproduced

The proof note claimed a run covering four modules, but the committed `[tool.mutmut]` configuration
listed two. The authors confirmed the claim was wrong: mutmut reads `pyproject.toml` before
`setup.cfg`, so a temporary widening was silently ignored.

**Disposition: fixed.** The configuration now lists all four modules. A fresh run produced 862
mutants, 821 killed and 41 survivors, each classified in the proof note, which also records the
correction. The wider run found and killed one real gap in `prove_decimal_sum_bound`.

### F5 (should-fix) — the rule record contradicted the packet

The rule record listed preconditions 13, 14 and 16 as unsatisfied gates, pinned by a test, while the
code could satisfy them.

**Disposition: fixed.** The record now maps every gate to its precondition, and
`test_the_rule_record_names_exactly_the_gates_a_real_compile_leaves_open` derives the unsatisfied
gates from a fully evidenced compile, so the two cannot diverge again.

### F6 (note) — live magnitude evidence used a placeholder legality decision

**Disposition: disclosed.** The packet and proof note now state that the live journey proves the
magnitude check runs before publication, not that it is bound to a compiler decision.

### F7 (note) — identity digests are weaker than their names

The build digest is derived from the version string, and the image digest is the pinned reference
that was launched rather than something the server reports.

**Disposition: disclosed** in the live evidence report's engine section and the packet's known gaps.
The precondition 8 check itself is unchanged.

### F8 (note) — two tests passed for the wrong reason

The null and empty-group semantics test failed at strict revalidation, not at precondition 11, and
the overflow-semantics test used an invalid literal with the same effect.

**Disposition: fixed.** One test was renamed to state what it proves and asserts the refusal at
precondition 7; the other uses a valid but wrong value (`wrap`) so that precondition 11 itself
refuses it.

### F9 (note) — PostgreSQL-only scope existed in wording only

**Disposition: unchanged and disclosed.** Every compilation still refuses on both engines. The
per-engine activation record is designed but deliberately not built before independent review
(packet question Q4). Preconditions 15 and 17 are now both worded per engine.

## Answers the pre-review gave to the packet's questions

- **Q1:** reword precondition 15 per engine before closing it. Done.
- **Q2:** a JSON-number exclusion described only by the contract was not acceptable, and the
  unenforced surface was wider. A statement-level guard was chosen as the enforcement point.
- **Q3:** accept the cast probe with the integer proof for finite values only, not for NaN. NaN is
  now refused at decode.
- **Q4:** approve the design only if the activation record carries the reviewed commit and digests
  of the review file and rule record, verified by the compiler, with a recorded confirmatory review.

These answers are the pre-review's opinions. The independent reviewer decides each question.

## Second pass

- **Status: advisory. It approves nothing and does not satisfy precondition 18.**
- Reviewer: a fresh instance of the same different-family model, with no access to the first pass
  beyond this record, instructed to test each disposition above rather than accept it.
- Revision reviewed: `f73e85c8dcfc4a5d6bf7a83ee12c8c3a43c356c0`
- Recommendation at that revision: **ready for human review after listed fixes** (S1, S2, S3, S4).
- It confirmed F1, F2, F3, F5, F8 resolved and F6, F7 disclosed; F4's configuration change was
  confirmed by reading, while its mutation counts were not independently reproduced; F9 remains
  unresolved by design and disclosed. It re-ran every live suite (all passed) and about 55 engine
  probes. Attacks that held included forced parallel query, forced JIT, prepared statements,
  `CREATE TABLE AS`, 1000 valid rows, top-level non-object payloads, planner folding of the refusal
  branch, and leakage of payload values through the error message.
- As before, every finding below was checked against the pinned engine or the code before it was
  acted on.

### S1 (should-fix) — the decode guard depended on the session search_path

Confirmed live: with `search_path = shadow, pg_catalog` and a shadow `jsonb_typeof` and `~`, the
statement returned `16.000000000` for `"0x10"`.

**Disposition: fixed.** Every function, operator, type and collation in the admitted PostgreSQL
statement is qualified to `pg_catalog`, including the outer `sum` and the generation filter's `=`.
Verified live against a session that also shadows `sum`, `=`, `->>` and `||`: valid rows return the
same result, hex is still refused, and the generation filter holds. Evidence: the
`shadowed_search_path_*` cases in the live bundle, and
`test_postgresql_generation_statement_resolves_nothing_through_search_path`. The plain, never
admissible `emit_postgresql` syntax candidate was left unqualified.

### S2 (should-fix) — the observer reported an unvalidated NOT NULL column as non-null

Confirmed live: PostgreSQL 18 sets `attnotnull` for `NOT NULL ... NOT VALID` over an existing NULL.

**Disposition: fixed.** The observer reports a column non-null only if no unvalidated not-null
constraint stands behind `attnotnull`. Evidence:
`test_live_observer_does_not_trust_an_unvalidated_not_null_constraint`, which fails with the old
query.

### S3 (should-fix) — the proof note contradicted itself on D7

Confirmed. An earlier edit replaced the first matching D3 and D7 rows, which belonged to the coverage
table, instead of the rows in the cross-engine table.

**Disposition: fixed.** The coverage table rows are restored and the cross-engine rows carry the
current text.

### S4 (note) — the ADR said the rule "is activated"

**Disposition: fixed.** The amendment now says the rule is to be activated on independent approval
and that nothing is activated yet; the packet says the amendment provides for per-engine activation.

### S5 (note) — COLLATE "C" changes the sort order consumers see

**Disposition: disclosed** in the live evidence report and the packet's known gaps.

### S6 (note) — the pattern is not canonical

**Disposition: fixed in prose.** It is described as a restricted decimal literal, with `0`, `-0`
and `0.0` noted as equal values. The constant and test names still say "canonical".

### S7 (note) — duplicate JSON keys keep the last value

**Disposition: disclosed** in the live evidence report, proof note and packet's known gaps. Not
enforced here.

### S8 (note) — relation_ref has two meanings

**Disposition: disclosed** in the proof note's observation boundary section.

### S9 (note) — newline and Unicode-digit refusal rested on Python regex tests

**Disposition: fixed.** The live bundle now records a trailing newline, an Arabic-Indic digit and a
fullwidth digit, each refused with `22P02` on the pinned engine.

### S10 (note) — identity digests carry no architecture

**Disposition: disclosed.** The live evidence report states that the image reference is a
multi-architecture manifest, that the build digest carries no architecture, and that every capture of
the bundle ran the arm64 variant.

### S11 (note) — observation-to-execution gap and no relation kind

**Disposition: disclosed** in the proof note and the packet's known gaps.

## Second-pass answers to the packet's questions

- **Q1:** not yet; close precondition 15 only when the runtime check is bound to a real compiler
  decision digest, and keep it per engine.
- **Q2:** a statement-level guard is acceptable only once it is independent of `search_path` (now
  done); a landing-time check is desirable defence in depth, including for duplicate keys, but need
  not gate approval.
- **Q3:** yes for finite values.
- **Q4:** approve the design only with an engine-keyed activation record carrying the reviewed commit
  and digests of the review file and rule record, verified by the compiler, a negative ClickHouse
  test, a confirmatory review, and a pinned `search_path` for the materialization principal as
  defence in depth.

These answers are the pre-review's opinions. The independent reviewer decides each question.
