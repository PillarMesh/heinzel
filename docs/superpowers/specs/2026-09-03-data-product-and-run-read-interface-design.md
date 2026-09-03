# Data Product and Run Read Interface Design

- Status: Accepted (both open decisions taken 2026-09-03)
- Date: 2026-09-03
- Governing specifications: Managed Data Engineering Platform Addendum v0.1, Data Architect
  Console Design (2026-09-01)
- Related decisions: ADR-0004 (compilation target), ADR-0005 (data acquisition and load)
- Question: what must an owning service publish before the console can show a tenant its
  data products and the runs that produced them?

## 1. Purpose

The console reserves two routes — `GET /api/v1/data-products/{ref}` and
`GET /api/v1/runs` — and reports both as `not_delivered`, naming its dependency as "a
tenant-scoped data product and run read interface". This document says what that
interface is, and it exists because the gap is not a wiring gap. Two capabilities in the
console turned out to be built and merely uncomposed; this one is not. Nothing in the
estate owns a data product, and no run can be listed for a tenant.

## 2. What the estate holds today

Both claims below were checked against the code rather than inferred from the capability
register.

**A data product is only a reference.** `ArtifactReference` — an artifact id, a digest and
a version — appears in `FulfillmentPolicySnapshot.permitted_data_product_refs`,
`AccessScopePreview.data_product_ref` and the grounding snapshot's
`governed_dataset_refs`. No service stores a data product row. The console's
`DataProductView` projects `data_product_id`, `display_name`, `state`, `summary` and
`version`; only the identifier and the version have an owning field, and the console
already records the same shortfall for `EvidenceContextView.datasets`, which it publishes
empty because an `ArtifactReference` is not a display reference.

**A run is not tenant-scoped.** `contract-service` mints `run-<digest>` and calls
`EvidenceStore.create_run(run_id, activation_key, contract_digest, summary_digest,
signed_graph_json, now)`. `RunRecord` carries `run_id`, `activation_key`,
`contract_digest`, `summary_digest`, `signed_graph_json`, `state`, `checkpoint`,
`batch_id` and timestamps — and no tenant. The store reads runs by `run_id` or by
`activation_key`; neither is a listing, and neither is scoped. A console route that
listed runs today would either list every tenant's runs or invent a scope, and the second
is the console becoming an authority over someone else's records.

## 3. Decisions taken

Both were the user's to take, because each changes what an owning service asserts.
Both were taken on 2026-09-03 and are recorded here as settled rather than open.

1. **Where a data product lives: nowhere new.** The console shows references and never
   names. No new service is introduced and no existing service is asked to assert a name,
   a state or a summary it does not own. Should a service later want to own product
   identity, it can, and this interface widens rather than changes.
2. **How a run acquires a tenant: by derivation.** The tenant is resolved at read time
   through the activation the run belongs to. `RunRecord` is not altered, no digest
   changes, and the append-only evidence store is not migrated.

Section 4 records why each was recommended; sections 5 and 6 are scoped to these answers.

## 4. Why each was decided that way

**Derive, do not store.** A read interface that resolves the tenant through the
contract chain needs no change to `RunRecord`, no re-digesting, and no migration of an
append-only store. It keeps the evidence chain exactly as it was witnessed.

The chain was then traced through the code, and it is not the one first written here.
`ActivationSummary` carries no tenant either, so "through the activation" was wrong. The
tenant hangs off `AcquisitionContractLifecycleState`, which is keyed
`(tenant_id, contract_digest)` — and `RunRecord` carries `contract_digest`. The real
derivation is therefore **run → contract digest → activated contract lifecycle → tenant**,
one hop shorter than described.

What that costs is not "a join". Both stores are keyed for point reads and neither can
enumerate:

- `AcquisitionContractLifecycleRepository` exposes `activate`, `get(tenant_id,
  contract_digest)` and `retire`. It cannot answer "which contracts are activated for this
  tenant", which is the first half of a tenant-scoped listing.
- `SQLiteStore` reads runs by `get_run(run_id)` and `get_run_by_activation(activation_key)`
  and has no listing on any key, including `contract_digest`.

So the decision costs **two additive listing reads, one in each owning service**. Both are
reads over columns that already exist and are already indexed by their primary keys; no
record changes, no digest changes, no migration. That is still far cheaper than re-digesting
an append-only evidence store, so the decision stands — but the cost is two service changes,
not one join, and the implementation plan is scoped to that.

**Publish references, not names, until a service owns names.** `DataProductView` should
carry the identifier, the version and the governed state the owning service can assert,
and should omit `display_name` and `summary` rather than composing them in the console.
That is the same rule the console already follows for `EvidenceContextView.datasets` and
`RequestDetailView.proposal`, and it means the capability can be delivered without first
inventing a data-product service.

## 5. Scope

Included: two additive listing reads in the owning services (activated contracts for a
tenant; runs for a set of contract digests); a tenant-scoped run listing derived through
that chain; a run detail
read; a data-product read limited to what an owning service asserts; the console
projections and routes for both; and the capability register entries that stop reporting
them as undelivered.

Not included: a data-product entity with an owned name and summary; run cancellation,
retry or replay from the console; execution itself; and any claim that a listed run
represents work the platform performed rather than work it recorded.

## 6. Open questions

1. Whether a run whose activation has been invalidated should still list for the tenant,
   and under which state.
2. Whether run listing needs paging before the first tenant has enough runs to need it.
3. Whether `RunState` maps onto the console's `OperationState` without loss, or whether
   the console needs its own run vocabulary.
4. Whether the data-product read should refuse a reference the requesting actor's policy
   snapshot does not permit, or report it as not visible.
