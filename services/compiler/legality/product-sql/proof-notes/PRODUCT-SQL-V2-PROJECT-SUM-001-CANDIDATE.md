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

**PostgreSQL landing decode guard.** The PostgreSQL generation-scoped decoder does not trust the
landing payload or PostgreSQL's permissive casts. Each field must be present as a JSON string
(`pg_catalog.jsonb_typeof(...)` must be `'string'`); the decimal field must also match the
restricted decimal literal `^-?(0|[1-9][0-9]{0,28})([.][0-9]{1,9})?$`. It admits only finite
Decimal(38,9) values and no leading zeros, but it is not a canonical form: `0`, `-0` and `0.0`
denote the same value. Any other value takes a refusal branch that casts a message naming the row's
generation to `NUMERIC`, raising SQLSTATE `22P02`; referencing the generation column keeps the
planner from folding that cast into a constant. The decoded region is grouped under an explicit
`COLLATE pg_catalog."C"`. Every function, operator, type and collation in the statement is
qualified to `pg_catalog`; an unqualified name resolves through the session's `search_path`, and a
second advisory pre-review showed live that a hostile schema listed before `pg_catalog` could replace
`jsonb_typeof` and `~` and let `0x10` through as 16. The live evidence now includes hostile-session
cases. Duplicate JSON keys are not refused: `jsonb` keeps the last value. Without the guard, PostgreSQL's numeric input accepts NaN, Infinity, hex (`0x10`
reads as 16), underscores, exponents, surrounding whitespace, a leading plus sign and leading zeros,
and rounds excess scale; a JSON number, boolean, array or object is stringified by `->>`; and a
missing or JSON-null key yields SQL NULL, producing a NULL group or a NULL sum. The ClickHouse
decoder has no such guard, which is one reason ClickHouse is out of scope.

## Provider observation boundary

PostgreSQL and ClickHouse provider boundaries can now produce provider-owned Ed25519 observation
envelopes. Given a signed envelope, an injected verifier, and an injected UTC evaluation time, the
compiler verifies the envelope internally and satisfies provenance only when the verified observation
is fresh and matches the expected tenant, warehouse binding identifier and revision, relation
reference and namespace, engine, pinned version and image, and build digest. Raw observation
compatibility remains for existing callers, but a raw value cannot satisfy provenance.

