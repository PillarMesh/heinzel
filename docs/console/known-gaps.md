# Data architect console — known gaps

This file records what the console does **not** deliver in `governed_local` mode, and
why. Every entry names the owning transaction or contract field that is missing. A gap
is closed only by an owning service publishing the transaction, never by the console
approximating it.

The console reports each of these as a `not_delivered` capability on
`GET /api/v1/workspace`, and the corresponding route fails closed with the error code
`capability_not_delivered`. No governed failure ever falls back to fixture content.

## Commands that are wired

| Console command | Owning transaction |
| --- | --- |
| `POST /api/v1/setup/warehouse-binding` | `WarehouseControlService.create_draft` then `WarehouseLifecycleOrchestrator.provision` |
| `POST /api/v1/requests` | `RequestManagementService.submit_question` / `submit_access_request` |
| `POST /api/v1/requests/{id}/conversation` | `RequestManagementService.append_conversation` |
| `POST /api/v1/requests/{id}/clarified-outcome/acceptance` | `FulfillmentService.record_approval` under the requester principal |
| `POST /api/v1/inbox/{id}/decisions` | `FulfillmentService.record_approval` under the architect authority |
| `POST /api/v1/inbox/{id}/admission` | `FulfillmentService.admit` |
| `POST /api/v1/reviews/{id}/decisions` | `SemanticReviewService.decide_item` |

## Capabilities that remain undelivered

### Business process package submission (`process-package`)

`ProcessPackageCommand` carries `file_name`, `media_type` and `package_digest` and no
document bytes, while `ProcessPackageService.upload` requires the narrative bytes plus
a `BusinessProcessManifest` and accepts only `text/markdown; charset=utf-8`. The
command's media types (`application/pdf`, the Word document type) are not accepted by
that transaction at all. There is nothing to delegate, and constructing a manifest in
console code would make the console the author of a semantic artifact.

### Operation retry (`operation-retry`)

`OperationView` may only advertise `retry` when it carries an `operation_digest` and a
`retry_token`. No owning service issues either. Minting them in the console would make
the console a second replay authority for someone else's transaction, which the
architecture forbids, so `POST /api/v1/operations/{id}/retry` is undelivered. A
warehouse lifecycle that failed transiently is still recoverable: warehouse-control's
own `provision` replay path adopts the live operation and reconciles it.

### Transformation and destination effects

No console command exists and no owning transaction is wired. Compilation, execution
graph admission, and destination writes remain outside this component.

### Superset render, analyst dashboards and analyst embedding (`analyst-dashboard`)

`GET /api/v1/dashboards/{ref}` and `GET /api/v1/previews/{ref}` are undelivered. The
governed Superset embedding surface is a separate, explicit MVP obligation. The
architect surface offers previews and authenticated deep links only, and the deep-link
issuer (`GET /api/v1/links/{ref}`) is itself undelivered because no owning service
issues server-side managed-service links yet.

### Stakeholder answer text delivery

Admission is now wired: `POST /api/v1/inbox/{id}/admission` calls
`FulfillmentService.admit`, which promotes an approved proposal to execution and writes
the admission and evidence receipts, so a console-driven journey reaches `executing`
rather than stopping at *every required approval recorded*.

What remains undelivered is the answer **text**. `RequesterRequestView` carries the
request state, the clarified outcomes, the requester's own decisions and a denial
explanation - it has no field for the proposed answer. After admission the requester
sees `ready_for_execution`, not the answer itself. Publishing the text would need an
owning field to publish; composing one in the console would make it the author of a
governed artifact.

### Access grant application, expiry and revocation

An approved `AccessScopePreview` is a decision record, not an applied grant. Nothing in
the console applies, expires, or revokes a grant, and no owning service publishes those
transactions today.

### Acquisition evidence (`acquisition-evidence`)

**Closed.** Receipts an acquisition records are retained and readable per tenant.

`AcquisitionEvidenceReceipt` already carried `tenant_id`, so unlike a run the read needs
no derivation: `SQLiteStore.list_acquisition_receipts(tenant_id)` is a direct filter, and
the store satisfies the console's reader protocol without an adapter. Evidence schema
version 3 adds the table, guarded by the same append-only triggers the evidence chain
carries. An existing version 2 database is upgraded in place rather than refused, because
version 3 only adds tables and refusing would discard the run evidence the service exists
to retain.

What remains deliberately unasserted:

- A listed receipt records work an acquisition performed. It does not assert that data
  reached a destination; nothing in this estate writes one.
- Refusals are listed beside successes. The receipt carries `outcome` and `reason_codes`
  so a refusal is publishable, and a refusal an operator cannot see is one they cannot
  act on. This answers open question 1 of the design document.
- Every identifier is projected as the reference an owning service allocated, including
  `logical_object_refs`, and the console invents no display name for any of them — the
  rule `DataProductView` follows. This answers open question 2.
