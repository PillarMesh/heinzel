# Demonstration gaps

[status.md](status.md) states what Heinzel does today and how each claim is proved. This page
states what the demonstration console cannot show, measured against one flow:

> A data engineer picks a warehouse and populates it. An external stakeholder asks a question
> nobody anticipated. The answer arrives as a dashboard.

Each gap names the file that establishes it. A gap is **wiring** when the capability exists, is
tested, runs in the shape this demonstration deploys, and only wants a reader passed in;
**unbuilt** when it does not exist, or exists only in a deployment shape the demonstration is
not. The distinction decides sequencing, and the two are not comparable in cost. It is also
easy to get wrong from the console's side alone: three of the gaps below read as missing
arguments and are not. The column stays because the distinction is the one worth asking of
each new gap. The acquisition receipts were the first of the three to close, and closing them
took the acquisition itself rather than a reader.

This page describes the demonstration console in `apps/console`, not a deployment. Several gaps
below are deliberate for a demonstration and would be defects in a deployment; they are listed
because the flow above cannot be shown without closing them.

## The demonstration answers the question that was composed, and only a composed one

This section used to say that any question admitted in the demonstration returned daily order
value, under the full governed path, with correct evidence for an intent nobody expressed. That is
closed. A stakeholder question now carries the governed terms it was composed from
(`QuestionTermSelection` in
`services/request-management/src/heinzel_request_management/models.py`), the console offers exactly
the terms the publication carries (`GET /api/v1/answer-terms`, read through
`SelectableAnswerTermReader`), and the interpreter resolves that selection
(`DemoAnswerInterpreter` in `apps/console/server/src/heinzel_console/demo/answers.py`). A selection
naming a term the publication does not carry, or naming a dimension where the measure belongs, is
refused rather than substituted for — and so is a question that carries no selection at all, which
is the case the old behaviour answered confidently.

What remains unbuilt is free-text interpretation: resolving the words of a question against the
governed semantic layer. The words are kept as the label of what was asked and nothing reads them.
A question with no selection is still accepted, stored, clarified and proposed exactly as before;
it is the answer that refuses it, which is the honest place for the refusal.

The selection carries no time window, because nothing in the published vocabulary declares which
dimension is a time axis: `SemanticObject` carries an identifier, a name, a definition and its
source references (`packages/contract-model/src/heinzel_contract_model/models.py`), and
`BoundSemanticReference.kind` distinguishes only a metric from a dimension
(`services/request-management/src/heinzel_request_management/answer_validation.py`). A builder
offering one would assert a grain the publication never stated.

## 1. Pick a warehouse

| Gap | Kind | Established by |
| --- | --- | --- |
| The demonstration wires no warehouse-binding reader, so the warehouse capability reports `not_delivered` and the setup surface refuses. The workspace still summarises as `active`, deliberately: routing to a setup surface that answers 503 would name work the deployment cannot offer. | Unbuilt | `GovernedConsoleBackend` construction in `demo/console.py` passes no `warehouse_bindings`; `_warehouse_capability` and `_workspace_state` in `governed_backend.py` |
| The demonstration provisions its own PostgreSQL rather than one warehouse-control provisioned, so its setup never advances past `foundation`. Passing a reader would not change that: a binding reported here has to be one warehouse-control made. | Unbuilt | `demo/warehouse.py` |
| The `sources`, `meaning`, `data_product` and `activation` setup stages are reported blocked unconditionally. | Unbuilt | `_UNDELIVERED_STAGES` in `governed_backend.py` |
| The engine options carry a fixed region and one capacity profile. | Unbuilt | `WarehouseOptionView` construction in `governed_backend.py` |

warehouse-control itself provisions, validates, backs up, restores, suspends, resumes and retires
on PostgreSQL and ClickHouse with signed evidence, and CI runs that acceptance whenever a change
touches it. It provisions by driving a Compose project: `PostgreSQLWarehouseProvider` takes a
`ComposeProjectControl` alongside nine secret capabilities and TLS material. A console that
reports a real binding is therefore a console that creates warehouses, and the quickstart
declares its warehouse as a sibling service the console cannot create. Reporting a binding here
without that is worse than reporting none, because `WarehouseBinding.deployment_mode` admits
only `heinzel_cloud`: the record would say a managed warehouse exists where a temporary local
database does. Closing this is a deployment-shape change, not a reader.

## 2. Populate it

| Gap | Kind | Established by |
| --- | --- | --- |
| No surface registers a source; the demonstration acquires one seeded source at startup. | Unbuilt | `demo/seed.py`, `_UNDELIVERED_STAGES` |
| The Stripe provider reads object snapshots and events against a mocked API, is not composed into acquisition, and has no live test. | Unbuilt | `providers/stripe`, [status.md](status.md) |
| No owning service binds a contract to its destination; LAND routing is deployment configuration. | Unbuilt | [status.md](status.md) |
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
| The question's words still do not reach the answer; the governed terms it was composed from do (above). Free-text interpretation is unbuilt. | Unbuilt | `DemoAnswerInterpreter` in `demo/answers.py` |
| The builder offers one metric and one dimension, because that is all the demonstration's publication carries. A richer offering is a richer publication, not a wider form. | Unbuilt | `demo_answer_bindings` in `demo/answers.py` |
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
| After admission the evidence panel reports `0 of 2 required approval(s) are recorded`, although both were recorded and both were checked. Measured on a live console: `2 of 2` at revision 4, `0 of 2` at revision 7. | Unbuilt | `_approval_views` in `governed_backend.py`; `source_request_revision=expected_revision` in `services/request-management/src/heinzel_request_management/fulfillment_service.py` |
| Data access requests are refused at intake, because grant application, expiry and revocation are not delivered. | Unbuilt | `data_access_intake_available=False` in `demo/console.py` |

