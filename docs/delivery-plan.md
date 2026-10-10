# Delivery plan for the open demonstration gaps

A sequence for the rows [demonstration-gaps.md](demonstration-gaps.md) leaves open. It plans
from what the running system reports about itself, not from the register's prose: the register
has drifted twice in one sitting, and both drifts were rows claiming something was missing that
had since been built.

This is a programme, not a backlog to burn down in order. Read it as four questions — what is a
decision rather than work, what unblocks the most, what the critical path is, and what should
not be built at all.

## The state it plans from

Taken from the live console on 2026-10-10, not from documentation.

| Reported by | Reading |
| --- | --- |
| Capability register (`GET /api/v1/workspace`) | 16 capabilities, 11 `ready`, 5 `not_delivered` |
| Setup surface (`GET /api/v1/setup`) | 7 stages, 2 complete, 1 current, 4 blocked |
| Gap register | 41 rows, 14 closed, 27 open (one of them now partly closed) |
| Warehouse (`psql` against the provisioned instance) | 5 least-privilege roles, 5 landed rows, 1 landing receipt, a built product of 3 rows |

One lane works end to end and is attested at every step. What is missing is breadth and
operability, which is a different problem from correctness and wants different work.

### What has shipped against this plan

Phase 1's first row and Phase 2's first row, in that order, because the second needed the first.

* **Secret custody.** The warehouse's operation secrets are kept in warehouse-control's own
  encrypted store, and the role passwords are derived from one kept root rather than minted per
  start. A restart adopts its warehouse and answers, which was the ship criterion for that row.
* **Source enrolment and registration.** The console composes the connection broker over its own
  register. An architect sees the deployment's enrolled connection, registers it, and the broker
  drives `draft -> validating -> ready` with the PostgreSQL probe observing the real source. The
  ready binding and its two-probe evidence survive a restart. `source-registration` reports
  `ready`, and the sources stage reports `complete` once a source is registered.

* **Catalog asset preview.** One line: the publication repository the materialization already
  writes its receipt into, handed to the console read that was already written against it. The
  Catalog destination lists the four assets the published generation left behind.
* **The business process package.** Contract-service's `ProcessPackageService` was written,
  repositoried and tested, and nothing composed it -- the fourth time in this programme that
  the gap was a composition rather than a build. Composed, the capability is `ready`, the
  stage is reachable, and an architect uploads a narrative and reads the stored version back.
* **The acquisition under the registered binding.** It observes the source under the broker's
  binding identifier, activates its contract over the capability profile the probe measured,
  and reads the binding back out of the broker's register. The acquisition receipts an
  architect reads name the binding the sources stage shows. The hand-built binding and the
  reader that served it are gone.

Making those the same binding needed one of the two to move: the acquisition out of startup, or
the registration into it. The registration moved, because moving the acquisition is not one
change but three -- a command that acquires, materializes and publishes; a console that
recomposes its governed answer over the new generation rather than the one it was built on; and
an operation surface for a dbt run that takes longer than a request. Those three are Phase 3's
refresh, and they are worth doing as that rather than as a prelude to it.

What this costs is honest to state: the demonstration registers its own source on the way up, so
the sources stage opens complete and the architect's registration is a path the product has and
the demonstration does not walk. It is the same code, driven by
`tests/integration/test_source_binding_registration_live.py` and by the console's own suite.

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

### The composition gaps are exhausted

Six times in this programme the gap was that nothing composed a service that was already
written, repositoried and tested: warehouse-control's encrypted secret store, the console's
source registration, the demonstration's source secret store, the connection broker itself,
contract-service's process package service, and semantic-registry's publication read. Each
closed in hours rather than the weeks the register implied, because the week was already spent
somewhere else.

Every remaining `not_delivered` capability has now been checked against the service behind it,
and none of them is that shape:

