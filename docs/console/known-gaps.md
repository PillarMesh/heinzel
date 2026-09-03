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

### Source acquisition (`source-acquisition`)

Acquisition receipts are produced per run and are not publicly listable per tenant, so
no acquisition operation can be projected or commanded.

### Data products and runs (`data-product-runs`)

No owning service publishes tenant-scoped data products or runs, and the two halves
fail for different reasons, both checked against the code:

- A data product exists only as an `ArtifactReference` — an artifact id, a digest and a
  version — carried by policy snapshots, access previews and grounding snapshots. No
  service stores a data product row, so of the five fields `DataProductView` projects
  only the identifier and the version have an owning field.
- A run is not tenant-scoped. `contract-service` records it through
  `EvidenceStore.create_run`, and `RunRecord` carries `activation_key` and
  `contract_digest` but no tenant; the store reads runs by run id or activation key and
  offers no listing. Listing runs for a tenant today would either cross tenants or make
  the console invent a scope.

`docs/superpowers/specs/2026-09-03-data-product-and-run-read-interface-design.md`
proposes deriving the tenant through the activation chain rather than storing it on an
append-only evidence record, and publishing references rather than composing names.

### Catalog asset preview (`catalog-asset-preview`)

Publication receipts name references but carry no reviewable asset detail.

### Demo reset

`POST /api/v1/demo/reset` is registered only for the fixture backend. Governed-local
state is authoritative and nothing in the console may reset it; the route is absent
(404) in governed mode.

## Contract fields that are missing or unused

These are console contract gaps found while wiring Task 10. Each is worked around in a
way that never invents authority, and each needs a contract change to close.

1. **`DecisionCommand` names no review item.** `SemanticReviewService.decide_item`
   decides one ontology review item. A bundle-wide decision therefore cannot be
   expressed. The console decides the bundle only when exactly one item is still
   pending; a multi-item bundle is refused with `review_item_required` rather than
   fanning one decision across several owning transactions, which could not be applied
   atomically.
2. **`DecisionCommand.decision = "request_changes"` cannot reach semantic review.** The
   owning `revise`/`merge` decisions require replacement wording; the command carries
   none. Refused with `review_revision_content_required`. The remaining `unresolved`
   decision drives the bundle to `no_valid_plan`, which is far stronger than "request
   changes" and must not be substituted for it.
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
