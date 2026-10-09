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
| On its default path the demonstration is given a warehouse rather than creating one, so it wires no warehouse-binding reader: the warehouse capability reports `not_delivered` and the setup surface refuses. The workspace still summarises as `active`, deliberately: routing to a setup surface that answers 503 would name work the deployment cannot offer. | Unbuilt on the default path, delivered on the opt-in one | `GovernedConsoleBackend` construction in `demo/console.py` passes `warehouse_bindings` only on the warehouse-control path; `_warehouse_capability` and `_workspace_state` in `governed_backend.py` |
| The opt-in path's live provisioning is proved by no test in this repository. The offline suite proves the Compose operations it issues and their order, that a failure at each one is classified and reported, and that the setup surface answers from a binding already `ready`; it does not start a container. | Unbuilt | `apps/console/server/tests/test_demo_managed_warehouse.py` |
| ~~The opt-in path and the answering path are exclusive, so no single console both reports a managed warehouse and answers a question over it.~~ **Delivered, and it publishes the dashboard too.** The console composes its governed answer over the warehouse warehouse-control provisioned, so the one path that produces a binding that service owns is the one the demonstration answers from, as ADR-0003 requires. The provider says where its warehouse listens, which name it answers to on its own container network and which certificates reach it; it never hands out the password, because provisioning rotates the administering login to the `administration` operation secret and whoever minted that secret already holds it. Superset reaches that warehouse by name over mutual TLS and reads the product as `dashboard_reader`. Two manual steps are needed because Superset and the console run in different Compose projects: putting Superset on the warehouse's network, and letting it make the `0600` copy of the client key libpq requires of the user presenting it. The quickstart README gives both. | Delivered | `_administration_dsn`, `internal_hostname` and `share_warehouse_client_material`; [quickstart README](../deploy/quickstart/README.md) |
| The opt-in path answers only in the start that provisioned the warehouse. Provisioning rotates the administering login to an operation secret minted per start and written nowhere, so a later start adopts a `ready` binding holding none of the credentials that warehouse accepts: it reports the binding and reports every answer capability as not delivered, which is what it reports with no warehouse at all. Making it resumable would mean keeping those secrets in the state directory, which this path deliberately does not do. A provisioning that stopped part-way is refused outright for the same reason. | Unbuilt, deliberately | `_resumable_binding` and `_administration_dsn` in `demo/managed_warehouse.py` |
| The `meaning`, `data_product` and `activation` setup stages are reported blocked unconditionally. `sources` no longer is: its state derives from the connection-broker read, and it reports blocked with its own dependency named when no reader is wired. | Unbuilt | `_UNDELIVERED_STAGES` and `_stage_states` in `governed_backend.py` |
| The engine options carry a fixed region and one capacity profile. | Unbuilt | `WarehouseOptionView` construction in `governed_backend.py` |

warehouse-control itself provisions, validates, backs up, restores, suspends, resumes and retires
on PostgreSQL and ClickHouse with signed evidence, and CI runs that acceptance whenever a change
touches it. It provisions by driving a Compose project: `PostgreSQLWarehouseProvider` takes a
`ComposeProjectControl` alongside nine secret capabilities and TLS material.

An earlier revision of this page said closing this was a deployment-shape change rather than a
reader, and that is what it turned out to be. `HEINZEL_DEMO_WAREHOUSE_CONTROL` now selects that
shape: `demo/managed_warehouse.py` composes `WarehouseControlService`,
`WarehouseLifecycleOrchestrator`, `PostgreSQLWarehouseProvider` and `DockerComposeProcess` over
`deploy/quickstart/warehouse-control/compose.yaml`, and the console reports the binding
warehouse-control made. The default path is unchanged, and still reports `not_delivered`, because
`WarehouseBinding.deployment_mode` admits only `heinzel_cloud`: a binding for the database the
demonstration provisions beside warehouse-control would say a managed warehouse exists where a
temporary local one does.

### Why that path is opt-in rather than default

Creating containers means the console process runs `docker`, and a console in a container needs
that daemon's socket bind-mounted into it. **Access to the Docker socket is root on the host.**
This console has no authentication, so anyone who reaches its published port would reach a
process that can start a container mounting the host's filesystem. The quickstart's
`compose.yaml` therefore does not mount the socket and does not set this variable. Two further
constraints follow from the same fact: Compose bind-mounts are resolved by the daemon on the
host, so the provider's private directory has to mean the same path to both — which holds for a
console run from a checkout and not for one whose state directory is a named volume — and the
compose project has to be on disk, which the quickstart image does not carry.

The second reason is evidence. The live path cannot be run where there is no daemon, so making it
the only path would have replaced a startup this repository proves with one it cannot. The
[quickstart README](../deploy/quickstart/README.md) states all of this where an operator reads it.

## 2. Populate it