**The observation describes the relation the statement reads.** The emitted statement reads the
generation-scoped landing relation named by the physical-plan authority, not the IIR's logical
source, which has no physical existence. Precondition 10 and the admission chain therefore require
the observation's namespace and relation name to equal the authority's source, the IIR namespace to
equal that namespace, and the observed generation column to be non-null `TEXT` under a
deterministic collation (`C`, `POSIX` or `default`) in `UTF8`, and the payload column to be non-null
`JSONB`. Other landing columns are described but not constrained. Field typing, non-null keys and
binary grouping are properties of the statement's decode guard, not of any observed column. An
earlier version of precondition 10 compared an observation of the logical relation against the IIR's
declared columns; an adversarial pre-review showed that such an observation proved nothing about the
statement, and it was replaced. The PostgreSQL observer now describes a whole landing relation --
`json` and `other` column kinds were added to the SDK -- and observes SUM semantics for the
statement's `NUMERIC(38,9)` input type. A column is reported non-null only when its not-null
constraint is validated; PostgreSQL 18 sets `attnotnull` for `NOT NULL ... NOT VALID` over existing
NULLs. Views and materialized views are refused by the PostgreSQL observer, but the observation
model carries no relation kind, and nothing re-checks the relation between observation and execution
beyond the ten-minute freshness bound. Note that `relation_ref` on an observation is an
owning-service label matched against the expected reference, while `relation_ref` on cardinality
evidence names the landing relation itself. ClickHouse has no landing observation contract, so its
precondition 10 cannot be satisfied.

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
evidence or records why none applies, cited cases carry the polarity the fixture records, the
divergence-motivated exclusions stay recorded, the declared unenforced exclusions are exactly the
negative cases PostgreSQL still returns rows for in the live fixture (currently none), and every
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
| D1 null and missing | The IIR declares group and measure inputs non-null. On PostgreSQL the decode guard enforces it: a missing or JSON-null group key or measure is refused with `22P02`, verified live, so no NULL group or NULL sum can be produced. Before the guard, the pinned engine mapped them to a NULL group and a NULL aggregate. ClickHouse maps missing or null groups to an empty string and rejects missing or null measures; it has no guard. There is no join or filter. |
| D2 precision and range | Fresh pinned-engine probes observed PostgreSQL NUMERIC(38,9) input with INTERNAL transition state, NUMERIC result, and promotion, while ClickHouse Decimal(38, 9) retained that type and wrapped on overflow. With the proposed widening, PostgreSQL's inner cast is NUMERIC(57,9), `SUM` returns unconstrained NUMERIC, and the outer NUMERIC(57,9) cast rejects values beyond its declared precision. The PostgreSQL materialization boundary takes compiler-signed Decimal57/9 output-check declarations, re-observes the committed output relation, rejects null or values at and beyond the exclusive `-10^48` and `10^48` bounds, and includes zero violations in its provider commit reference. The current answer reader verifies that same signed declaration, rechecks the locked relation, and reconstructs the commit reference before returning rows. A fresh live PostgreSQL source-to-answer transaction exercised this boundary, but compiler admission and the complete request-to-product journey remain absent. ClickHouse's inner cast is Decimal(57, 9), `sum` returns Decimal(76, 9), and its outer Decimal(57, 9) cast is nominal. A new provider boundary verifies the compiler-signed Decimal57/9 declarations and counts null or values at and beyond the exclusive bounds in one read-only query, preserving signed order and rejecting malformed or nonzero evidence. It is not integrated with a committed generation or answer reader and has no fresh live result-equivalence evidence. The pure integer proof establishes `(2^63 - 1) * (10^38 - 1) < 10^57` and `< 2^255`, so Decimal(57,9) can exactly represent the sum if an authoritative contributing-row ceiling is at most the signed-ledger maximum. No caller row count is accepted as evidence. Fresh pinned-engine fixtures agree for zero, negative, exact scale-nine, maximum positive/negative inputs, a safe two-row maximum sum, and rejection outside Decimal(38,9) precision. On PostgreSQL the decode guard refuses excess scale and every non-canonical numeric form, including NaN and Infinity, with `22P02` before any cast; without it PostgreSQL rounds excess scale where ClickHouse truncates, so that input stays inadmissible. The Decimal(57,9) result cast alone accepts NaN, verified live, which is why NaN must be refused at decode; the runtime magnitude check also flags NaN. Runtime-signed cardinality verification is implemented at the compiler boundary, but the request journey does not yet compose that authority. ClickHouse publication integration and cross-engine review remain unsatisfied. |
| D3 collation | Group strings are compared bytewise. On PostgreSQL the statement groups under an explicit `COLLATE "C"`, recorded live on the result column, so grouping does not depend on the database default (the pinned image defaults to `en_US.utf8`). ClickHouse uses `binary`/`UTF-8`. Fresh pinned-engine fixtures keep case variants, composed/decomposed Unicode, and trailing-space values in distinct groups with equivalent row sets. Result ordering is not claimed. |
| D4 locale | No parsing, formatting, case conversion, or locale-sensitive function exists. |
| D5 timezone | No timestamp expression exists. Observation timestamps and the injected evaluation time are UTC. |
| D6 identifier encoding | Schema-v2 identifiers use a lowercase ASCII allowlist and every emitted identifier is revalidated and quoted. |
| D7 literal typing | The logical shape contains no request literals. The generation-scoped emitter renders only the source contract's validated lowercase SHA-256 generation identifier; arbitrary caller SQL remains impossible. The contract requires landing fields to arrive as JSON strings, and on PostgreSQL the decode guard enforces it: JSON numbers, booleans, arrays and objects are refused with `22P02`, verified live -- `{"revenue": 12.5}` previously returned `(east, 12.500000000)`. The coverage map derives its unenforced exclusions from the live results and currently has none for PostgreSQL. ClickHouse still coerces a JSON number. |
| D8 relational behavior | One source, direct projection, one group, and one sum are present. Joins, filters, deduplication, ordering, limits, and additional measures are excluded. Fresh pinned-engine fixtures produce equivalent valid aggregates, exclude a different generation, and return no rows for an empty selected generation. The deterministic one-generation source renderer is bound into a strict physical-plan candidate, but that candidate has no admitted legality decision or execution authorization. |

