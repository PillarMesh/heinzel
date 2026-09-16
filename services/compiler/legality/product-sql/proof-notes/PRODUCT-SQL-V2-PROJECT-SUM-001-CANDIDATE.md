# Proof candidate: PRODUCT-SQL-V2-PROJECT-SUM-001

- Status: Changes requested; incapable of admission
- Rule version: 1
- Scope: schema-v2, one source, direct-column project, then one grouped decimal sum
- Engine profiles: pinned PostgreSQL 18.6 and ClickHouse 25.8.32.4 image digests
- **Activation scope: `postgresql` only** (ADR-0004 amendment 2026-09-15). This candidate seeks
  single-engine activation. It does **not** claim cross-engine result equivalence, and approving it
  does not admit ClickHouse. Every dimension below records ClickHouse as `not_claimed`; ClickHouse
  activation requires its own runtime magnitude authority, its own fixtures, a live cross-engine
  equivalence run, and a second independent review.
- Restrictions that exist because the engines diverge remain in force under single-engine
  activation. They are listed as `divergence_motivated_exclusions` in the coverage map and must not
  be relaxed: widening an activated rule later is ordinary governance, but narrowing one after
  activation invalidates evidence already accepted for it.

## Candidate shape

The candidate accepts exactly a `ProjectOperation` followed by an `AggregateOperation`. The project
contains exactly the aggregate group columns followed by the single measure input. Every expression
is a direct reference to a declared column of the source relation. Projected input aliases preserve
their source names, so no alias is silently discarded. The aggregate groups by exactly the ordered
product grain. Group columns are non-null strings and the measure is one non-null decimal. The
canonical schema-v2 measure explicitly carries `function="sum"`, its argument, and its output alias;
the emitter does not infer the aggregate function.

Grouped projection aliases and the measure alias are pairwise distinct. The emitter re-evaluates the
same complete shape predicate before rendering, including when a caller bypasses Pydantic validation
with `model_copy`. For this shape, PostgreSQL renders
`CAST(SUM(CAST(argument AS NUMERIC(57,9))) AS NUMERIC(57,9))`; ClickHouse renders
`CAST(sum(CAST(argument AS Decimal(57, 9))) AS Decimal(57, 9))`. These are syntax candidates only.
The compiler always returns `NoValidPlan`. Provider-owned provenance can pass for a valid, fresh,
identity-matching Ed25519 envelope. Given strict owning-service source and target bindings, the
compiler composes the generation-scoped SQL into a revalidated `ProductPhysicalPlan` candidate and
satisfies precondition 13. Runtime can derive, sign, and durably replay cardinality evidence from
committed LAND receipts; the compiler satisfies precondition 14 only when that Ed25519 envelope binds
the exact plan, source generation, receipt, contract, tenant, and Decimal(38,9) proof. Execution
authorization, ClickHouse materialization and answer integration, complete JSON-decoding semantics,
live cross-engine review, and independent review remain separate gates.

The original candidate syntax names only the source namespace and relation. Separate deterministic
PostgreSQL and ClickHouse generation-scoped emitters now accept the immutable source contract, decode
the ordered string and Decimal(38,9) fields from the landing JSON payload, and filter on exactly one
validated SHA-256 generation identifier. The source contract also carries the landing receipt and
observed source-schema digests. Exact SQL and invalid-source tests exist for both dialects. The
compiler composes this emitter into a strict candidate that binds the IIR, tenant, product, contract,
warehouse, provider observation, generation, landing receipt, observed source schema, target,
statement, output schema, and Decimal57 checks. The candidate remains unapproved and cannot be signed
or executed.

## Provider observation boundary

PostgreSQL and ClickHouse provider boundaries can now produce provider-owned Ed25519 observation
envelopes. Given a signed envelope, an injected verifier, and an injected UTC evaluation time, the
compiler verifies the envelope internally and satisfies provenance only when the verified observation
is fresh and matches the expected tenant, warehouse binding identifier and revision, relation
reference and namespace, engine, pinned version and image, and build digest. The namespace must also
equal the canonical IIR source namespace. Raw observation compatibility remains for existing callers,
but a raw value cannot satisfy provenance.

The compiler reconstructs every observation through the strict model before inspecting it. Invalid
`model_copy` or `model_construct` values become an attributable `NoValidPlan`; serialization failures
also fail closed with a fixed message that does not expose the underlying exception.

The signed envelope authenticates the observation to an injected trusted provider key; its expected
digest still binds the exact verified value. Tampering, unknown keys, stale or future observations,
malformed copied or constructed envelopes, wrong tenant, binding, or provider, and missing verifiers
all leave provenance unsatisfied with a fixed non-sensitive explanation. Caller-supplied legacy
version and capability strings cannot satisfy observation or provenance preconditions.