| Gap | Kind | Established by |
| --- | --- | --- |
| The console registers a source, and the demonstration reaches that surface only on the opt-in warehouse-control path. It lists the bindings the broker holds for a tenant, offers the enrolled connections no binding names yet, and registers one by driving `draft -> validating -> ready` through `SourceBindingService`. It is reached through `GET /api/v1/setup` and `POST /api/v1/setup/sources`; on the default path both answer `capability_not_delivered` for the warehouse-binding reason in section 1 above, and on the opt-in path `get_setup` answers and the next row is what is still missing. | Wiring | `register_source` in `governed_backend.py`; `_require_warehouse_binding_reader` refuses `get_setup` at `governed_backend.py:864` |
| The demonstration enrols no source connection, so it would offer nothing to register even past the setup refusal. Its role passwords are minted fresh on every start and written nowhere, and enrolment is immutable per handle -- so a handle enrolled on one start is refused on the next, and enrolling at all would put a credential on the state volume the demonstration deliberately keeps clear of one. | Unbuilt | `_fresh_passwords` in `demo/bootstrap.py:86-96`, applied again at `:303-305`; `enroll_connection` in `demo/source_secrets.py:151-173` |
| The demonstration acquires one seeded source at startup under a source binding it constructs by hand, rather than one the broker registered. | Unbuilt | `_DemoSourceBindingReader` and `_source_binding` in `demo/generation.py`, `demo/seed.py` |
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
| ~~How an answer was produced is recorded in full and shown nowhere: a reviewer could approve an answer without seeing which warehouse it came from, what shape the source was read in, or either statement the compiler emitted.~~ **Delivered as a read, on the answering path.** The console joins the receipts the services already wrote -- the acquisition contract's agreed object shape, the landing run's receipt, the signed transform and its materialization receipt, the signed query plan with its ceilings and the execution receipt -- and the decision workspace shows them as seven stages under a `Lineage` tab. Nothing is narrated: a stage whose step has not happened is absent rather than described, and the chain is the architect's to read, so a requester is answered `404`. Wired alongside the governed answer, because the query and the execution are read through the runtime that answer owns. | Delivered | `demo/provenance.py`; `get_request_provenance` in `governed_backend.py`; `features/inbox/provenance-pane.tsx`; `_assert_the_production_chain_reads_back` in [the quickstart smoke test](../tests/quickstart/test_quickstart_smoke.py) |
| ~~After admission the evidence panel reports `0 of 2 required approval(s) are recorded`, although both were recorded and both were checked.~~ Closed in the projection, which now matches the approvals against the revision the admission was taken from rather than against the request's current one. Proved at the console projection over a real request lifecycle, not re-measured on a live console. | Delivered | `_admitted_from_revision` and `_approval_views` in `governed_backend.py`; `test_the_approvals_an_admission_consumed_stay_recorded_once_it_advances_the_revision` in `apps/console/server/tests/test_governed_commands.py` |
| ~~Data access requests are refused at intake, because grant application, expiry and revocation are not delivered.~~ **Delivered.** The demonstration composes access-control over its own stores, so an admitted request reaches an active grant narrowed against the same entitlement the answer resolved through, and a revocation reaches a revoked one. Two of the three effects are demonstration stand-ins that record a receipt rather than granting a database role or setting a Superset permission, which the module states: the grant is real and what it is a grant *of* is not yet enacted at the warehouse or at Superset. | Delivered | `demo/access.py`; `test_demo_access.py` |

The approval count was worth separating from the rest, because the mechanism under it was correct
and the display was not. An approval is bound to the request revision it was given against, so any
change to the request withdraws it and the offered admission with it. That is pinned by
`test_a_revision_bump_after_the_approvals_withdraws_the_offered_admission` in the
[console governed journey](../tests/end-to-end/test_console_governed_journey.py), and it is the
property that stops an approval outliving what it approved.

Admission itself advances the revision. On the fulfillment path the admission receipt carries
`source_request_revision`, which pins the match back to the revision the approvals were given
against, and the count survived. The answer path records no such receipt, so the match fell back to
the request's current revision and no approval carried it. The panel then reported none recorded for
a request whose admission required all of them -- `2 of 2` at revision 4, `0 of 2` at revision 7,
where admission, execution and delivery account for the three revisions in between.

What the projection now matches against is not the approvals and not the receipt the answer path
does not write, but the transition the admission wrote: a request leaves `awaiting_approval` for
`executing` in the same transaction that records the admission, and `TransitionEvent` carries the
revision that transaction produced, so the revision it was taken from is the one before it.
Matching instead on the revision a proposal was approved at would report approvals as satisfied in
exactly the case the test above exists to catch; this does not, because a revision bump after the
approvals leaves the request in `awaiting_approval` with no such transition, and the count falls
back to the current revision and reports none recorded. That case is pinned alongside the first by
`test_approvals_the_admission_could_not_have_consumed_are_never_reported_as_recorded`.