## Evidence and remaining work

Positive and nullable-negative aggregate fixtures exist for both engines. Provider-owned Ed25519
observation production and compiler verification have unit coverage, including tamper, identity,
freshness, malformed-envelope, and non-sensitive failure cases; raw observations remain untrusted.
Generation-scoped JSON decoding has exact positive SQL and invalid-source unit tests for PostgreSQL
and ClickHouse. The compiler-side D1-D8 mapping and mutation regression are complete. These tests
still do not form the live provider-pair fixtures, which require the gated conformance run against
both pinned images, and they do not prove numeric result equivalence.

**Mutation regression.** The committed configuration in `services/compiler/pyproject.toml`
(`[tool.mutmut]`) mutates `generation_sql.py`, `numeric_bounds.py`, `product_compiler.py` and
`restricted_sql.py`; run `uv run mutmut run` from `services/compiler`. The run on 2026-09-16
generated 862 mutants (111, 32, 583 and 136 per module): 821 killed, 41 survived, every survivor
inspected.

*Correction.* An earlier version of this note reported a 758-mutant run "covering" all four modules.
That was wrong. mutmut reads `[tool.mutmut]` from `pyproject.toml` before `setup.cfg`, the committed
configuration then listed only `product_compiler.py` and `restricted_sql.py`, and the temporary
`setup.cfg` meant to widen it was ignored; `numeric_bounds.py` and `generation_sql.py` were never
mutated. The configuration is now committed at the stated scope, so the run is reproducible.

Widening the run found one real gap, now killed: nothing checked the proof's
`maximum_scaled_input`, so returning `None` there survived. Earlier runs killed per-condition
admission mutants in `_compose_physical_candidate`, which had been pinned only in aggregate.

The 41 survivors fall into named classes, none of which is a reachable weakening:

- Sixteen alter only the text of a refusal message.
- Four drop an explicit `strict=True` that the model configuration already requires, in
  `_revalidate_source` and `_revalidate_observation`.
- Three change the Pydantic dump mode in `_revalidate_observation`, which produces the same payload.
- One includes error input on a path no assertion reads.
- Three replace a falsy initialiser in `compile_product_iir` with another falsy value.
- Six relax a `None` or verifier guard whose relaxed path dereferences `None` inside a `try` block
  that converts the error back into the same refusal: two in `_verify_signed_cardinality_evidence`,
  one each in `_cardinality_evidence_is_bound` and `_verify_signed_observation`, and the
  `authority is None` and `provider_observation is None` guards in `_compose_physical_candidate`.
- Two flip the verified flag returned by `_verify_signed_observation` to `True` on a failure path;
  the returned observation is `None`, and precondition 16 also requires `_observation_is_bound`,
  whose first conjunct is `observation is not None`.
- One flips the `prove_decimal_sum_bound` failure path in `_cardinality_evidence_is_bound` to
  `True`; the signed evidence model constrains the row ceiling and the signer revalidates, so an
  out-of-range ceiling cannot reach it.
- One relaxes the post-check guard in `require_project_sum_shape`; the shape checks already refuse
  a missing projection or aggregate.
