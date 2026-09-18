# Proof Note: SNAPSHOT-POSTGRESQL-SNOWFLAKE-001

- Status: Approved by independent review of `ab41c3418135d833f897a2c5273de4a093ead227` on 2026-08-13
- Rule version: 1
- Scope: the exact fixture mapping in the M0 design section 4.2
- Prior independent dispositions: `changes_required` for
  `317d2424530c333483b55fbb85edf62417724377` and
  `8f440d1d222d25436a734d1e5d0c0b7889cef9b3` and
  `48b400d9918a0335e0249abf9b3f9af7b6f9e303`
- Independent review record:
  `../reviews/SNAPSHOT-POSTGRESQL-SNOWFLAKE-001-review.md` (round-3 commit, reviewer identity,
  re-review date, rulings, and approved disposition)

The rule admits only observations resolved through the contract's declared opaque handles, a
bounded PostgreSQL base-table snapshot accessed by a credential with effective read but no effective
table/column write privilege on that source relation, and ordered by a declared single-column,
non-null btree primary key. It admits the fixed six-column projection only when both PostgreSQL and
Snowflake use `VARCHAR(65535)` for customer reference/status, the provider row model enforces the
same per-value bound, and every other physical type matches exactly. The Snowflake target and commit
ledger must be observed base tables with exact columns, nullability, and key declarations. Deletion
is explicitly not observed. The required evidence set contains metadata and digests only.

The text mapping is lossless within the admitted domain: both databases constrain the same two
values to at most 65,535 characters, Pydantic rejects a longer provider row before encoding, and the
Snowflake staging projection casts to the same bound. Unconstrained PostgreSQL `TEXT`, a narrower or
wider destination declaration, and a value above the shared bound are outside the rule.

Soundness depends on all ten preconditions being satisfied together. Every required provider fact
is explicit: absence is represented as Unknown rather than replaced by an optimistic default. An
Unsatisfied or Unknown result produces `No Valid Plan`. Self-contained JSON fixtures execute the
positive provider pair and every negative, including write-capable/unknown source access and
view/unknown destination or ledger kinds.

Verification establishes one coherent time boundary by observing both providers first and then
capturing the evaluation timestamp used by legality and graph compilation. A deterministic
advancing-clock regression proves normal same-process observations completed immediately before
that timestamp are not rejected as future. The independent future/stale legality tests still reject
timestamps outside that ordered boundary.

The clean focused local mutation campaign on 2026-08-13 generated 383 mutations and killed all 383.
Every numbered precondition maps to an admission-capable removed fact or disjunction and a direct
killing test. There are no survivors requiring equivalence or acceptance rulings.

This proof remains bounded by the following limitations:

- It proves source-handle read-only behavior for the resolved source relation by effective SELECT
  plus table- and column-level write privileges. It does not claim that the PostgreSQL principal is
  globally unable to write unrelated databases or objects; environment setup separately requires
  the dedicated role to lack create and unrelated-schema access.
- It does not cover nullable selected values, blank-padded character types, any text bound other
  than 65,535 characters, alternative timestamp/numeric precision, CDC, deletes, multiple segments,
  additional provider pairs, or runtime migration.
- Snowflake primary-key declarations on standard tables are informational rather than enforced.
  Admission proves the exact declaration and schema, not database-enforced uniqueness. The M0
  runtime and contract service assume one active run and one worker for this contract; concurrent
  writers or workers are not proven.
- Merge-plus-ledger atomicity is established offline by transaction-boundary and ambiguity-recovery
  tests. It is not a live Snowflake transaction witness; that remains part of the separately owned
  live acceptance run.
- A post-commit evidence-store write outage leaves the run nonterminal at its last durable
  checkpoint. The failed evidence write cannot itself be recorded while that store is unavailable;
  a later resume re-resolves the ledger/visibility state, writes the missing evidence, and must not
  repeat the destination mutation.
- Provider observations are point-in-time metadata bounded to ten minutes against one evaluation
  timestamp captured after both observation calls, and revalidated by the existing
  activation/runtime drift boundary; the rule does not lock provider DDL or grants for the lifetime
  of a run.

This note was not self-approved. Independent reviewer `/root/acceptance_task6_review` confirmed the
shared text domain, effective source privilege observation, target/ledger object-kind observation,
exact key and schema facts, coherent observation/evaluation time boundary, executable fixtures,
mutation evidence, and these limitations against exact commit
`ab41c3418135d833f897a2c5273de4a093ead227` on 2026-08-13.
