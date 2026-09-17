# Review packet: PRODUCT-SQL-V2-PROJECT-SUM-001, PostgreSQL activation

- Purpose: everything an independent reviewer needs to decide precondition 18 for **PostgreSQL
  only**. This packet approves nothing and records no decision.
- Rule under review: `PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE`
- Scope sought: activation for `postgresql`. **No cross-engine equivalence is claimed and
  ClickHouse is not in scope.** Approving this packet must not be read as approving ClickHouse.
- Prior review: `services/compiler/legality/product-sql/reviews/PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE-review.md`
  (status: Changes requested).
- Decision record to update on approval: that same review file.

## 1. Who may review

AGENTS.md requires an independent reviewer and forbids self-certification. The work was authored
by AI agents (Codex and Claude) under the direction of the repository owner. The reviewer must be
none of those, must not have designed this rule, and should be comfortable with SQL decimal
precision, overflow and aggregate semantics, and with reading a proof against test evidence.

## 1a. An advisory AI pre-review has already been run

Before this packet went to a human reviewer, a different AI model reviewed it adversarially with no
access to the authors' reasoning. That review is **advisory and approves nothing**; it does not
satisfy precondition 18. It recommended "not ready" and its findings, each verified against the
pinned engine or the code before being acted on, are recorded with their dispositions in
`services/compiler/legality/product-sql/reviews/PRODUCT-SQL-V2-PROJECT-SUM-001-POSTGRESQL-ADVISORY-PREREVIEW.md`.
You are encouraged to check that each disposition is real rather than take it on trust.

## 2. What is being asked, in two phases

The rule's proof and evidence are ready to review now. Three pieces of engineering are not
finished (section 7), and one of them is designed to be built only after approval. So the review
has two phases:

1. **Now: review the proof, scope and evidence.** Raise change requests freely. An approval in
   this phase approves the rule, its PostgreSQL-only scope, and the design of the remaining work.
2. **Before activation: confirm the finished revision.** Approval must name the exact commit it
   covers. Code that lands after that commit, including the admission code in section 7, needs a
   confirmatory check before the rule is switched on.

Verify the revision you are reading with `git rev-parse HEAD`. The approval must name that commit.

## 3. Where the rule stands today

A PostgreSQL candidate compiled with every artifact the code accepts, all of them signed,
satisfies 15 of 18 preconditions. The three that remain are hardcoded unsatisfied because each is
a governed decision, not missing evidence. This state is pinned by
`services/compiler/tests/test_product_physical_plan_candidate.py::test_a_fully_evidenced_postgresql_candidate_leaves_only_the_governed_gates_open`.

| # | Precondition | Status |
| --- | --- | --- |
| 1-6 | restricted project-then-sum shape, direct inputs, grain, one non-null decimal measure, non-null binary string groups, distinct output names | satisfied |
| 7 | observation bound to expected tenant, warehouse, relation and digest | satisfied |
| 8 | pinned engine version, image and build bound to the observation | satisfied |
| 9 | observation no older than ten minutes | satisfied |
| 10 | the landing relation the statement reads is observed: non-null text generation column and non-null jsonb payload | satisfied |
| 11 | pinned engine-specific SUM input, accumulator, result, overflow, null and empty-group semantics observed | satisfied |
| 12 | NUMERIC(38,9) sums proven to fit Decimal(57,9) at the signed ledger ceiling | satisfied |
| 13 | physical source bound to exactly one authoritative generation and receipt digest | satisfied |
| 14 | contributing row ceiling bound to owning-service cardinality evidence | satisfied |
| 15 | checked Decimal(57,9) result magnitude enforced at runtime on the pinned PostgreSQL engine | **unsatisfied** (see question Q1) |
| 16 | observation authenticated by provider-owned provenance | satisfied |
| 17 | live checked SUM reviewed on pinned PostgreSQL; cross-engine equivalence not claimed | **unsatisfied**, decided by you |
| 18 | independent legality review approves the rule | **unsatisfied**, decided by you |

## 4. The prior review's objections, and what changed