- Three mutate the Decimal(57,9) re-check in `prove_decimal_sum_bound`. The row ceiling is range
  checked first, and `(2^63 - 1)(10^38 - 1) < 10^57 < 2^255`, so no reachable ceiling makes either
  comparison true.
- One relaxes the null and empty-group conjunction in `_sum_semantics_match`, which is unreachable
  for the reason given next.

All 44 mutants of `_landing_relation_matches` were killed. In the PostgreSQL decode guard,
`_decode_postgresql_binding`, 11 of 14 were killed and the three survivors alter only its
unsupported-binding message. mutmut does not mutate module-level constants, so the canonical decimal
pattern `_POSTGRESQL_CANONICAL_DECIMAL` is not covered by this run; it is pinned instead by
parametrised accept and refuse tests in `test_generation_scoped_product_sql.py`, by the rule record,
and by the live evidence, which refuses every permissive numeric form on the pinned engine.

**The null and empty-group SUM semantics are carried by the observation type, not by the compiler
comparison.** `ProductSqlSumSemantics` declares `null_input_behavior: Literal["exclude"]` and
`empty_group_behavior: Literal["no_row"]`, each single-valued, so strict revalidation rejects any
other value at precondition 7 before precondition 11 runs, and that part of `_sum_semantics_match`
is unreachable. `test_null_and_empty_group_semantics_are_enforced_by_the_observation_type` pins where
the refusal actually happens. `test_physical_decimal_and_sum_overflow_semantics_fail_closed` now uses
a valid but wrong overflow behaviour (`wrap`), so precondition 11 itself is the gate that refuses it.
D1 null handling for landing values does not rest on either; it rests on the decode guard.

An earlier mutation run also exposed a refusal-attribution defect. The first shape check asserted
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
maximum-magnitude Decimal(38,9) inputs sum past Decimal(38,9) and return exactly; the measure column
is declared `numeric(57,9)` and the group column carries collation `C`; seventeen malformed landing
values, including NaN, hex, excess scale, JSON numbers and null or missing keys, are refused by the
decode guard with `22P02`; finite values beyond Decimal(57,9) are refused by the result cast with
`22003`; the result cast alone accepts NaN; and the runtime magnitude predicate flags NaN and 10^48.
The result bound is observed on the exact cast the statement applies, not reached through a real
SUM; the integer proof above is what shows a SUM bounded by the signed ledger ceiling cannot reach
it. Preconditions 15 and 17, both worded per engine, stay unsatisfied until the independent reviewer
decides them.

**Live compiled journey on the pinned engine.**
`tests/integration/test_postgresql_compiled_product_journey_live.py` runs on the pinned PostgreSQL
18.6 image with every compiler input produced by its owning service: rows are acquired and landed
through the real providers and generation ledger, the landing relation is observed and signed by
the provider observer, the physical plan is composed from a generation authority built on the
committed LAND receipt, and input cardinality is resolved from the ledger and signed by the runtime.
The compiler leaves exactly preconditions 15, 17 and 18 unsatisfied; removing the signed cardinality
makes precondition 14 fail. The compiler's guarded statement then materializes through dbt, the
runtime magnitude check runs before publication, and the product reconciles to the inserted source
rows. Nothing is admitted: the execution authorization uses a legality decision digest derived from
the compiler's refusal, so the run proves the check executes, not that it is bound to an approval.
The older native journeys that start PostgreSQL from local binaries ran on PostgreSQL 14.17 when last
exercised for this note and are not evidence about the pinned engine.

PostgreSQL activation additionally requires composing the runtime-signed cardinality artifact into
the request journey, an execution authorization bound to the exact candidate, a per-engine
activation record, and approval by an independent reviewer. ClickHouse activation separately
requires a landing decode guard and observation contract, ClickHouse materialization and answer
integration for its magnitude observer, its own live D1-D8 fixtures and checked SUM evidence, and its
own review.
