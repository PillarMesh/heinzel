# Live evidence: PRODUCT-SQL-V2-PROJECT-SUM-001 on PostgreSQL

- Status: evidence for review. This document records observations; it approves nothing.
- Rule: `PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE`
- **Activation scope: `postgresql` only.** Cross-engine equivalence is not claimed, and nothing
  here is evidence about ClickHouse.
- Serves: precondition 17 as worded for single-engine activation. The precondition stays
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
| Declared type and collation of `region` | `text`, collation `C` |

What the identity fields do and do not bind:

- **Image digest** is the digest of the pinned image reference the harness started. A running
  server cannot report its own image digest, so this is the reference that was launched, checked
  equal to both the provider's and the compiler's pinned digest; it is not read from the server.
  The reference is a multi-architecture manifest, so it does not identify whether an amd64 or arm64
  binary ran.
- **Build digest** is derived from `server_version_num` and `server_version` as the server reports
  them. It binds the version string, not the binary, and carries no architecture. Every capture of
  this bundle ran the arm64 variant of the image, and nothing in the recorded digests says so.
- The build digest and SUM semantics were read with the PostgreSQL provider's own observers
  (`_read_context` and `_observe_sum_semantics`), so they are what the compiler's provenance path
  records for this engine.

## Statement

Every statement case ran this exact statement, digest `8e01c7af598220f012014bbf6d04ca9e02eeccbdc0221a912cfefe13f4f2d543`. An offline test
fails if the compiler's emitted statement ever differs from it, so this evidence cannot silently
outlive a change to the emitter.

```sql
SELECT "revenue_events"."region" AS "region", CAST(pg_catalog.sum(CAST("revenue_events"."revenue" AS NUMERIC(57,9))) AS NUMERIC(57,9)) AS "total_revenue" FROM (SELECT (CASE WHEN pg_catalog.jsonb_typeof("payload" OPERATOR(pg_catalog.->) 'region') OPERATOR(pg_catalog.=) 'string' THEN "payload" OPERATOR(pg_catalog.->>) 'region' ELSE CAST(CAST('pillarmesh refused a string landing value in generation ' OPERATOR(pg_catalog.||) "generation_id" AS NUMERIC) AS pg_catalog.text) END) COLLATE pg_catalog."C" AS "region", CAST(CASE WHEN pg_catalog.jsonb_typeof("payload" OPERATOR(pg_catalog.->) 'revenue') OPERATOR(pg_catalog.=) 'string' AND "payload" OPERATOR(pg_catalog.->>) 'revenue' OPERATOR(pg_catalog.~) '^-?(0|[1-9][0-9]{0,28})([.][0-9]{1,9})?$' THEN "payload" OPERATOR(pg_catalog.->>) 'revenue' ELSE 'pillarmesh refused a decimal landing value in generation ' OPERATOR(pg_catalog.||) "generation_id" END AS NUMERIC(38,9)) AS "revenue" FROM "raw"."raw_sales" WHERE "generation_id" OPERATOR(pg_catalog.=) 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa') AS "revenue_events" GROUP BY "revenue_events"."region"
```

The inner select is the landing decode guard. Each field must be present as a JSON string; the
decimal field must also match the restricted decimal literal
`-?(0|[1-9][0-9]{0,28})([.][0-9]{1,9})?`. That pattern admits only finite Decimal(38,9)
values, but it is not a canonical form: `0`, `-0` and `0.0` are all admitted and denote the same
value. Anything else takes the refusal branch, which casts a message naming the row's generation to
`NUMERIC` and raises SQLSTATE `22P02`; the payload value itself does not appear in the message. The
region is grouped under an explicit `COLLATE pg_catalog."C"`.

Every function, operator, type and collation in the statement is qualified to `pg_catalog`, so the
statement resolves nothing through the session's `search_path`.

## Statement cases

Landing rows were inserted into `raw.raw_sales` with the JSON payload shown, then the statement
ran for the selected generation `a…`. Generation `d…` is another generation.