- The recovery state — both receipt references and both checkpoint revisions — is not
  projected. It says where the runtime is in its own protocol, which is not something an
  operator reads.
- Retention has no bound. The table is append-only and grows; open question 3 of the
  design document is still open.

### Source acquisition (`source-acquisition`)

Receipts are now retained (see above), but nothing here can perform an acquisition.

`AcquisitionRunner` is constructed only in the Plan 4A acceptance harness and in unit
tests, with its four resolvers bound to the scenario. Three of its nine collaborators have
durable implementations already (`SQLiteAcquisitionStateRepository`,
`LocalAcquisitionArtifactStore` and `PostgreSQLAcquisitionProvider` — not the reference
factory, which exists only as a test double), and a fourth is now real: the durable
`SQLiteAcquisitionEvidenceWriter`. Its `BindingResolver` signature already matches
connection-broker's `load(tenant_id, binding_id)` exactly — it has simply never been handed
over. What has no owning publisher is `ActivatedAcquisitionContract`, a fifteen-field model
declared in the runtime itself against contract-service's six-field lifecycle state.

`docs/superpowers/specs/2026-09-03-source-acquisition-delivery-design.md` measures all of
this and sequences the remaining work.

### Data products and runs (`data-product-runs`)

**Closed.** Both halves are delivered; this entry is kept because what the capability
does *not* assert is still a live constraint.

The tenant is derived, never stored. `AcquisitionContractLifecycleRepository.
list_activated(tenant_id)` names the contracts a tenant has activated, and
`SQLiteStore.list_runs_for_contracts(digests)` names the runs witnessed under them.
`RunRecord` is unchanged, no digest moved, and the append-only evidence store was not
migrated. A tenant with no activated contracts yields no digests and therefore no runs,
which is the boundary the derivation rests on.

What remains deliberately unasserted:

- A data product is still only an `ArtifactReference`. `DataProductView` carries the
  identifier, the version and the digest, and omits a display name and a summary,
  because no owning service stores either. The console shows the newest permitted
  version of a reference, and its scope is what the tenant's own policy snapshots
  permit.
- `RunView` carries the evidence store's own state vocabulary rather than the shared
  `OperationState`. The two do not map without loss: `non_conforming` is a witnessed
  outcome, and `outcome_unknown` would report it as ignorance. This answers open
  question 3 of the design document with evidence rather than with a choice.
- The projected timestamps are the record's own `created_at` and `updated_at`. They are
  not renamed to `started_at` and `completed_at`, which would assert a lifecycle meaning
  the stored fields do not carry.

`docs/superpowers/specs/2026-09-03-data-product-and-run-read-interface-design.md`
records the decisions and the measured cost.

### Catalog asset preview (`catalog-asset-preview`)

Publication receipts name references but carry no reviewable asset detail.

### Demo reset

`POST /api/v1/demo/reset` is registered only for the fixture backend. Governed-local
state is authoritative and nothing in the console may reset it; the route is absent
(404) in governed mode.

## Contract fields that are missing or unused

These are console contract gaps found while wiring Task 10. Each is worked around in a
way that never invents authority, and each needs a contract change to close.

1. **Closed.** `DecisionCommand` names a review item. `SemanticReviewService.decide_item`
   has always decided one ontology review item and the command could not say which, so
   a bundle with several undecided items was refused outright and an architect could
   decide none of them here. `review_item_id` carries it now. A bundle-wide decision is
   still not expressible, and is still not attempted: fanning one decision across
   several owning transactions could not be applied atomically. Naming an item that is
   absent or already decided is refused rather than resolved to the nearest pending
   one.
2. **Closed.** `request_changes` reaches semantic review as the owning `revise`
   decision, carrying the replacement wording that decision requires in
   `revised_content`. A change request without wording is still refused, because there
   is nothing to revise with; `unresolved` is still never substituted, because it
   drives the bundle to `no_valid_plan`, a far stronger verdict than asking for a
   change. Wording sent with an approval or a rejection is refused rather than dropped.
   `merge` remains unreachable: it needs candidate identifiers the command does not
   carry.
3. **`CreateRequestCommand.title` has no owning field.** Request-management stores no
   title, so the console returns the title derived from the owning payload (the
   question text or the data product reference) rather than echoing the browser's.
4. **`CreateRequestCommand.request_digest` is not verified.** No owning service defines
   a canonicalization for the submitted request content, so the console cannot check
   the declared digest without inventing one. Replay safety for intake comes from
   request-management minting a fresh identity per submission.
5. **`ConversationMessageView.author_role` has no owning source.** Request-management
   records an actor, not a role. The console labels the requester's own entries
   `requester` and every other entry `data_architect`. This is a presentation
   classification and carries no service authority. `author_label` is the owning
   actor identifier, because no display-name directory exists.
