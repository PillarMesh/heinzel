# Changelog

All notable changes to Heinzel are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Initial public release of Heinzel under the Apache License 2.0.
- A Docker Compose quickstart that runs the demonstration console and the PostgreSQL warehouse
  it answers over, on `127.0.0.1:8000` or on another port through `HEINZEL_PORT`. The warehouse
  is not published, and `down -v` discards it together with the console's state.
- The demonstration answers its own seeded question. Given `HEINZEL_DEMO_WAREHOUSE_DSN`, the
  console provisions that database with five least-privilege logins, acquires its seeded source
  through the runtime's governed acquisition under an acquisition contract it activates over an
  observation of that source, lands it under a receipt and acknowledges it, compiles and
  materializes a product with dbt, and publishes the generation through the product authorities
  -- all before it listens. Acquisition happens once and never again: the acknowledgement
  advances the source checkpoint, so a later start reads back the generation it already landed. Admitting the approved proposal then compiles a governed query over that product,
  bounds its scan by the relation's measured size, admits it under the answer scope policy's
  ceilings, runs it as a read-only role and delivers the rows to the requester who asked.
  Without that variable the console is unchanged: every answer capability reports itself as not
  delivered. `deploy/quickstart/README.md` records what the answer went through and where it
  stops.
- A constrained question builder. A stakeholder question now carries the governed terms it was
  composed from -- an approved metric and at least one approved dimension, as
  `QuestionTermSelection` -- and the demonstration's interpreter resolves that selection instead
  of naming fixed terms whatever was asked. The console offers exactly the terms the tenant's
  publication carries, read through `GET /api/v1/answer-terms`, so the form cannot drift from what
  the semantic layer will resolve; the interpreter refuses a selection naming an unpublished term,
  a term of the wrong kind, or no term at all, rather than substituting one. The selection is
  optional and excluded from serialization when absent, so every question recorded before it
  existed still validates and still digests to what it digested to -- and is refused at the answer
  rather than answered about something else. A question's words remain its label: nothing reads
  them, and free-text interpretation is still not delivered.
- A source can be registered through the connection broker. `SourceBindingService` could already
  drive a binding `draft -> validating -> ready`, but both collaborators it needs to do it were
  protocols with no implementation anywhere, so no source could be registered at all and the
  demonstration built its ready binding by hand. `PostgreSQLSourceCapabilityProbe` is the missing
  probe: it observes the declared objects through the acquisition provider's own `observe_source`,
  and observes the server refusing the connecting role every relation in the schema the
  declaration is held away from -- the same least-privilege property `PostgreSQLAcquisitionProvider`
  refuses an acquisition for, against the same `unrelated_schema_name`. Each digest is taken over
  what the server answered rather than over what was asked, and neither includes a clock reading,
  so an unchanged source observes to the same digest twice. Because
  `SourceBindingValidationEvidence` types both `*_succeeded` fields as `Literal[True]`, a probe
  that fails raises rather than returning evidence that records the failure. The demonstration's
  `DemoSourceSecretStore` is the missing resolver: references minted from the operating system's
  generator rather than derived from the credential they name, kept in files mode 0600 under the
  demonstration's state directory, with the connection detail in no `repr` and no error message.
  A live journey registers a real PostgreSQL source end to end, reaching `ready` at revision 3
  with the capability profile and source observation the probe measured, and leaves a binding whose
  role reaches outside its declaration in `validating` with no evidence at all. What this does not
  add is a console surface for registering a source, or the demonstration switching its hand-built
  binding over to this path.
- The console's source onboarding surface. `GET /api/v1/setup` now reports which sources a tenant
  has registered -- each binding's handle, provider, lifecycle state, approved objects and, for a
  `ready` one, the capability profile digest the probe measured -- alongside the connections an
  operator has enrolled that no binding names yet. `POST /api/v1/setup/sources` registers one of
  those, driving `draft -> validating -> ready` through `SourceBindingService` so the binding that
  results carries the two-probe evidence or no `ready` state at all. The `sources` setup stage is
  no longer blocked unconditionally: it derives from whether a connection-broker read is wired and
  whether a registered source reached `ready`, and reports blocked with its own dependency named
  when no reader is wired rather than borrowing the sentence an unbuilt stage carries.

  The surface deliberately accepts no connection string. Enrolling a connection is an operator
  action in whatever secret custody the deployment injected into the broker, which happens before
  any binding exists; the console works over handles that are already enrolled, and the provider,
  account mode and approved objects a registration uses come from the deployment's own offering
  rather than from the browser -- so nothing a form sends can widen the declaration the probe
  proves least privilege over. A browser form taking a DSN would be a credential-handling surface
  needing its own review, and the stage says where the connection came from instead of implying
  the console holds it. Tests assert the absence rather than describing it: the command model
  carries no field that could hold an endpoint, a credential or a reference to either, a body
  carrying one is refused by the contract, no serialized view or error message contains a
  connection detail, and a custodian that puts a DSN in its own failure message does not get it
  printed by anything the console raises.

  `DemoSourceSecretStore` can now list the handles it holds, and nothing else about them. The
  demonstration is not wired to this surface and cannot be yet: it wires no warehouse-binding
  reader, so `/api/v1/setup` and this command both answer `capability_not_delivered`, and it
  enrols no connection because its role passwords are minted fresh on every start and written
  nowhere while enrolment is immutable per handle. `docs/demonstration-gaps.md` records both.