| Case | Input | PostgreSQL returned | Result digest |
| --- | --- | --- | --- |
| `zero` | gen `a…` `{"region": "g", "revenue": "0"}` | `g` → `0.000000000` | `c869ae1bbaaf442a` |
| `negative` | gen `a…` `{"region": "g", "revenue": "-42.125000000"}` | `g` → `-42.125000000` | `ac78bfbcd7d7d145` |
| `exact_scale_nine` | gen `a…` `{"region": "g", "revenue": "0.123456789"}` | `g` → `0.123456789` | `cdd914f9ebd333cb` |
| `maximum_positive_input` | gen `a…` `{"region": "g", "revenue": "99999999999999999999999999999.999999999"}` | `g` → `99999999999999999999999999999.999999999` | `b0b8096d0341854d` |
| `maximum_negative_input` | gen `a…` `{"region": "g", "revenue": "-99999999999999999999999999999.999999999"}` | `g` → `-99999999999999999999999999999.999999999` | `3d596fb286d994a5` |
| `safe_two_row_maximum_sum` | gen `a…` `{"region": "g", "revenue": "99999999999999999999999999999.999999999"}`<br>gen `a…` `{"region": "g", "revenue": "99999999999999999999999999999.999999999"}` | `g` → `199999999999999999999999999999.999999998` | `14a6c4e96b1e34df` |
| `safe_two_row_minimum_sum` | gen `a…` `{"region": "g", "revenue": "-99999999999999999999999999999.999999999"}`<br>gen `a…` `{"region": "g", "revenue": "-99999999999999999999999999999.999999999"}` | `g` → `-199999999999999999999999999999.999999998` | `c19b75cff3e9ba1b` |
| `input_at_exclusive_upper_bound` | gen `a…` `{"region": "g", "revenue": "100000000000000000000000000000"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `input_at_exclusive_lower_bound` | gen `a…` `{"region": "g", "revenue": "-100000000000000000000000000000"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `empty_group` | gen `d…` `{"region": "g", "revenue": "999"}` | no rows | `57926c1b92cc4297` |
| `excluded_generation` | gen `a…` `{"region": "g", "revenue": "1"}`<br>gen `d…` `{"region": "g", "revenue": "999"}` | `g` → `1.000000000` | `34811dcef1714644` |
| `invalid_rows_in_another_generation_do_not_refuse` | gen `a…` `{"region": "g", "revenue": "1"}`<br>gen `d…` `{"region": null, "revenue": "NaN"}` | `g` → `1.000000000` | `34811dcef1714644` |
| `nan_measure` | gen `a…` `{"region": "g", "revenue": "NaN"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `infinity_measure` | gen `a…` `{"region": "g", "revenue": "Infinity"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `hex_measure` | gen `a…` `{"region": "g", "revenue": "0x10"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `underscore_measure` | gen `a…` `{"region": "g", "revenue": "1_000"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `exponent_measure` | gen `a…` `{"region": "g", "revenue": "1e3"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `whitespace_measure` | gen `a…` `{"region": "g", "revenue": " 7 "}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `plus_sign_measure` | gen `a…` `{"region": "g", "revenue": "+5"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `leading_zero_measure` | gen `a…` `{"region": "g", "revenue": "007"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `excess_scale_measure` | gen `a…` `{"region": "g", "revenue": "1.1234567895"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `json_number_measure` | gen `a…` `{"region": "g", "revenue": 12.5}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `null_measure` | gen `a…` `{"region": "g", "revenue": null}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `missing_measure` | gen `a…` `{"region": "g"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `null_group` | gen `a…` `{"region": null, "revenue": "1"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `missing_group` | gen `a…` `{"revenue": "1"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `non_string_group` | gen `a…` `{"region": 1, "revenue": "1"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `trailing_newline_measure` | gen `a…` `{"region": "g", "revenue": "1\n"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `arabic_indic_digit_measure` | gen `a…` `{"region": "g", "revenue": "\u0663"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `fullwidth_digit_measure` | gen `a…` `{"region": "g", "revenue": "\uff11"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `binary_collation_groups` | gen `a…` `{"region": "b", "revenue": "1"}`<br>gen `a…` `{"region": "A", "revenue": "1"}`<br>gen `a…` `{"region": "a", "revenue": "1"}`<br>gen `a…` `{"region": "B", "revenue": "1"}` | `A` → `1.000000000`, `B` → `1.000000000`, `a` → `1.000000000`, `b` → `1.000000000` | `a31462c4758d2b85` |

## Hostile search_path cases

These cases ran the same statement in a session with `search_path = shadow, pg_catalog`, where the
`shadow` schema defines `jsonb_typeof`, `sum`, and the `~`, `=`, `->>` and `||` operators that
accept anything or return constants. Before the statement was schema-qualified, such a session let
`0x10` through as 16.

| Case | Input | PostgreSQL returned | Result digest |
| --- | --- | --- | --- |
| `shadowed_search_path_valid_rows` | gen `a…` `{"region": "east", "revenue": "10.25"}`<br>gen `a…` `{"region": "east", "revenue": "2.75"}` | `east` → `13.000000000` | `1fb38c373ef3412d` |
| `shadowed_search_path_hex_measure` | gen `a…` `{"region": "g", "revenue": "0x10"}` | refused, SQLSTATE `22P02` | `293d899cea251731` |
| `shadowed_search_path_other_generation` | gen `a…` `{"region": "g", "revenue": "1"}`<br>gen `d…` `{"region": "g", "revenue": "999"}` | `g` → `1.000000000` | `34811dcef1714644` |

## Result-cast probes

The statement applies `CAST(… AS NUMERIC(57,9))` to its SUM. Driving the SUM itself to 10^48
would take on the order of 10^19 maximum-valued input rows, so the result bound is observed on
that exact cast directly rather than through the statement.

| Case | Input | PostgreSQL returned | Result digest |
| --- | --- | --- | --- |
| `result_cast_at_maximum` | `999999999999999999999999999999999999999999999999.999999999` | accepted `999999999999999999999999999999999999999999999999.999999999` | `26f15306cade5db7` |
| `result_cast_at_minimum` | `-999999999999999999999999999999999999999999999999.999999999` | accepted `-999999999999999999999999999999999999999999999999.999999999` | `c9d86e82a0a05f8d` |
| `result_cast_at_exclusive_upper_bound` | `1000000000000000000000000000000000000000000000000` | refused, SQLSTATE `22003` | `b3d59dc68c907a8b` |
| `result_cast_at_exclusive_lower_bound` | `-1000000000000000000000000000000000000000000000000` | refused, SQLSTATE `22003` | `b3d59dc68c907a8b` |
| `result_cast_of_nan` | `NaN` | accepted `NaN` | `6f12123f2f3a22ea` |

## Runtime magnitude probes

After materialization, the PostgreSQL provider counts violations with
`value IS NULL OR value <= -10^48 OR value >= 10^48`. These probes evaluate that predicate, with
the provider's own bound constant, on the pinned engine.

| Case | Input | PostgreSQL returned | Result digest |
| --- | --- | --- | --- |
| `runtime_magnitude_accepts_maximum` | `999999999999999999999999999999999999999999999999.999999999` | not flagged | `6b82185560616cde` |
| `runtime_magnitude_flags_exclusive_bound` | `1000000000000000000000000000000000000000000000000` | flagged as a violation | `4f62b3c19e805db9` |
| `runtime_magnitude_flags_nan` | `NaN` | flagged as a violation | `4f62b3c19e805db9` |

## What this shows, and what it does not

- The widening argument holds on the real engine. Two maximum-magnitude Decimal(38,9) inputs sum
  to a value that no longer fits Decimal(38,9) and PostgreSQL returns it exactly, and the measure
  column is declared `numeric(57,9)`.
- Malformed landing input never reaches a cast. NaN, Infinity, hex, underscores, exponents,
  whitespace, a trailing newline, non-ASCII digits, a plus sign, leading zeros, excess scale, JSON
  numbers, and null or missing keys are all refused by the decode guard with `22P02`.
- The guard does not depend on session state: a hostile `search_path` changes no result.
- Group keys are non-null strings and group bytewise under `C`. **The sort order of the region
  column is now bytewise** (`A`, `B`, `a`, `b`) rather than the database default's order; group
  membership is unchanged. A consumer that orders by the grain column sees a different order.
- Finite values outside Decimal(57,9) are refused by the result cast with `22003`. **The result
  cast alone accepts NaN**; NaN is kept out by the input guard, and the runtime magnitude check
  also flags NaN because PostgreSQL orders NaN above every number.
- A malformed row in another generation neither contributes nor refuses the selected generation.
- **Duplicate JSON keys are not refused.** `jsonb` keeps the last value of a duplicated key, so
  `{"revenue":"NaN","revenue":"1"}` decodes as 1. Whether landing rejects duplicate keys, and
  whether the landing receipt digest covers the raw text, is outside this evidence.
- The Decimal(57,9) result bound is observed on the cast, not reached through a real SUM. The
  integer proof in the proof note is what establishes that a SUM bounded by the signed ledger row
  ceiling cannot reach it.
- This is PostgreSQL evidence only. It says nothing about ClickHouse, which has no decode guard,
  truncates where PostgreSQL rounds, and needs its own evidence and review.

## Reproducing

```sh
PILLARMESH_RUN_PRODUCT_SQL_CONFORMANCE=1 uv run pytest tests/integration/test_postgresql_checked_sum_evidence.py -m live
```

The run starts the pinned image, captures every case, and fails if any result differs from the
bundle. Adding `PILLARMESH_WRITE_CHECKED_SUM_EVIDENCE=1` rewrites the bundle from the run instead.