| Prior finding | What exists now | Evidence |
| --- | --- | --- |
| An output-name collision was admissible. | The shared shape predicate refuses grouped and measure output names that collide, and the emitter re-checks it. | `services/compiler/tests/test_restricted_product_aggregate_sql.py::test_emitter_rejects_a_measure_output_that_collides_with_a_group_output` |
| Caller capability claims did not establish provider authority. | Providers sign observations with Ed25519 inside the provider boundary. The compiler verifies the envelope, freshness and identity; a raw observation never satisfies provenance. | `services/compiler/tests/test_restricted_product_aggregate_sql.py::test_raw_provider_observation_remains_untrusted_for_provenance`, `services/compiler/tests/test_restricted_product_aggregate_sql.py::test_signed_provider_provenance_fails_closed_for_invalid_or_mismatched_envelopes`, `services/compiler/tests/test_restricted_product_aggregate_sql.py::test_provider_observation_digest_tampering_fails_closed`, `providers/postgresql/tests/test_product_sql_observation.py::test_observe_signed_rejects_key_substitution` |
| Exact cross-engine decimal SUM semantics were not established; pinned probes showed ClickHouse wraps where PostgreSQL promotes. | That finding stands and is not disputed. The scope was narrowed instead: the ADR-0004 amendment of 2026-09-15 provides for activating the rule per engine on approval, and preconditions 15 and 17 are worded per engine. The rule record keeps ClickHouse's `outer_cast_enforces_magnitude: false`. | `docs/architecture/decisions/ADR-0004-compilation-target.md`, `services/compiler/legality/product-sql/rules/PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE.json`, `services/compiler/tests/test_restricted_product_aggregate_sql.py::test_live_sum_review_is_worded_per_engine_and_stays_unsatisfied` |

The prior review said re-review must cover the exact revision, the D1-D8 proof, engine fixtures,
identifier cases, provider-pair regression, mutation results, provider-owned evidence, and live
result and overflow observations. Section 5 lists each.

## 5. The evidence, in reading order

1. **Proof note.** `services/compiler/legality/product-sql/proof-notes/PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE.md`.
   Read the candidate shape, the D1-D8 coverage map, and "Evidence and remaining work".
2. **D1-D8 coverage.** `services/compiler/legality/product-sql/fixtures/d1-d8-coverage.json` maps
   every dimension to its fixtures and tests, and `services/compiler/tests/test_d1_d8_coverage.py`
   keeps that map honest (every case mapped, polarity matching the fixture, every cited test
   existing). Locale (D4) and timezone (D5) rest on the shape predicate, pinned by
   `services/compiler/tests/test_locale_and_timezone_exclusion.py`.
3. **Live PostgreSQL evidence.** `services/compiler/legality/product-sql/proof-notes/PRODUCT-SQL-V2-PROJECT-SUM-001-POSTGRESQL-LIVE-EVIDENCE.md`,
   with its bundle `services/compiler/legality/product-sql/fixtures/postgresql-live-checked-sum-evidence.json`.
   Real results from the pinned PostgreSQL 18.6 image for zero, negative, scale nine, maximum and
   minimum inputs, the two-row sum past Decimal(38,9), both input bounds, both Decimal(57,9)
   result bounds, the empty group, the excluded generation, seventeen malformed landing values
   refused by the decode guard, bytewise `C`-collation grouping, a NaN result-cast probe, and the
   runtime magnitude predicate on NaN and 10^48. Its engine section states what the image and
   build digests do and do not bind: the image digest is the pinned reference that was launched,
   not something the server reports, and the build digest is derived from the version string.
4. **Engine fixtures and provider-pair regression.** `services/compiler/legality/product-sql/fixtures/generation-scoped-json-conformance.json`,
   run live by `tests/integration/test_generation_scoped_product_sql_conformance.py::test_pinned_postgresql_generation_json_conformance`.
   The ClickHouse fixtures and its live test also exist; they record divergence, not a claim.
5. **Identifier cases.** `services/compiler/tests/test_restricted_product_aggregate_sql.py::test_identifier_security_rejects_sql_syntax_wildcards_and_confusables`,
   `services/compiler/tests/test_restricted_product_aggregate_sql.py::test_emitter_revalidates_identifiers_after_unsafe_model_copy`,
   `services/compiler/tests/test_generation_scoped_product_sql.py::test_generation_source_rejects_hostile_physical_and_json_identifiers`.
6. **Mutation results.** Recorded in the proof note's "Evidence and remaining work": 862 mutants
   across the four modules, 821 killed, 41 survivors, each classified, reproducible from the
   committed `[tool.mutmut]` configuration. The note also corrects an earlier, wrong coverage claim. Admission is pinned per condition by
   `services/compiler/tests/test_product_physical_plan_candidate.py::test_each_authority_binding_is_required_to_match_on_its_own`,
   landing-relation binding by
   `services/compiler/tests/test_product_physical_plan_candidate.py::test_landing_relation_must_carry_the_columns_the_statement_reads`
   and `services/compiler/tests/test_product_physical_plan_candidate.py::test_an_observation_of_another_relation_cannot_stand_in_for_the_landing_relation`,
   and refusal reasons by `services/compiler/tests/test_shape_refusal_attribution.py`.
7. **Cardinality and the numeric bound.** `services/compiler/tests/test_product_sum_bounds.py::test_decimal_sum_bound_proves_the_signed_ledger_ceiling`
   and `services/compiler/tests/test_product_physical_plan_candidate.py::test_signed_cardinality_evidence_must_match_every_physical_plan_parent`.