- `PostgreSQLRelationSizeQueryEstimator`, which bounds a restricted statement's scan by the
  measured size of the one relation it reads. `PostgreSQLQueryEstimator` beside it declines to
  report PostgreSQL's planner output as scan bytes and is right to, which left policy admission
  with no estimate to check a ceiling against. The new bound needs no statistics, refuses a
  statement over any relation other than the one it was configured for, and is deliberately
  loose: it is an upper bound on what a statement can read, not a prediction of what a plan will.
- `SQLiteProductMaterializationReceiptReader`, so a process that restarts over an existing
  ledger can answer for a generation whose materialization run has long since ended.
  `ProductMaterializationRunner.read_receipt` now delegates to it rather than keeping a second
  copy of which records are answerable.
- A `heinzel-console` command that serves the demonstration console from a state directory.
- The release audit verifies the private terms file against `HEINZEL_PRIVATE_TERMS_SHA256`,
  and requires that variable whenever `HEINZEL_REQUIRE_PRIVATE_TERMS=1`. Its other checks all
  accept a terms file that is well formed but incomplete, so one that lost lines on its way in
  would scan the tree with less coverage than was configured and still report a pass. The
  digest is carried beside the file rather than committed, because that file lives outside this
  repository and changes independently of it. `tests/release/README.md` records the variables,
  the terms file format and what each misconfiguration produces.
- `docs/demonstration-gaps.md`, which reads `docs/status.md` the other way round: what the
  demonstration console cannot show, measured against one flow -- a warehouse chosen and
  populated, an unanticipated question asked, an answer presented as a dashboard. Each gap
  names the file that establishes it and whether the capability is unbuilt or merely
  unwired, and the page closes with the order the gaps are worth closing in.

### Changed

- An answer's policy admission may now be recorded from a request awaiting approval as well as
  from one being investigated, so a console that drives propose, review, approve, admit can
  reach an admitted plan. The reason code `request_not_investigating` becomes
  `request_state_not_admissible`, which is a breaking change for a caller matching on it. The
  set is stated in three places that must agree -- the evaluation, the transaction that records
  the receipt and the delivery's re-read of the request's history -- and all three moved
  together. Nothing else about the admission changed: the plan, the policy, the entitlement
  snapshot and every ceiling are decided the same way whichever state it was asked from, and the
  state check remains a check on where the request has got to rather than a claim about what has
  been approved.
- The demonstration console starts again over its own state directory. Two things stopped it:
  it connected to its warehouse without waiting, and the local entitlement authority refused the
  entitlement a second start publishes. `docker compose restart` restarts both containers without
  re-reading `depends_on`, so the console came back while PostgreSQL was still starting; and the
  authority's store, which is a stand-in for a connected one, refuses a body that does not
  supersede what it holds -- which a second start's otherwise identical entitlement does not,
  because its timestamps move while its revision does not. The authority now starts from an empty
  store, as the rest of it already did: its signing key, its bearer credential and its TLS
  material are all generated per run, so an entitlement carried over was one nothing could verify.
- The demonstration console waits up to a minute for its warehouse to accept connections before
  provisioning it, instead of ending the command on the first refused connection. Compose holds
  the first start back until the warehouse reports healthy, but `docker compose restart` restarts
  both containers without re-reading `depends_on`, which left the console exiting while PostgreSQL
  was still starting. A refusal the server produced -- a wrong password, a missing database -- is
  still reported at once rather than waited out.
- `heinzel-provider-postgresql` now declares `heinzel-compiler`, which its new estimator
  constructs a scan estimate with. Installed from its own manifest alone it would have imported
  cleanly and then raised `ImportError` from the call.
- The declared minimum versions of the runtime dependencies now match what the lockfile actually
  resolves: `cryptography` 50.0.1, `pydantic` 2.13.5, `psycopg` 3.3.6 and `mcp` 2.2.0. Several had
  drifted several releases behind the versions every test ran against. `starlette` 1.7.0 and
  `snowflake-connector-python` 4.7.5 followed, and `dbt-clickhouse` moved to 1.10.3.
- Product materialization now refuses a result the warehouse did not attest was held to the
  declared `Decimal(57,9)` output magnitude. `MaterializationObservation` carries the output columns
  the engine asserted it on, the receipt records them, and they must match the physical plan's
  declared checks exactly. A warehouse implementation that attests nothing is refused rather than
  assumed compliant, so this is a breaking change for any caller supplying its own observation.

### Fixed

- The PostgreSQL and ClickHouse warehouses now report the output columns whose decimal magnitude
  the engine was held to, so a live materialization is admitted again. Both already verified the
  bound against the materialized relation and refused a violation, but neither carried that
  result into `MaterializationObservation`, so `magnitude_asserted_columns` stayed empty and the
  refusal added above rejected every materialization through a real warehouse -- the enforcement
  landed without the production side that produces its evidence. The attestation is taken from
  the engine-side result, not from the model's declarations: reading back what was declared would
  attest only that a check was asked for, which is the decoration the enforcement exists to
  remove. Columns are reported only where the observed violation count is zero, so a mistake here
  withholds an attestation and causes a refusal rather than admitting an unchecked result.
- The authoring and context-exposure MCP servers again report *why* they refused a result. Their
  leakage guards raised `RuntimeError`, which `mcp` 2.2.0 classifies as a crash: it replaces the
  message with a bare `Error executing tool <name>` and keeps the reason server-side. They now
  raise `ToolError`, the classification these deliberate refusals always warranted. Nothing about
  what is withheld changed -- the guard still refuses before any value is returned.
