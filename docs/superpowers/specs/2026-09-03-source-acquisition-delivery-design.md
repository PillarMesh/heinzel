# Source Acquisition Delivery Design

- Status: Proposed for review
- Date: 2026-09-03
- Governing specifications: Managed Data Engineering Platform Addendum v0.1, Plan 4A Managed
  Source Acquisition Design (2026-08-31), Data Architect Console Design (2026-09-01)
- Related decisions: ADR-0005 (data acquisition and load)
- Question: what stands between an acceptance-proven acquisition runtime and a platform that
  actually acquires data for a tenant?

## 1. Purpose

The console reserves the `source-acquisition` capability and reports it `not_delivered`,
naming its dependency as "a tenant-scoped acquisition receipt read interface". That
description is wrong, and this document exists to correct it before anyone builds against
it.

A read interface is the last and smallest part. Acquisition is proven by Plan 4A's
acceptance run, and the console can already show governance around it — setup, warehouse,
catalog, meaning review, requests, approvals, admission, and now runs. What it cannot show
is data arriving, because nothing outside a test composes the runtime that would make data
arrive.

## 2. What the estate holds today

Every claim below was checked against the code rather than inferred from the capability
register.

**`AcquisitionRunner` needs nine collaborators**, and they are in three different states.

Durable implementations already exist for three:

- `AcquisitionStateStore` — `SQLiteAcquisitionStateRepository` in `pillarmesh_state`, which
  carries all eight of the protocol's methods.
- `AcquisitionArtifactStore` — `LocalAcquisitionArtifactStore`, likewise.
- At least one real provider — `PostgreSQLAcquisitionProvider`.

`ReferenceFactory` is not among them, though an earlier draft of this document counted it.
`pillarmesh_state` declares the identical alias, `Callable[[str], str]`, and accepts one as a
constructor argument; it publishes none. The only implementation in the repository is
`_ReferenceFactory` in the Plan 4A harness — another test double. It is a small thing to
write, but the plan has to include writing it rather than assume it is already in hand.

One resolver is already satisfiable with no new interface: `BindingResolver` is
`Callable[[str, str], SourceConnectionBinding]`, and connection-broker's repository already
publishes `load(tenant_id, binding_id)` returning exactly that. It has simply never been
handed to the runner.

**Acquisition evidence is produced and then discarded.** `AcquisitionEvidenceWriter` is a
protocol with a single `append(receipt)`. Every implementation of it in the repository is an
in-memory Python list inside a test double — `tests/acceptance/run_plan4a.py`,
`tests/fault-injection/test_source_acquisition_recovery.py`, and
`services/runtime/tests/test_acquisition.py`. There is no durable writer, no table, and no
read path. A receipt built during a run lives until the process exits.

This is the part worth stating plainly: the gap is not that the console cannot list
acquisition receipts. It is that a governed platform whose premise is evidence does not
retain the evidence its acquisition path produces.

**A receipt is already tenant-qualified.** `AcquisitionEvidenceReceipt` carries `tenant_id`,
along with `run_intent_ref`, `contract_ref`, `source_binding_ref`, `acquisition_mode`,
`logical_object_refs`, checkpoint revisions, reason codes, an outcome and `created_at`. Unlike
`RunRecord`, it needs no derivation to be read per tenant. Once it is stored, the read is
almost free.

**Two resolver inputs have no owning publisher.**

- `ContractResolver` returns `ActivatedAcquisitionContract`, a fifteen-field model declared
  *in the runtime itself*. contract-service owns `AcquisitionContractLifecycleState`, which
  carries six — tenant, contract digest, revision, lifecycle state, and two timestamps. Both
  counts exclude `schema_version`, which every artifact model carries.
  It does not carry `object_schemas`, `acquisition_modes`, `record_ceiling`,
  `encoded_byte_ceiling`, `capability_profile_digest`, or the observation references. Nothing
  in the estate assembles the shape the runner requires.
- `ObservationResolver` returns `AcquisitionSourceObservation`, a provider-SDK model. Providers
  produce one; no service stores or serves it.

**Nothing composes acquisition outside tests.** `AcquisitionRunner` is constructed in exactly
two places: `tests/acceptance/run_plan4a.py` and `services/runtime/tests/test_acquisition.py`.
Its four resolvers are bound methods of the acceptance scenario. There is no deployed process,
no service boundary, and no console command that reaches it.

## 3. Decisions this needs, which this document does not take alone

Each changes what a service asserts or owns, so each belongs to the user.

1. **Where acquisition receipts durably live.** The candidates are the evidence service
   alongside runs; the existing artifact store, keyed by reference; or a new acquisition
   service. The first is recommended below.
2. **Who owns `ActivatedAcquisitionContract`.** Either contract-service grows it and publishes
   an activated acquisition contract as a first-class artifact, or a composition layer assembles
   it from the compiler's output and the lifecycle state. This is the largest piece of work in
   the whole capability, and it is a genuine ownership question rather than a mechanical one.
3. **Whether the runtime becomes a service or stays a library.** Today it is a library invoked
   in-process. Making it a deployed service with its own boundary is a larger commitment; keeping
   it a library and composing it where the console already runs is smaller and reversible.

## 4. Recommendation

**Persist receipts in the evidence service, as a table beside runs.** They are evidence, that
service already owns evidence, and it has just grown the tenant-scoped listing pattern this
would follow. Because `AcquisitionEvidenceReceipt` already carries `tenant_id`, the read needs
no derivation through the activation chain — it is a direct filter, unlike the run listing. The
cost is one table, one repository method to append, one to list per tenant, and a durable
`AcquisitionEvidenceWriter` that wraps them. No new service, and no change to any existing
record.

**Keep the runtime library-shaped and compose it in the governed-local harness first.** That
proves the path end-to-end inside the product the user can actually open, and it makes the
capability visible before anyone commits to a service boundary. If the path is right, promoting
it to a service later is a deployment change rather than a redesign. If it is wrong, nothing has
been deployed to unwind.

**Sequence the work so the smallest honest thing ships first.** In order:

1. A durable evidence writer and a tenant-scoped receipt listing. Self-contained, needs no
   decision beyond 3.1, and turns discarded evidence into retained evidence.
2. The console read and capability, which is then nearly free.
3. The binding resolver, wired to connection-broker's existing `load`. No new interface.
4. `ActivatedAcquisitionContract` ownership, which is where the real work and the real decision
   are.
5. Composition in the governed-local harness, ending in a live acquisition the browser can show.

Steps 1 to 3 are deliverable without answering decision 3.2, which is the expensive one. That
matters: it means progress does not block on the hardest question.

## 5. Scope

Included: a durable acquisition evidence writer and its store; a tenant-scoped receipt read; the
console projection, route and capability entry; the binding resolver wiring; and the harness
composition that makes an acquisition observable in the browser.

Not included: destination writes; a deployed acquisition service; scheduling or triggering
acquisition from the console; provider onboarding beyond the PostgreSQL provider that already
exists; and any claim that a listed receipt represents data the platform moved rather than work
it recorded.

## 6. Open questions

1. Whether a receipt for a failed or governed-refusal outcome is listed alongside successful
   ones, or separated — the model carries an `outcome` and `reason_codes` for both.
2. Whether the console may show `logical_object_refs` directly, or whether object references
   need the same reference-not-name treatment `DataProductView` received.
3. Whether receipt retention has a bound, given the append-only posture, or grows without one.
4. Whether the acceptance harness's in-memory writer should be replaced by the durable one, so
   Plan 4A exercises the path the product uses rather than a double.