The approval count is worth separating from the rest, because the mechanism under it is correct and
the display is not. An approval is bound to the request revision it was given against, so any change
to the request withdraws it and the offered admission with it. That is pinned by
`test_a_revision_bump_after_the_approvals_withdraws_the_offered_admission` in the
[console governed journey](../tests/end-to-end/test_console_governed_journey.py), and it is the
property that stops an approval outliving what it approved.

Admission itself advances the revision. On the fulfillment path the admission receipt carries
`source_request_revision`, which pins the match back to the revision the approvals were given
against, and the count survives. The answer path records no such receipt, so the match falls back
to the request's current revision and no approval carries it. The panel then reports none recorded
for a request whose admission required all of them.

The fix is the receipt, not the view. Matching instead on the revision a proposal was approved at
would report approvals as satisfied in exactly the case the test above exists to catch.

## 6. Present a dashboard

| Gap | Kind | Established by |
| --- | --- | --- |
| The requester receives a governed table and an exact CSV. There is no chart. | Unbuilt | `apps/console/web/src/features/results/` |
| The dashboards surface exists in the console and the demonstration publishes no dashboards. `deploy/quickstart/compose.yaml` declares `warehouse` and `console` and nothing else, and the reader returns only dashboards whose desired state has a matching provider receipt, so a reader passed in would report a delivered capability with nothing in it. | Unbuilt | `deploy/quickstart/compose.yaml`; `DashboardPublicationReader` in `governed_adapters.py` |
| Superset single sign-on is not implemented. | Unbuilt | [status.md](status.md) |

The Superset provider publishes governed dashboards to a fresh Superset, reads them back, and
applies and revokes dashboard access, against a live engine. It is not connected to the
demonstration.

## Cross-cutting

| Gap | Kind | Established by |
| --- | --- | --- |
| The entitlement authority and the catalog provider are demonstration doubles. The question interpreter is demonstration-grade for a different reason: it resolves a governed term selection, which is real, and reads no prose, which a deployment's would. | Unbuilt | `demo/collaborators.py`, [quickstart README](../deploy/quickstart/README.md) |
| The MCP agent interface cannot start as a production application: no authority adapters are implemented. | Unbuilt | [status.md](status.md) |
| One request reaching a published data product end to end is not delivered. | Unbuilt | [status.md](status.md) |
| ClickHouse parity: warehouse lifecycle, destination, access and statement conformance work; acquisition and the full journey do not. | Unbuilt | [status.md](status.md) |

## Order worth closing them in

The order below is by risk and by cost, not by visibility.

An earlier revision of this page opened with three readers to pass into
`GovernedConsoleBackend`, and said that closed stages 1 and 6. It does not. Each of those three
reads from the console's side as a missing argument, and each turns out to need something the
demonstration does not have: a warehouse binding needs a warehouse warehouse-control created,
dashboards need a Superset the quickstart does not run, and acquisition receipts need the
acquisition to run through the runtime's application rather than the provider alone. Only the
third is reachable without changing what the demonstration deploys, which is why it leads here
and the other two sit with the deployment-shape work.

1. ~~**Acquisition receipts.**~~ Delivered. The demonstration acquires through the runtime's
   acquisition application, the service composes the evidence, and the console reads it. The
   placeholder run and contract digests went with it, and a start that dies between landing and
   publishing now resumes from what it landed rather than acquiring into a checkpoint it cannot
   take a snapshot from.
2. **Identity.** Two real logins in place of a header. Every other gap is a missing feature; this
   one is a missing boundary, and no external audience should be shown a console where naming
   another actor is a header edit.
3. **The compiler gates.** 15, 17 and 18, or a demonstration product that is admitted. This is
   the only item where the demonstration's headline claim rests on a step that refused, and it is
   the gap least likely to be forgiven on inspection.
4. ~~**A question that reaches its answer.**~~ Delivered as the constrained builder argued for
   below. A requester composes a question from the terms the publication carries, the selection
   travels with the request as structured data, and the interpreter resolves it or refuses. What is
   left of this item is free-text interpretation, which is deliberately still absent.
5. **The deployment shape**, which is what the warehouse binding and the dashboards are. Either
   the demonstration gains a warehouse it created and a Superset to publish to, or those two
   stages stay honestly absent. Neither is a reader.

On the fourth: a constrained question builder was worth preferring to free-text interpretation, on
three grounds rather than on cost alone.

- The compiler admits one shape. Free text that accepts questions the compiler must refuse
  produces a path whose common outcome is `No Valid Plan`, which demonstrates less than a builder
  that offers only answerable questions.
- A builder composed from the published semantic layer cannot express an intent the layer does
  not carry, so the class of confidently wrong answers described at the top of this page is closed
  by construction rather than by a check. It is closed by a check as well: the interpreter refuses
  a selection the builder could not have offered, because an interpreter that trusted its caller
  would be the authority for a reading it never verified.
- A stakeholder composing a question nobody anticipated, from governed terms, is ad-hoc in the
  sense that matters, and the claim stays true.

Free-text interpretation remains the larger capability. It is worth building after the shape the
compiler admits is wider than one aggregate over one decimal column, not before.
