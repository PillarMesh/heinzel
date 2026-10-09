# Delivery plan for the open demonstration gaps

A sequence for the rows [demonstration-gaps.md](demonstration-gaps.md) leaves open. It plans
from what the running system reports about itself, not from the register's prose: the register
has drifted twice in one sitting, and both drifts were rows claiming something was missing that
had since been built.

This is a programme, not a backlog to burn down in order. Read it as four questions — what is a
decision rather than work, what unblocks the most, what the critical path is, and what should
not be built at all.

## The state it plans from

Taken from the live console on 2026-10-09, not from documentation.

| Reported by | Reading |
| --- | --- |
| Capability register (`GET /api/v1/workspace`) | 16 capabilities, 8 `ready`, 8 `not_delivered` |
| Setup surface (`GET /api/v1/setup`) | 7 stages, 1 complete, 6 blocked |
| Gap register | 41 rows, 10 closed, 31 open |
| Warehouse (`psql` against the provisioned instance) | 5 least-privilege roles, 5 landed rows, 1 landing receipt, a built product of 3 rows |

One lane works end to end and is attested at every step. What is missing is breadth and
operability, which is a different problem from correctness and wants different work.

## Rows that are decisions, not work

Three rows cannot be closed by building the thing they describe, because the thing they describe
was chosen. Each wants a written decision, and two of them want an ADR amendment rather than a
commit.

| Row | Why it is not work |
| --- | --- |
| Opt-in path answers only in the start that provisioned the warehouse | Marked `Unbuilt, deliberately`. Credentials are minted per start and written nowhere. Closing it means persisting a credential, which is the opposite of the property it protects. The work is **secret custody** (below), after which the row closes as a consequence. |
| Query estimator returns no estimate | `by design`: PostgreSQL's planner rows and width cannot conservatively bound bytes scanned. The scan bound already comes from the relation's measured size, which is the conservative answer. Either accept it as closed on PostgreSQL, or scope it to an engine whose planner can. |
| Workspace summarises `active` while the warehouse capability is `not_delivered` | Marked deliberate: routing to a setup surface that answers 503 would name work the deployment cannot offer. Revisit only when the setup surface stops refusing. |

One further row is **stale**: the dashboard-publication row says publishing "stays unwired" and
the offering "can publish to nothing". The capability register reports `Dashboard publication:
ready`, and the recorded journey publishes a dashboard that Superset then draws. It is accurate
only for a deployment given no Superset. Correct the row before planning against it.

**So: 31 open rows, 1 stale, 3 decisions, 27 to build.**

## Three root causes under most of the 27

Closing these does not close rows directly. It is what makes the rows closable.

### A. No persistent secret custody

Role passwords are minted fresh each start and written nowhere, and enrolment is immutable per
handle, so a handle enrolled on one start is refused on the next. This is why the demonstration
enrols no source connection, which is why it offers nothing to register, which is why it
acquires under a source binding it builds by hand rather than one a broker registered.

Blocks: source enrolment, source registration, broker-registered acquisition, warehouse
resumption across restarts.

### B. Doubles where services belong

The entitlement authority and the catalog provider are demonstration doubles, and dbt models
materialize without compiler admission or catalog publication. The catalog capability is
`not_delivered` as a direct consequence.

Blocks: managed catalog, catalog asset preview, product publication, meaning review.

### C. No identity

An actor is a request header naming one of two fixed identities. Everything that depends on
*who* someone is — standing authority at admission, Superset SSO, MCP authority adapters — is
unreachable until this exists.

Blocks: fulfillment admission, SSO, the MCP interface, and any claim about least privilege that
rests on a person rather than a role.

## Phases

Sequenced so each phase is shippable and each unblocks the next. Sizes are engineer-weeks and
are estimates, not commitments; the ones marked **?** have genuine unknowns and want a spike
first.

### Phase 1 — Foundations (unblocks the most per unit of work)

| Work | Closes or unblocks | Size |
| --- | --- | --- |
| Secret custody service: durable, rotatable, never on the state volume | Root A; closes the warehouse-resumption row as a consequence | 3–4 |
| Authentication and a workspace principal directory | Root C; closes *no authentication* | 4–6 **?** |

Ship criterion: a restart adopts its warehouse and answers; two real identities sign in and the
console refuses one of them something.

### Phase 2 — The operator surface

The six blocked setup stages are the largest visible hole: the console looks like a product with
nine destinations and six are closed doors.

| Work | Closes | Size |
| --- | --- | --- |
| Source enrolment and registration end to end, broker-backed | 3 rows (registration wiring, enrolment, hand-built binding) | 3–4 |
| Warehouse engine options: real regions and capacity profiles | 1 row | 1 |
| `meaning`, `data_product`, `activation` stage states from their own reads | 1 row | 2–3 |
| Business process package command delegation | capability | 2 |

Ship criterion: an architect adds a warehouse, enrols a source and registers it, from the UI,
with no startup bootstrap involved.

### Phase 3 — The pipeline becomes a pipeline

Today it is a one-shot: one generation, materialized once, by the bootstrap.