The missing fulfillment admission receipt is a gap in its own right and stays as the first row of
this stage: nothing here re-checks an approver's standing authority or proposal drift. This closes
the count, not the admission.

## 6. Present a dashboard

| Gap | Kind | Established by |
| --- | --- | --- |
| The requester receives a governed table and an exact CSV. There is no chart. | Unbuilt | `apps/console/web/src/features/results/` |
| ~~The dashboards surface exists in the console and the demonstration publishes no dashboards.~~ **Delivered.** The console reads what it published from bi-control's own repository, where a publication is recorded only against a provider receipt, so the read answers the same question a control service would without holding a provider. Read that way on purpose: over the control service the surface would go absent exactly when no Superset is configured, reporting nothing published for a deployment that published something and has since been given no instance. | Delivered | `RepositoryDashboardPublicationReader` in `governed_adapters.py`; `test_demo_console.py` |
| ~~A published dashboard renders nothing.~~ **Delivered.** The Superset provider was given the governed intent as a `viz_type` and params carrying only Heinzel's own metadata, so Superset had no plugin to draw with and no query to run: the dashboard reported as published and its chart answered `Empty query?`. The intent is now translated to the plugin that draws it, and the desired state carries how the approved query binding reads each metric and dimension -- which column, which aggregate -- so the chart asks for the governed metric along the governed dimension. Each of the four shapes was confirmed against a running Superset rather than written from documentation. | Delivered | `_form_data` and `_VIZ_TYPES` in `providers/superset/src/heinzel_provider_superset/client.py`; `DashboardMetricProjection` in `services/bi-control/src/heinzel_bi_control/models.py` |
| ~~Publishing needs a **delivered** answer, and nothing in the product reacts to delivery.~~ **Delivered, by the person running it rather than by a reaction.** The seeded question still waits, and publication is a command the console offers once that question has been answered and delivered -- which is the demonstration's point rather than a gap in it. The result evidence still expires an hour after delivery, and the declared intent carries that deadline. | Delivered | `composition.py` and `dashboard_authority.py`; `demo/bi_provider.py` |
| ~~The publication path exists and nothing in the demonstration can reach its end.~~ **Delivered on the warehouse-control path.** `POST /api/v1/dashboards/publications` settles `published` against a real Superset, and the console lists the result. On the default path it still cannot: a dashboard dataset connection cites a warehouse-control binding and a database that service never saw has none, so the publication fails as an unavailable authority -- which ADR-0003 makes deliberate rather than unfinished. | Delivered on the opt-in path | `publication.py` in `services/bi-control/`; `demo/bi_provider.py`; [ADR-0010](architecture/decisions/ADR-0010-dashboard-publication-lifecycle.md) |
| The demonstration now offers a dashboard and can publish to nothing. It seeds one certified contract over its own product generation and approved terms, signed under a key kept beside its other state so a restart still verifies it, and the decision workspace names that dashboard for the delivered answer. What is missing is the only thing left: a BI provider. Publishing stays unwired, the offering says so, and the control is not rendered -- an architect sees which dashboard matches and is told publication is not delivered, rather than being given a button that refuses every press. | Unbuilt | `demo/dashboard_contract.py`; `_compose_publishable_dashboards` in `demo/console.py` |
| Nothing provisions a Superset. The provider speaks HTTP to an instance that already exists; unlike the PostgreSQL warehouse provider there is no provisioning path, no pinned-image constant and no container inspection. The only thing that creates one is the test emulator, which the demonstration may not import, and which builds its image rather than pinning it because the official one carries no PostgreSQL driver. | Unbuilt | `providers/superset/.../client.py`; `tests/emulators/superset/` |
| ~~The requester could not see a published dashboard.~~ **No longer answered `404`.** The dashboards read for a requester needs access grants, a principal directory and the inbox, and all three are wired now, so a requester is answered `200` with the dashboards their grants name. It is an empty list until one does: publication confers no access, which is ADR-0010's rule and not an omission here. That an admitted dashboard access request makes a published dashboard appear in that list is not verified -- the grant reaches `active`, and nothing has yet driven that grant through this read. | Partly delivered, the rest unverified | `get_dashboards` and `_requester_dashboard_publications` in `governed_backend.py`; `demo/access.py` |
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
5. **The deployment shape**, which is what the warehouse binding and the dashboards are. Half of
   this is now delivered and opt-in: the demonstration can create a warehouse through
   warehouse-control, which is what makes the binding real, and the cost is the Docker socket —
   root on the host — which is why it is not the default. What is left of it is the two paths
   being exclusive, so one console does not yet both report that warehouse and answer over it,
   and the live provisioning being proved by the warehouse-lifecycle acceptance run rather than
   by the demonstration's own. Dashboards still need a Superset to publish to. Neither was a
   reader.

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
