# Legality rules

The compiler emits a plan only when a legality rule admits it. A rule declares numbered
preconditions; the compiler evaluates each one against the contract or product intent and the
current provider observations. If every precondition is satisfied, the plan is admitted. If any is
not, compilation returns `No Valid Plan`, which lists every precondition by number with its status
and reason, and the smallest changes that would satisfy the unsatisfied ones.

Rules are sound but deliberately incomplete: a rule may refuse work that would in fact be safe, but
it must never admit work that is not.

## Layout

| Path | Contents |
| --- | --- |
| `rules/SNAPSHOT-POSTGRESQL-SNOWFLAKE-001.json` | The PostgreSQL-to-Snowflake snapshot rule. |
| `fixtures/positive.json`, `fixtures/negative.json` | Snapshot rule inputs and expected decisions. Each negative case names the precondition numbers it must fail. |
| `product-sql/rules/PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE.json` | The candidate restricted product SQL rule (project, then sum one decimal column by string keys). |
| `product-sql/fixtures/` | Per-engine positive and negative fixtures, the D1-D8 equivalence coverage map (`d1-d8-coverage.json`), generation-scoped conformance cases, and recorded live checked-SUM evidence. |

## Rule file fields

The two rule files have different fields.

The snapshot rule has:

- `rule_id` and `version`: the rule's identity.
- `preconditions`: the precondition numbers the rule evaluates.
- `soundness` and `completeness`: soundness is required; completeness is not claimed.

The candidate product SQL rule has no `preconditions`, `soundness` or `completeness` fields. It has:

- `rule_id` and `rule_version`: the rule's identity.
- `constructs` and `excluded_constructs`: the SQL shape it covers and what it leaves out.
- `engine_profiles` and `decimal_sum_bound`: the pinned per-engine semantics it relies on.
- `physical_source_requirements`: what the landing source must provide.
- `review_status`: the outcome of the rule's most recent review, currently `changes_requested`.
- `gate_preconditions`: the preconditions that stand for review and evidence gates rather than
  checks on the input, mapped by name to their number.
- `unsatisfied_gates`: a record of which gates the compiler currently treats as unsatisfied.

## Snapshot rule preconditions

`SNAPSHOT-POSTGRESQL-SNOWFLAKE-001` evaluates preconditions 1 to 10
(`src/heinzel_compiler/legality.py`):

| # | Requirement |
| --- | --- |
| 1 | The source is a read-only PostgreSQL base table on the contract's connection. |
| 2 | The source key is a non-null `BIGINT` primary key with a stable total order. |
| 3 | Source and destination columns match the fixed snapshot mapping. |
| 4 | The projection is the fixed projection and destination names are unique. |
| 5 | The Snowflake target and its commit ledger exactly match the snapshot contract. |
| 6 | The destination key preserves the source key. |
| 7 | Snapshot, commit and deletion semantics exactly match the contract. |
| 8 | Both provider observations are no older than ten minutes. |
| 9 | Both providers declare every required snapshot capability. |
| 10 | The required evidence can be produced without secrets or row values. |

## Candidate product SQL rule preconditions

`PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE` evaluates preconditions 1 to 18
(`src/heinzel_compiler/product_compiler.py` and `src/heinzel_compiler/restricted_sql.py`):

| # | Requirement |
| --- | --- |
| 1 | Exactly one direct projection followed by one aggregate. |
| 2 | The projection carries only the aggregate's direct declared source inputs. |
| 3 | The aggregate's group equals the product grain. |
| 4 | Exactly one non-null decimal source column is summed. |
| 5 | Grouping uses only non-null string columns under binary collation. |
| 6 | Grouped and measure output names are distinct. |
| 7 | The provider observation is bound to the expected tenant, warehouse, relation and digest. |
| 8 | The pinned engine version, image and build are bound to the observation. |
| 9 | The observation is no older than ten minutes. |
| 10 | The landing relation has the non-null text generation and JSONB payload columns the statement reads. Only PostgreSQL has a landing observation contract, so this can never be satisfied on ClickHouse. |
| 11 | The engine's observed `SUM` semantics match the pinned engine profile. |
| 12 | `NUMERIC(38,9)` sums are proven to fit exact `Decimal(57,9)` arithmetic. |

Preconditions 13 to 18 are the rule's `gate_preconditions`:

| # | Gate | Requirement |
| --- | --- | --- |
| 13 | `authoritative_generation_addressing` | The physical source is bound to exactly one authoritative generation and landing-receipt digest. |
| 14 | `authority_bound_cardinality` | The contributing-row ceiling is bound to cardinality evidence from the owning service. |
| 15 | `runtime_result_magnitude_enforcement` | The runtime enforces the checked `Decimal(57,9)` result magnitude on the engine. |
| 16 | `provider_owned_provenance` | The observation is authenticated by provider-owned provenance authority. |
| 17 | `live_checked_sum_review` | The live checked-SUM run on the pinned engine has been reviewed. For PostgreSQL this withholds any cross-engine equivalence claim; ClickHouse needs its own activation. |
| 18 | `independent_review` | An independent reviewer has approved the rule. |

The compiler hard-codes preconditions 15, 17 and 18 as unsatisfied
(`src/heinzel_compiler/product_compiler.py`). `unsatisfied_gates` in the rule file records that
state, and a test keeps the two in step. The compiler therefore cannot admit a product plan today:
every product SQL compilation, on either engine, returns `No Valid Plan`.

## Changing a rule

Adding or widening a rule needs, in the same change, a proof of the property it relies on,
positive and negative fixtures for each engine it covers, a test that each precondition can fail,
and approval by a reviewer other than its author. See [AGENTS.md](../../../AGENTS.md).

Rule changes are reviewed on public pull requests.
