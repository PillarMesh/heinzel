# Live evidence: PRODUCT-SQL-V2-PROJECT-SUM-001 on PostgreSQL

- Status: evidence for review. This document records observations; it approves nothing.
- Rule: `PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE`
- **Activation scope: `postgresql` only.** Cross-engine equivalence is not claimed, and nothing
  here is evidence about ClickHouse.
- Serves: precondition 17 as reworded for single-engine activation. The precondition stays
  unsatisfied until the independent reviewer accepts this evidence (precondition 18).
- Machine-readable bundle: `fixtures/postgresql-live-checked-sum-evidence.json`
- Harness: `tests/integration/test_postgresql_checked_sum_evidence.py`

## Engine

| Property | Observed |
| --- | --- |
| Engine version | `18.6` |
| Image | `postgres:18.6-bookworm` |
| Image digest | `sha256:33c86c9cfb790e257e470b29e8c97bd1bd6fee0a70ab2d7a2e377ab639c09935` |
| Build digest | `37b575bed7f1f55dc90308c50dc9ec6d4d8a9362d1c3d9d4643501d8c8274bab` |
| Database encoding | `UTF8` |
| SUM input / accumulator / result | `NUMERIC(38,9)` / `INTERNAL` / `NUMERIC` |
| SUM overflow / null input / empty group | `promote` / `exclude` / `no_row` |
| Declared type of `total_revenue` | `numeric(57,9)` |

The build digest and SUM semantics were read with the PostgreSQL provider's own observers
(`_read_context` and `_observe_sum_semantics`), so they are what the compiler's provenance path
would record for this engine.

## Statement

Every statement case ran this exact statement, digest `b6e557d525b10edc3d7615483723d6938e52159bc597506e59a34b724dcf85ab`. An offline test
fails if the compiler's emitted statement ever differs from it, so this evidence cannot silently
outlive a change to the emitter.

```sql
SELECT "revenue_events"."region" AS "region", CAST(SUM(CAST("revenue_events"."revenue" AS NUMERIC(57,9))) AS NUMERIC(57,9)) AS "total_revenue" FROM (SELECT "payload" ->> 'region' AS "region", CAST("payload" ->> 'revenue' AS NUMERIC(38,9)) AS "revenue" FROM "raw"."raw_sales" WHERE "generation_id" = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa') AS "revenue_events" GROUP BY "revenue_events"."region"
```

## Statement cases

Landing rows were inserted into `raw.raw_sales` with the revenue value as a JSON string, then the
statement above ran for the selected generation `a…`. Generation `d…` is the other generation.

| Case | Input | PostgreSQL returned | Result digest |
| --- | --- | --- | --- |
| `zero` | gen `a…` `0` | `g` → `0.000000000` | `c869ae1bbaaf442a` |
| `negative` | gen `a…` `-42.125000000` | `g` → `-42.125000000` | `ac78bfbcd7d7d145` |
| `exact_scale_nine` | gen `a…` `0.123456789` | `g` → `0.123456789` | `cdd914f9ebd333cb` |
| `maximum_positive_input` | gen `a…` `99999999999999999999999999999.999999999` | `g` → `99999999999999999999999999999.999999999` | `b0b8096d0341854d` |
| `maximum_negative_input` | gen `a…` `-99999999999999999999999999999.999999999` | `g` → `-99999999999999999999999999999.999999999` | `3d596fb286d994a5` |
| `safe_two_row_maximum_sum` | gen `a…` `99999999999999999999999999999.999999999`; gen `a…` `99999999999999999999999999999.999999999` | `g` → `199999999999999999999999999999.999999998` | `14a6c4e96b1e34df` |
| `safe_two_row_minimum_sum` | gen `a…` `-99999999999999999999999999999.999999999`; gen `a…` `-99999999999999999999999999999.999999999` | `g` → `-199999999999999999999999999999.999999998` | `c19b75cff3e9ba1b` |
| `input_at_exclusive_upper_bound` | gen `a…` `100000000000000000000000000000` | refused, SQLSTATE `22003` | `f9f805730adff70f` |
| `input_at_exclusive_lower_bound` | gen `a…` `-100000000000000000000000000000` | refused, SQLSTATE `22003` | `f9f805730adff70f` |
| `empty_group` | gen `d…` `999` | no rows | `57926c1b92cc4297` |
| `excluded_generation` | gen `a…` `1`; gen `d…` `999` | `g` → `1.000000000` | `34811dcef1714644` |

## Result-cast probes

The statement applies `CAST(… AS NUMERIC(57,9))` to its SUM. Driving the SUM itself to 10^48 would
take on the order of 10^19 maximum-valued input rows, so the result bound is observed on that exact
cast directly rather than through the statement.

| Case | Input | PostgreSQL returned | Result digest |
| --- | --- | --- | --- |
| `result_cast_at_maximum` | `999999999999999999999999999999999999999999999999.999999999` | accepted `999999999999999999999999999999999999999999999999.999999999` | `26f15306cade5db7` |
| `result_cast_at_minimum` | `-999999999999999999999999999999999999999999999999.999999999` | accepted `-999999999999999999999999999999999999999999999999.999999999` | `c9d86e82a0a05f8d` |
| `result_cast_at_exclusive_upper_bound` | `1000000000000000000000000000000000000000000000000` | refused, SQLSTATE `22003` | `b3d59dc68c907a8b` |
| `result_cast_at_exclusive_lower_bound` | `-1000000000000000000000000000000000000000000000000` | refused, SQLSTATE `22003` | `b3d59dc68c907a8b` |

## What this shows, and what it does not

- The widening argument holds on the real engine. Two maximum-magnitude Decimal(38,9) inputs sum
  to a value that no longer fits Decimal(38,9) and PostgreSQL returns it exactly, and the measure
  column is declared `numeric(57,9)`.
- Both bounds fail closed. An input outside Decimal(38,9) refuses the whole statement, and a value
  outside Decimal(57,9) refuses the result cast, each with SQLSTATE `22003`
  (`numeric_value_out_of_range`). Neither case rounds, truncates, or wraps.
- An empty selected generation yields no group rather than a zero-valued group, and rows from
  another generation never contribute.
- The Decimal(57,9) result bound is observed on the cast, not reached through a real SUM. The
  integer proof in the proof note is what establishes that a SUM bounded by the signed ledger row
  ceiling cannot reach it.
- This does not cover D7. A JSON number where the contract requires a string is still accepted;
  see the D7 row of the proof note.
- This is PostgreSQL evidence only. It says nothing about ClickHouse, which truncates where
  PostgreSQL rounds and needs its own evidence and review.

## Reproducing

```sh
PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE=1 uv run pytest tests/integration/test_postgresql_checked_sum_evidence.py -m live
```

The run starts the pinned image, captures every case, and fails if any result differs from the
bundle. Adding `PILLARMESH_WRITE_CHECKED_SUM_EVIDENCE=1` rewrites the bundle from the run instead.