The PostgreSQL and ClickHouse emitters are pure syntax backends. Their `SqlEmission` contains only
an engine, statement, and parameters; it has no rule identifier, admission decision, signature, or
execution graph. A repository-wide caller search found no production execution caller of these
functions. Rendering this candidate therefore cannot create signed or executable authority.

## D1-D8 coverage map

`legality/product-sql/fixtures/d1-d8-coverage.json` is the machine-checked map from each dimension
to the artifacts that carry it. `services/compiler/tests/test_d1_d8_coverage.py` enforces it: every
conformance case is mapped to at least one dimension and every mapped case exists, the case tags and
the map agree, every referenced test exists as a real function, every dimension carries positive
evidence or records why none applies, the divergence-motivated exclusions stay recorded, and every
dimension reports ClickHouse as `not_claimed`. Mutating any one of those properties fails the suite.

| Dimension | Carried by | Positive evidence | Negative evidence |
| --- | --- | --- | --- |
| D1 null and missing | engine cases | `valid_rows` | `missing_group`, `null_group`, `missing_measure`, `null_measure` |
| D2 precision and range | engine cases | `decimal_zero`, `decimal_negative`, `decimal_scale_nine`, `decimal_max_positive`, `decimal_max_negative`, `decimal_safe_multirow_sum` | `decimal_outside_precision`, `decimal_excess_scale` |
| D3 collation | engine cases | `case_variant_groups`, `unicode_composed_decomposed_groups`, `trailing_space_groups` | none applies: every distinct byte sequence is a distinct group, so no input is inadmissible on collation grounds |
| D4 locale | compiler boundary | `test_the_baseline_product_satisfies_every_shape_check` | `test_d4_case_conversion_in_the_projection_is_not_a_direct_input`, `test_d4_upper_case_conversion_is_rejected_on_the_same_ground` |
| D5 timezone | compiler boundary | evaluation-time checks | `test_d5_a_timestamp_group_column_cannot_be_grouped`, `test_d5_a_timestamp_measure_cannot_be_summed`, `test_non_utc_evaluation_time_fails_closed`, `test_aware_non_utc_evaluation_time_fails_closed` |
| D6 identifier encoding | compiler boundary | revalidated quoted emission | `test_identifier_security_rejects_sql_syntax_wildcards_and_confusables`, `test_emitter_revalidates_identifiers_after_unsafe_model_copy`, `test_generation_source_rejects_hostile_physical_and_json_identifiers` |
| D7 literal typing | engine cases | `valid_rows` | `malformed_decimal`, `wrong_type_decimal` |
| D8 relational behavior | engine cases | `valid_rows`, `wrong_generation_excluded`, `empty_selected_generation` | shape predicate: `test_shape_evaluation_rejects_a_reference_outside_the_declared_source_alias`, `test_emitter_never_invents_an_aggregate_function`, `test_emitter_rejects_a_measure_output_that_collides_with_a_group_output` |

D4 and D5 deserve a note, because their invariants are weaker than the original prose implied. The
IIR is not free of locale-sensitive or temporal vocabulary: `ApprovedFunctionName` includes the
case-conversion functions `lower` and `upper`, `ScalarType` includes `timestamp`, and
`LiteralScalar` includes `datetime`. The claim that no such construct reaches an emitted statement
therefore rests entirely on the restricted project-sum shape predicate, not on the absence of the
vocabulary. `services/compiler/tests/test_locale_and_timezone_exclusion.py` pins that predicate
directly, and includes a baseline case so the rejections are attributable to the construct under
test rather than to an already-failing product.

## Cross-engine semantic argument

