# Demonstration gaps

[status.md](status.md) states what Heinzel does today and how each claim is proved. This page
states what the demonstration console cannot show, measured against one flow:

> A data engineer picks a warehouse and populates it. An external stakeholder asks a question
> nobody anticipated. The answer arrives as a dashboard.

Each gap names the file that establishes it. A gap is **wiring** when the capability exists, is
tested, and no demonstration surface reaches it; **unbuilt** when the capability does not exist.
The distinction decides sequencing, and the two are not comparable in cost.

This page describes the demonstration console in `apps/console`, not a deployment. Several gaps
below are deliberate for a demonstration and would be defects in a deployment; they are listed
because the flow above cannot be shown without closing them.

## The demonstration answers one question

The console accepts a stakeholder question as free text (`request_intake_content` in
`apps/console/server/src/heinzel_console/request_intake.py`). The words then stop: the
demonstration's interpreter checks the tenant and returns a fixed metric and dimension reference,
whatever was asked (`DemoAnswerInterpreter` in
`apps/console/server/src/heinzel_console/demo/answers.py`). Its docstring says so, and the
seeded question is the one it describes.

The consequence is that **any question admitted in the demonstration returns daily order value**,
under the full governed path, with correct evidence for an intent nobody expressed. Resolving a
question against the governed semantic layer is the capability that would close this, and it is
unbuilt. Until it is, a question typed into the demonstration is a label on a fixed answer.

## 1. Pick a warehouse

| Gap | Kind | Established by |
| --- | --- | --- |
| The demonstration wires no warehouse-binding reader, so the warehouse capability reports `not_delivered` and the setup surface refuses. The workspace still summarises as `active`, deliberately: routing to a setup surface that answers 503 would name work the deployment cannot offer. | Wiring | `GovernedConsoleBackend` construction in `demo/console.py` passes no `warehouse_bindings`; `_warehouse_capability` and `_workspace_state` in `governed_backend.py` |
| The demonstration provisions its own PostgreSQL rather than one warehouse-control provisioned, so its setup never advances past `foundation`. | Wiring | `demo/warehouse.py` |
| The `sources`, `meaning`, `data_product` and `activation` setup stages are reported blocked unconditionally. | Unbuilt | `_UNDELIVERED_STAGES` in `governed_backend.py` |
| The engine options carry a fixed region and one capacity profile. | Unbuilt | `WarehouseOptionView` construction in `governed_backend.py` |

warehouse-control itself provisions, validates, backs up, restores, suspends, resumes and retires
on PostgreSQL and ClickHouse with signed evidence, and CI runs that acceptance whenever a change
touches it. The gap here is that the demonstration does not call it.

## 2. Populate it

| Gap | Kind | Established by |
| --- | --- | --- |
| No surface registers a source; the demonstration acquires one seeded source at startup. | Unbuilt | `demo/seed.py`, `_UNDELIVERED_STAGES` |
| The Stripe provider reads object snapshots and events against a mocked API, is not composed into acquisition, and has no live test. | Unbuilt | `providers/stripe`, [status.md](status.md) |
| No owning service binds a contract to its destination; LAND routing is deployment configuration. | Unbuilt | [status.md](status.md) |
| The acquisition receipts surface exists in the console but the demonstration wires no reader. | Wiring | `apps/console/web/src/features/acquisition/`, `demo/console.py` |
| One generation, materialized once. No refresh, no second generation, and no scheduler: `services/trigger` holds trigger policies and nothing runs them. | Unbuilt | `services/trigger`, [quickstart README](../deploy/quickstart/README.md) |

## 3. Define the product

The compiler cannot admit a product plan. Preconditions 15
(`runtime_result_magnitude_enforcement`), 17 (`live_checked_sum_review`) and 18
(`independent_review`) are recorded unsatisfied in
`services/compiler/legality/product-sql/rules/PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE.json`, a
test keeps that record in step with a real compile, and the rule's `review_status` is
`changes_requested`. Every product SQL compilation on either engine returns `No Valid Plan`. See
[the legality rules](../services/compiler/legality/README.md).

The demonstration materializes its product under that refusal rather than around it:
`unadmitted_decision_digest` in `demo/materialization.py` derives a digest under a domain naming
the absence, so a reader who follows the materialization receipt back finds a refusal and not an
approval. The statement dbt runs is the one the compiler emitted, signed; the legality decision
admitting it does not exist.

This is honest and it is still the most consequential gap on this page. The demonstration shows a
governed answer over a product whose own governing step withheld admission.

Independent of those gates, the admitted shape is narrow:

| Gap | Kind | Established by |
| --- | --- | --- |
| One aggregate function exists. | Unbuilt | `AggregateMeasure.function` is `Literal["sum"]` in `packages/iir/src/heinzel_iir/product_models.py` |
| Exactly one projection then one aggregate, grouping on non-null string columns under binary collation, summing exactly one non-null decimal column. No joins, filters, time windows or second measure. | Unbuilt | `constructs` and `excluded_constructs` in the rule file |
| Precondition 10 requires a landing observation contract, which only PostgreSQL has, so the rule can never be satisfied on ClickHouse. | Unbuilt | [legality README](../services/compiler/legality/README.md) |
| dbt models materialize without compiler admission or catalog publication. | Unbuilt | [status.md](status.md) |