6. **`RequestDetailView.proposal` is never populated.** The console vocabulary for a
   proposal (`metric_version`, `datasets`, and similar) has no mapping from the
   owning `ArtifactReference` values, and guessing one would put unverified strings in
   front of a decision. The console publishes `proposal_digest` — the exact subject the
   architect's authority must sign — plus counts in `evidence`, and leaves `proposal`
   absent.
7. **`EvidenceContextView.datasets` is always empty** for the same reason: an
   `ArtifactReference` is not a `PublicId` display reference.
8. **`ClarifiedOutcomeView.revision` is the request's live revision**, not the revision
   the statement was drafted at. The acceptance command is compared against the live
   request revision by the fulfillment service, so publishing the drafting revision
   would make every acceptance stale on arrival.

## Composition defects found while wiring

1. **Two capabilities were reported as unwired rather than undelivered.** The console
   read `catalog-binding` and `semantic-review` as `not_delivered` because the
   governed harness composed neither service, not because either was missing.
   `CatalogControlBindingReader` had existed since Plan 2, and both semantic-review
   seams existed on the governed backend. Both are composed now and both report what
   their owning service says. A capability that is merely unwired must not be
   presented as one the platform does not have.

2. **A workspace principal directory is console-held configuration.** The owning
   services validate authority but publish no reverse lookup from an actor to the
   authority reference it holds, because enumerating principals would itself be a
   disclosure. `InMemoryWorkspacePrincipalDirectory` therefore holds the deployment's
   mapping, and the owning service still performs the authority check.

## Composition defects that have since been closed

These were recorded as open while the console was being wired and are fixed. They are
kept here so a reader of the history is not left believing the console still carries
them.

1. **The setup digest was unstable.** `setup_snapshot_digest` covered `reset_token`,
   which `GET /api/v1/setup` mints fresh on every read, so two consecutive reads
   returned different `setup_digest` values and no command could ever have been guarded
   by the setup snapshot. The digest now excludes `reset_token`, which is an
   authorization credential rather than a description of state, so it identifies the
   snapshot it is supposed to identify.
2. **`SQLiteWarehouseRepository` could not borrow a connection.** Command routes run the
   backend in a threadpool while SQLite connections carry thread affinity, and only
   `SQLiteRequestRepository` accepted an injected connection, so a governed deployment
   had no way to supply a thread-tolerant one to warehouse-control and every warehouse
   command over HTTP failed as `downstream_unavailable`. The repository now takes either
   a `database_path` or a `connection` — exactly one — and closes only the connection it
   opened itself.
3. **Governed reads were not role-gated the way the demo is.** `FixtureConsoleBackend`
   authorizes every read against an explicit role set; `GovernedConsoleBackend`
   authorized only `get_request_detail`, `get_runs` and `get_acquisition_receipts`, so
   governed-local mode was more permissive than the demo standing in for it. Measured
   over HTTP against the governed deployment: with the requester actor header,
   `GET /api/v1/setup` and `GET /api/v1/inbox` both answered `200`, and
   `GET /api/v1/requests/mine` answered the architect `200` with an empty list. The six
   reads that differed — `get_setup`, `get_inbox`, `get_review`,
   `get_requester_requests`, `get_conversation` and `get_clarified_outcome` — now
   authorize against the role set the fixture backend declares for the same read:

   | Read | Role set |
   | --- | --- |
   | `get_setup` | `data_architect` |
   | `get_inbox`, `get_review` | `data_architect`, `data_owner`, `policy_approver`, `budget_approver` |
   | `get_conversation` | `requester`, `data_architect`, `data_owner`, `policy_approver` |
   | `get_clarified_outcome` | `requester`, `data_architect` |
   | `get_requester_requests` | `requester` |

   The requester keeps the three reads that are their own surface, so the fix is not
   "architect only" everywhere. Each check runs *before* the read's delivery check, so an
   unauthorized actor cannot learn from a `not_delivered` answer which capabilities this
   deployment has wired.

   The rest were left ungated for two different reasons, which an earlier draft of this
   entry ran together. `get_data_product`, `get_evidence` and `get_operation` need no gate
   because the fixture role sets there admit every role. `get_preview`, `get_catalog_asset`
   and `get_dashboard` restrict to three roles in the demo, but the governed reads raise
   `not_delivered` unconditionally, so there is no answer to gate; they need a role set the
   day they serve one.

   One asymmetry remains, in the other direction. `get_request_detail` authorizes
   `data_architect` alone while the demo admits the three approving authorities as well, so
   a `data_owner` can list the decision queue and is refused when opening an item from it.
   That fails closed, so it is left as it stands rather than widened here: relaxing an
   authorization is a decision about who may read an architect's decision, not a
   consistency edit.