| Dimension | Candidate invariant or open obligation |
| --- | --- |
| D1 null and missing | The IIR declares group and measure inputs non-null. A pinned-engine generation-JSON fixture now records that PostgreSQL maps missing/null groups to SQL NULL and missing/null measures to a NULL aggregate, while ClickHouse maps missing/null groups to an empty string and rejects missing/null measures. These cases remain invalid and cannot be admitted. There is no join or filter. |
| D2 precision and range | Fresh pinned-engine probes observed PostgreSQL NUMERIC(38,9) input with INTERNAL transition state, NUMERIC result, and promotion, while ClickHouse Decimal(38, 9) retained that type and wrapped on overflow. With the proposed widening, PostgreSQL's inner cast is NUMERIC(57,9), `SUM` returns unconstrained NUMERIC, and the outer NUMERIC(57,9) cast rejects values beyond its declared precision. The PostgreSQL materialization boundary takes compiler-signed Decimal57/9 output-check declarations, re-observes the committed output relation, rejects null or values at and beyond the exclusive `-10^48` and `10^48` bounds, and includes zero violations in its provider commit reference. The current answer reader verifies that same signed declaration, rechecks the locked relation, and reconstructs the commit reference before returning rows. A fresh live PostgreSQL source-to-answer transaction exercised this boundary, but compiler admission and the complete request-to-product journey remain absent. ClickHouse's inner cast is Decimal(57, 9), `sum` returns Decimal(76, 9), and its outer Decimal(57, 9) cast is nominal. A new provider boundary verifies the compiler-signed Decimal57/9 declarations and counts null or values at and beyond the exclusive bounds in one read-only query, preserving signed order and rejecting malformed or nonzero evidence. It is not integrated with a committed generation or answer reader and has no fresh live result-equivalence evidence. The pure integer proof establishes `(2^63 - 1) * (10^38 - 1) < 10^57` and `< 2^255`, so Decimal(57,9) can exactly represent the sum if an authoritative contributing-row ceiling is at most the signed-ledger maximum. No caller row count is accepted as evidence. Fresh pinned-engine fixtures agree for zero, negative, exact scale-nine, maximum positive/negative inputs, a safe two-row maximum sum, and rejection outside Decimal(38,9) precision. PostgreSQL rounds excess scale while ClickHouse truncates it, so that input remains inadmissible. Runtime-signed cardinality verification is implemented at the compiler boundary, but the request journey does not yet compose that authority. ClickHouse publication integration and cross-engine review remain unsatisfied. |
| D3 collation | Group strings use profile-specific binary semantics (`C`/`UTF8` or `binary`/`UTF-8`). Fresh pinned-engine fixtures keep case variants, composed/decomposed Unicode, and trailing-space values in distinct groups with equivalent row sets. Provider sort order differs for case variants, and result ordering is not claimed. |
| D4 locale | No parsing, formatting, case conversion, or locale-sensitive function exists. |
| D5 timezone | No timestamp expression exists. Observation timestamps and the injected evaluation time are UTC. |
| D6 identifier encoding | Schema-v2 identifiers use a lowercase ASCII allowlist and every emitted identifier is revalidated and quoted. |
| D7 literal typing | The logical shape contains no request literals. The generation-scoped emitter renders only the source contract's validated lowercase SHA-256 generation identifier; arbitrary caller SQL remains impossible. The contract requires the decimal field to arrive as a JSON string, but **nothing enforces that**: the emitter decodes with `->>`, which stringifies a JSON number. Verified live against the pinned PostgreSQL 18.6 image on 2026-09-15 -- the payload `{"revenue": 12.5}` returns the row `(east, 12.500000000)` rather than being refused. The fixture records the engine's behaviour, not a refusal, and the coverage map carries this as an unenforced exclusion. The compiler-side mutation regression is complete; see the evidence section. |
| D8 relational behavior | One source, direct projection, one group, and one sum are present. Joins, filters, deduplication, ordering, limits, and additional measures are excluded. Fresh pinned-engine fixtures produce equivalent valid aggregates, exclude a different generation, and return no rows for an empty selected generation. The deterministic one-generation source renderer is bound into a strict physical-plan candidate, but that candidate has no admitted legality decision or execution authorization. |

## Evidence and remaining work

Positive and nullable-negative aggregate fixtures exist for both engines. Provider-owned Ed25519
observation production and compiler verification have unit coverage, including tamper, identity,
freshness, malformed-envelope, and non-sensitive failure cases; raw observations remain untrusted.
Generation-scoped JSON decoding has exact positive SQL and invalid-source unit tests for PostgreSQL
and ClickHouse. The compiler-side D1-D8 mapping and mutation regression are complete. These tests
still do not form the live provider-pair fixtures, which require the gated conformance run against
both pinned images, and they do not prove numeric result equivalence.

A focused mutation run on 2026-09-15 covered `restricted_sql.py`, `numeric_bounds.py`,
`generation_sql.py` and `product_compiler.py` against the compiler suite: 758 mutants, 732 killed,
26 survivors, every survivor inspected. The run killed five previously surviving mutants in the
admission chain of `_compose_physical_candidate` by pinning each condition on its own -- a missing
authority, an authority whose tenant, warehouse binding identifier or binding revision does not match
the expected value, an observation whose engine does not match the requested engine, and an
observation digest that does not match the expected digest. Before those tests the chain was pinned
only in aggregate, so replacing any single `or` with `and` went undetected.

The 26 survivors fall into named classes, none of which is a reachable weakening:

- Three toggle `zip(strict=...)` after an exact length equality guard.
- Six mutate `_revalidate_observation` internals -- Pydantic dump mode, an explicit strict flag that
  model configuration already requires, and error-input inclusion on a path no assertion reads.
- Three replace a falsy initialiser with another falsy value.
- Three alter the text of one refusal message.
- Two relax the post-check defensive guard in `require_project_sum_shape`, which is unreachable
  because the shape checks already reject both a missing projection and a missing aggregate.