| Work | Closes | Size |
| --- | --- | --- |
| Trigger execution: a scheduler that runs the policies `services/trigger` already holds | 1 row | 3 |
| Refresh producing a second generation, and the console showing both | 1 row (with the above) | 2 |
| An owning service binding a contract to its destination | 1 row | 2 |
| dbt under compiler admission and catalog publication | 1 row; Root B | 3 **?** |
| Stripe provider composed into acquisition, with a live test | 1 row | 2–3 |

Ship criterion: a second generation appears without anyone restarting anything, and the lineage
chain shows two.

### Phase 4 — Expressiveness

The product can express one shape: one projection, one aggregate, grouping on non-null strings,
summing one decimal. No joins, filters, time windows or second measure.

| Work | Closes | Size |
| --- | --- | --- |
| More aggregate functions beyond `sum` | 1 row | 1–2 |
| Joins, filters, time windows, a second measure — each its own legality rule | 1 row | 6–10 **?** |
| A richer publication, which is what widens the question builder | 1 row | 1 (follows the above) |
| ClickHouse landing observation contract (precondition 10) | 1 row; part of parity | 3 **?** |

Ship criterion: a question the current compiler refuses is admitted and answered correctly.

### Phase 5 — Governance depth

| Work | Closes | Size |
| --- | --- | --- |
| Fulfillment admission: standing authority re-check, proposal drift, admission receipt | 1 row | 3 |
| Disclosure classifications carried into the answer scope policy | 1 row | 2 |
| Incident recovery from the console, and a test that raises a real failure and recovers it | 2 rows | 3 |

Ship criterion: an approver whose authority lapsed between approval and admission is refused,
and the refusal is on the record.

### Phase 6 — Semantics

Deliberately last. It is the row most often mistaken for the product, and it is worth least
until the lanes beneath it are wide.

| Work | Closes | Size |
| --- | --- | --- |
| Free-text question interpretation over governed terms | 1 row | 6–10 **?** |
| A product component that decides a question is unsupported | 1 row | 2 |

### Phase 7 — Deployment and parity

| Work | Closes | Size |
| --- | --- | --- |
| Superset provisioning with a pinned image, like the warehouse provider has | 1 row | 2–3 |
| Superset single sign-on | 1 row (needs Phase 1 identity) | 2 |
| MCP authority adapters | 1 row (needs Phase 1 identity) | 3 **?** |
| ClickHouse acquisition and full journey | 1 row | 4–6 **?** |
| One request reaching a published data product end to end | 1 row (falls out of Phases 2–3) | 1 |

## Critical path

```
secret custody ─┬─> source enrolment ─> source registration ─> broker-backed acquisition ─┐
                │                                                                          ├─> refresh / second generation
                └─> warehouse resumption                                                   │
identity ───────┬─> fulfillment admission                                                  │
                ├─> Superset SSO                                                           │
                └─> MCP adapters                                                           │
catalog + compiler admission ──────────────────────────────────────────────────────────────┘
```

Nothing in Phases 3–7 is safely startable before Phase 1. Phase 4 is the exception: compiler
expressiveness is independent of identity and custody, so it can run in parallel with Phase 2 by
a second pair of hands.

## Rough total

| Phase | Weeks |
| --- | --- |
| 1 Foundations | 7–10 |
| 2 Operator surface | 8–10 |
| 3 Pipeline | 12–15 |
| 4 Expressiveness | 11–17 |
| 5 Governance depth | 8 |
| 6 Semantics | 8–12 |
| 7 Deployment and parity | 12–17 |
| **Total** | **66–89 engineer-weeks** |

For one engineer that is well over a year. For three working the parallel tracks above, roughly
two to three quarters to the end of Phase 5, which is the point at which the product is
operable, refreshable and governed — and the point worth aiming at before Phases 6 and 7.

## What not to build

- **Free-text interpretation, early.** It is the most requested and the least load-bearing. A
  question builder over a rich publication answers more real questions than a parser over a thin
  one, and Phase 4 is what makes the publication rich.
- **ClickHouse parity, before the PostgreSQL lane is wide.** Parity with a narrow lane is a
  second narrow lane.
- **A second BI provider.** One works end to end. The gap is provisioning and SSO, not choice.
- **Anything that closes a register row without changing behaviour.** Two rows drifted stale
  this week because the register is maintained by hand. Prefer a test that fails when a row
  becomes untrue over a row edited to match.

## Keeping the register honest

The register drifted twice in one sitting. Both times a capability had been built and the row
still claimed it was missing, and both were found by reading the running console rather than the
document.

**Done.** The register now carries a capability table per deployment shape, and two tests hold
it to the product: `apps/console/server/tests/test_gap_register.py` asserts the warehouse-less
column in the ordinary suite, and the quickstart smoke test asserts the managed column and the
setup stages against a deployment that has a warehouse. Both directions are checked, so a
capability gained with no row fails as loudly as a row claiming one that no longer exists.

It covers the claims that are machine-readable, which is the smaller half. Of the two drifts
that prompted it, this catches one: dashboard publication, a capability state. The other was a
sentence about a chart, and prose is still prose. Treat the table as the part of this page that
cannot lie, and the rest as what it has always been.