8. **PostgreSQL runtime magnitude enforcement** (relevant to Q1).
   `providers/postgresql/tests/test_product_materialization.py::test_signed_decimal_magnitude_check_rejects_exclusive_bounds`,
   `providers/postgresql/tests/test_product_materialization.py::test_switch_rejects_post_execute_magnitude_mutation_before_publication_effect`,
   and, on the pinned PostgreSQL 18.6 image,
   `tests/integration/test_postgresql_compiled_product_journey_live.py::test_compiled_product_journey_reaches_the_governed_gates_and_materializes_on_the_pinned_engine`.
   That journey acquires and lands real rows, observes and signs the landing relation, composes the
   physical plan with the compiler, signs cardinality from the generation ledger, and compiles: only
   preconditions 15, 17 and 18 remain. It then materializes the compiler's guarded statement through
   dbt, running the magnitude check before publication, and the product reconciles to the source
   rows. **Limit:** nothing is admitted, so the materialization is authorized with a legality
   decision digest derived from the compiler's refusal; it proves the check runs, not that it is
   bound to an approval. The older
   `tests/integration/test_postgresql_product_materialization_live.py::test_fresh_source_acquisition_land_and_dbt_materialization_commit_one_generation`
   starts PostgreSQL from local binaries (PostgreSQL 14.17 when it was last run for this packet),
   not the pinned image, and uses hand-written SQL; it is not evidence about the pinned engine.

## 6. Questions that need your decision

- **Q1. Precondition 15.** It now reads "enforce checked Decimal(57,9) result magnitude at runtime on
  the pinned PostgreSQL engine", worded per engine as 17 is. PostgreSQL's enforcement is built and
  live-tested (item 8), including rejection of NaN, but its live journey does not yet involve a
  compiler decision. Is PostgreSQL's enforcement, bound to the activation record in section 7,
  sufficient to close it?
- **Q2. The landing decode guard.** The contract requires landing fields to arrive as JSON strings,
  and the decimal field as a restricted Decimal(38,9) literal. The PostgreSQL statement now enforces
  both itself, fails with SQLSTATE `22P02` otherwise, and resolves every name in `pg_catalog`, so a
  hostile `search_path` cannot defeat it (section 5, item 3). Do you accept a
  statement-level guard as the enforcement point, or do you also require a landing-time check
  attested in the landing receipt, which precondition 13 already binds?
- **Q3. The Decimal(57,9) result bound is observed on the cast, not reached through a SUM.**
  Reaching 10^48 through a real SUM would take on the order of 10^19 maximum-valued rows. The cast
  alone accepts NaN, which the decode guard keeps out. Do you accept the cast probe together with
  the integer proof (precondition 12) as sufficient for finite values?
- **Q4. The per-engine activation record does not exist yet.** It is built with the compiler's
  success path, after approval, so that what you approve defines what is admitted. Today every
  compilation on either engine refuses. Do you approve the design (admission keyed on the engine
  and on a per-engine record carrying the approved commit and the digests of the review file and
  rule record, with a negative test that ClickHouse still refuses after PostgreSQL activation),
  subject to a recorded confirmatory review of that code?

## 7. Known gaps, stated plainly

- **Signed cardinality in the request journey:** precondition 14 is satisfied with signed evidence
  in tests, but the live request journey does not yet supply it.
- **Execution authorization bound to the exact candidate:** not yet composed.
- **Per-engine activation record and admission path** (Q4): designed, not built.
- **Runtime magnitude bound to a compiler decision** (Q1): the live journey uses a placeholder
  legality decision.
- **Identity binding is version-level:** precondition 8 binds the version string and the pinned
  image reference, not a build of the binary.
- **Duplicate JSON keys:** `jsonb` keeps the last value of a duplicated key, so a landing payload with
  two `revenue` keys is decoded, not refused. Whether landing rejects them is not established here.
- **Observation to execution:** nothing re-checks the landing relation between observation and
  execution beyond the ten-minute freshness bound, and the observation model carries no relation
  kind; views are refused only inside the PostgreSQL observer.
- **Sort order:** grouping under `C` also makes the region column sort bytewise, which differs from
  the database default's order. Group membership is unchanged.
- **ClickHouse:** out of scope. It has no decode guard, no landing observation contract, and needs
  its own magnitude authority, fixtures, live evidence and review.

## 8. Reproducing the evidence

```sh
uv sync --locked --all-packages
uv run pytest -m "not live"
PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE=1 uv run pytest tests/integration/test_postgresql_checked_sum_evidence.py tests/integration/test_generation_scoped_product_sql_conformance.py -m live
```

The live runs need Docker and pull the digest-pinned images. They fail if any engine result
differs from the recorded evidence.

## 9. What an approval must record

In `services/compiler/legality/product-sql/reviews/PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE-review.md`:

- status: Approved, Approved with conditions, or Changes requested;
- the reviewer's name and the date;
- the exact commit SHA reviewed;
- the approved engine scope, stated as `postgresql` only;
- an answer to each of Q1-Q4, and any conditions on activation.