- Two weaken guards inside `_verify_signed_cardinality_evidence` and one inside
  `_cardinality_evidence_is_bound` whose effect is absorbed by the surrounding `try` block: the
  relaxed path dereferences `None` and raises `AttributeError`, which the same handler converts back
  into the refusal the guard would have produced.
- Two in `_compose_physical_candidate` relax the `authority is None` and `provider_observation is
  None` guards; both are absorbed the same way, and `test_admission_requires_an_authority` confirms
  the refusal still occurs.
- Two flip the verified flag returned by `_verify_signed_observation` from `False` to `True` on its
  failure paths. Both are masked by the explicit `observation is not None` conjunct in
  `_observation_is_bound`, which precondition 16 evaluates alongside the flag, so a forged flag
  cannot satisfy provenance on its own.
- One flips the `prove_decimal_sum_bound` failure path in `_cardinality_evidence_is_bound` from
  `False` to `True`. The path is unreachable through the signed entry point: the evidence model
  constrains the contributing row ceiling, and the signer revalidates before signing, so an
  out-of-range ceiling cannot reach the proof call.
- One relaxes the null and empty-group conjunction in `_sum_semantics_match`. See below.

**The null and empty-group invariant is carried by the type, not by the compiler comparison.**
`ProductSqlSumSemantics` declares `null_input_behavior: Literal["exclude"]` and
`empty_group_behavior: Literal["no_row"]`, each single-valued, and strict revalidation rejects any
other value at precondition 7 before precondition 11 is evaluated. The comparison in
`_sum_semantics_match` is therefore unreachable, and
`test_each_sum_null_semantic_is_independently_required` is vacuous: both of its parametrised cases
are refused at precondition 7, not at precondition 11. The invariant holds, but the proof of it is
the declared literal plus strict revalidation, and this note must not claim the compiler predicate
enforces it. D1 does not rest on that predicate; the coverage map records D1 as carried by engine
cases with no compiler test cited.

**D7 literal typing is not enforced anywhere.** The checklist item for this milestone asked for
proof that the compiler and the PostgreSQL provider boundary reject a numeric JSON value where the
contract requires a JSON string. The proof was attempted against the pinned PostgreSQL 18.6 image
and it failed: the statement returns `(east, 12.500000000)`. The emitter decodes every binding with
`payload ->> 'field'`, which stringifies whatever JSON type is present, and the subsequent
`CAST(... AS NUMERIC(38,9))` then succeeds. No compiler check and no provider-boundary check refuses
the value. The live run is reproducible with
`PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE=1 uv run pytest tests/integration/ -m live`, and the
execution was confirmed by perturbing the expected row and observing the engine-reported value.

This is recorded, not repaired. Choosing the enforcement point -- a JSON type guard in the emitted
statement, a landing-time contract check, or an observed-source-schema obligation -- decides what
the legality rule claims and how ClickHouse can later join it, so it belongs to the rule's owners
and the independent reviewer, not to the agent that found it. Until an enforcement point is
accepted, D7 must be described as carried by the contract text alone.

The same run also corrected a refusal-attribution defect it exposed. The first shape check asserted
only that a projection was present, so a product carrying no aggregate was refused with the grain
message, naming a constraint the product never reached. The check now requires both operations, and
`test_shape_refusal_attribution.py` pins each structural refusal to its own ground.

An earlier bounded run against the compiler bound precondition killed both generated mutants.
The numeric proof has direct tests
at zero, one, the signed-ledger maximum, negative, boolean, and over-maximum boundaries. The emitter
syntax has exact dialect-specific regression tests.

**Live checked SUM evidence for PostgreSQL** is recorded in
`PRODUCT-SQL-V2-PROJECT-SUM-001-POSTGRESQL-LIVE-EVIDENCE.md`, with its bundle in
`fixtures/postgresql-live-checked-sum-evidence.json`. Against the pinned PostgreSQL 18.6 image, two
maximum-magnitude Decimal(38,9) inputs sum past Decimal(38,9) and return exactly, the measure column
is declared `numeric(57,9)`, and both the input bound and the Decimal(57,9) result bound refuse with
SQLSTATE `22003` rather than rounding or wrapping. The result bound is observed on the exact cast
the statement applies, not reached through a real SUM; the integer proof above is what shows a SUM
bounded by the signed ledger ceiling cannot reach it. Precondition 17, now worded per engine, stays
unsatisfied until the independent reviewer accepts that evidence.

PostgreSQL activation additionally requires composing the runtime-signed cardinality artifact into
the request journey, an execution authorization bound to the exact candidate, an accepted
enforcement point for D7 literal typing, and approval by an independent reviewer. ClickHouse
activation separately requires ClickHouse materialization and answer integration for the new
magnitude observer, its own live D1-D8 fixtures and checked SUM evidence, and its own review.