## 4. Ask a question

| Gap | Kind | Established by |
| --- | --- | --- |
| The question's words do not reach the answer (above). | Unbuilt | `DemoAnswerInterpreter` in `demo/answers.py` |
| No authentication. An actor is a request header naming one of two fixed identities, which the console states is never a claim it trusts about a real identity. | Unbuilt | `DEMO_ACTOR_HEADER` in `demo/console.py` |
| Deciding that a question is unsupported is done only by the test harness; no product component does it. | Unbuilt | [status.md](status.md) |
| Retrying a failed operation from the console is not delivered. | Unbuilt | [status.md](status.md) |

## 5. Approve and execute

This stage works end to end and is the strongest part of the demonstration. What it does not show:

| Gap | Kind | Established by |
| --- | --- | --- |
| Governed-answer admission and fulfillment admission are exclusive, and the demonstration uses the former. That path re-checks neither each approver's standing authority nor proposal drift, and records no fulfillment admission receipt. | Unbuilt | `governed_backend.py`, [quickstart README](../deploy/quickstart/README.md) |
| The query estimator returns no estimate by design, because PostgreSQL's planner rows and width cannot conservatively represent bytes scanned. No pre-execution cost gate can fire; the scan bound comes from the relation's measured size. | Unbuilt | `PostgreSQLQueryEstimator.estimate` in `providers/postgresql/src/heinzel_provider_postgresql/query_estimator.py` |
| The answer scope policy carries no disclosure classifications although the contract classifies the product `commercial`, so no disclosure control over classified data is shown. | Unbuilt | [quickstart README](../deploy/quickstart/README.md) |
| No test raises an incident from a real failure and recovers it. | Unbuilt | [status.md](status.md) |
| Data access requests are refused at intake, because grant application, expiry and revocation are not delivered. | Unbuilt | `data_access_intake_available=False` in `demo/console.py` |

## 6. Present a dashboard

| Gap | Kind | Established by |
| --- | --- | --- |
| The requester receives a governed table and an exact CSV. There is no chart. | Unbuilt | `apps/console/web/src/features/results/` |
| The dashboards surface exists in the console; the demonstration wires no dashboard reader. | Wiring | `apps/console/web/src/features/dashboards/`, `demo/console.py` |
| Superset single sign-on is not implemented. | Unbuilt | [status.md](status.md) |

The Superset provider publishes governed dashboards to a fresh Superset, reads them back, and
applies and revokes dashboard access, against a live engine. It is not connected to the
demonstration.

## Cross-cutting

| Gap | Kind | Established by |
| --- | --- | --- |
| The entitlement authority, the catalog provider and the question interpreter are demonstration doubles. | Unbuilt | `demo/collaborators.py`, [quickstart README](../deploy/quickstart/README.md) |
| The MCP agent interface cannot start as a production application: no authority adapters are implemented. | Unbuilt | [status.md](status.md) |
| One request reaching a published data product end to end is not delivered. | Unbuilt | [status.md](status.md) |
| ClickHouse parity: warehouse lifecycle, destination, access and statement conformance work; acquisition and the full journey do not. | Unbuilt | [status.md](status.md) |

## Order worth closing them in

The order below is by risk and by cost, not by visibility. The dashboard is the most visibly
absent stage and among the cheapest to close; the interpreter is the least visible and the most
expensive.

1. **The three readers.** Pass warehouse-binding, dashboard and acquisition-receipt readers into
   `GovernedConsoleBackend` in `demo/console.py`, and provision the demonstration's warehouse
   through warehouse-control rather than beside it. This closes stages 1 and 6 of the flow with
   capabilities that already hold under live tests.
2. **Identity.** Two real logins in place of a header. Every other gap is a missing feature; this
   one is a missing boundary, and no external audience should be shown a console where naming
   another actor is a header edit.
3. **The compiler gates.** 15, 17 and 18, or a demonstration product that is admitted. This is
   the only item where the demonstration's headline claim rests on a step that refused, and it is
   the gap least likely to be forgiven on inspection.
4. **A question that reaches its answer.**

On the fourth: a constrained question builder is worth preferring to free-text interpretation, on
three grounds rather than on cost alone.

- The compiler admits one shape. Free text that accepts questions the compiler must refuse
  produces a path whose common outcome is `No Valid Plan`, which demonstrates less than a builder
  that offers only answerable questions.
- A builder composed from the published semantic layer cannot express an intent the layer does
  not carry, so the class of confidently wrong answers described at the top of this page is closed
  by construction rather than by a check.
- A stakeholder composing a question nobody anticipated, from governed terms, is ad-hoc in the
  sense that matters, and the claim stays true.

Free-text interpretation remains the larger capability. It is worth building after the shape the
compiler admits is wider than one aggregate over one decimal column, not before.