| Capability | Why it is a build |
| --- | --- |
| `catalog-binding` | Nothing implements `CatalogProvisioner` or `CatalogValidator`, and a ready binding takes validation evidence. The demonstration runs no catalog to provision or observe, so this is Root B below. |
| `semantic-review` | `SemanticReviewService` exists, but `create_bundle` takes a verified candidate set derived from stored observations. The demonstration produces no ontology candidates, so the bundle to review does not exist yet. |
| `data-product-runs` | `RunService` would compose in a line, over a run repository nothing writes to. The page would list nothing, which is what `not_delivered` exists to say. |
| `source-acquisition` | Phase 3's first row. |
| `operation-retry` | `RecoveryCommandService` composes in a line and then refuses most presses: its three recovery collaborators are unimplemented, so the capability would read ready and the buttons would be dead. |

Read the sizes below as sizes again. They are no longer hiding anything already built.

## Three root causes under most of the 27

Closing these does not close rows directly. It is what makes the rows closable.

### A. No persistent secret custody -- closed

Role passwords were minted fresh each start and written nowhere, and enrolment is immutable per
handle, so a handle enrolled on one start was refused on the next. That is why the demonstration
enrolled no source connection, why it offered nothing to register, and why it acquires under a
source binding it builds by hand rather than one a broker registered.

The first three of those are now done: the secrets are kept, the enrolment holds across starts,
and a registration reaches `ready` on probed evidence. Broker-registered **acquisition** is the
one consequence still open, and it is no longer blocked by custody -- it is blocked by the order
the demonstration does things in, which is the paragraph above.

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
| ~~Secret custody: durable, the same across starts~~ **Done.** Kept in warehouse-control's own encrypted store, with the role passwords derived from one kept root. Not rotatable, and on the state volume -- a deployment puts the key in a key management service, which is the difference between this and custody. | Root A; closed the warehouse-resumption row as a consequence | — |
| Authentication and a workspace principal directory | Root C; closes *no authentication* | 4–6 **?** |

Ship criterion: a restart adopts its warehouse and answers; two real identities sign in and the
console refuses one of them something.

### Phase 2 — The operator surface

The six blocked setup stages are the largest visible hole: the console looks like a product with
nine destinations and six are closed doors.

| Work | Closes | Size |
| --- | --- | --- |
| ~~Source enrolment and registration end to end, broker-backed~~ **Done.** All three rows: the registration surface, the enrolment, and the acquisition running under the binding the broker registered. | 3 rows | — |
| Warehouse engine options: real regions and capacity profiles | 1 row | 1 |
| `meaning`, `data_product`, `activation` stage states from their own reads | part of 1 row (`sources` and `business_process` now derive theirs) | 2–3 |
| ~~Business process package command delegation~~ **Done.** `ProcessPackageService` existed, with its repository and its tests, and nothing composed it. Composed, the capability reports `ready` and an architect uploads a narrative, sees its digest, and reads the stored version back. | `process-package`; one of the three remaining setup stages | — |

Ship criterion: an architect adds a warehouse, enrols a source and registers it, from the UI,
with no startup bootstrap involved.

### Phase 3 — The pipeline becomes a pipeline

Today it is a one-shot: one generation, materialized once, by the bootstrap. Phase 1 and 2
cleared everything in front of this, so it is now the top of the list rather than a thing to
reach. Its first row is three changes, and worth naming as three: a command that acquires,
materializes and publishes a generation; a console that recomposes its governed answer over the
newest generation instead of the one it started on; and an operation surface for a run that
takes longer than a request will wait.

| Work | Closes | Size |
| --- | --- | --- |
| An acquisition the console runs: `POST /api/v1/acquisitions/run-now` composed over a real acquisition application, with the three changes above | 1 row; `source-acquisition` | 3–4 |
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
   (done)       │        (done)               (done)                    (done)             ├─> refresh / second generation
                └─> warehouse resumption (done)                                            │
identity ───────┬─> fulfillment admission                                                  │
                ├─> Superset SSO                                                           │
                └─> MCP adapters                                                           │
catalog + compiler admission ──────────────────────────────────────────────────────────────┘
```

The custody branch is clear. Everything it blocked is built, and the next node on it -- refresh,
a second generation -- is now the top of Phase 3 with nothing in front of it.

Phase 4 remains independent of identity and custody, so it can run in parallel with Phase 2 by a
second pair of hands.

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
