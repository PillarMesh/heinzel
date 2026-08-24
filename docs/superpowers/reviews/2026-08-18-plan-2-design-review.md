# Plan 2 Design Review: Catalog and Semantic Formation

Instructions for the agent revising the Plan 2 design and writing its design
specification. Self-contained: every claim below cites a file and section you can read.

**Verdict:** the chosen approach is correct. Revise the design as set out here, resolve
the two decisions in "Blocked on the user", then write the design specification.

## What already exists

Read these before changing anything.

| Artefact | Path |
| --- | --- |
| Product boundary (design authority) | `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md` |
| Plan 1, delivered | `docs/superpowers/plans/2026-08-17-data-architect-mvp-foundation.md` |
| Contracts Plan 2 consumes | `services/warehouse-control`, `services/request-management`, `services/contract` |
| Spec/code conformance suite | `tests/conformance/test_specification_conformance.py` |
| Repository-wide rules | `AGENTS.md` |

`AGENTS.md` governs. This document does not restate it.

## Why the approach is right

Keep the PillarMesh semantic registry as the system of record and use OpenMetadata as the
managed catalog experience. Addendum section 9.2 already assigns "search, browsing, and
catalog presentation" to OpenMetadata and "approved PillarMesh process and contract" to the
PillarMesh semantic registry, and section 9.2 closes with: "PillarMesh never turns an
unreviewed catalog description, inferred lineage edge, or AI suggestion into executable
authority." Making OpenMetadata the system of record would contradict both.

## Required corrections

### 1. Authority resolution is per information kind, not a global ranking

**Problem.** The draft states one total order in which a declared OpenMetadata glossary
(rank 3) outranks the uploaded process package (rank 4), and adds that lower-ranked
evidence "cannot overwrite higher authority". The process package can therefore never
override a catalog glossary term.

**Why it is wrong.** Addendum section 9.2 is a table of authority *per information kind*:

| Information | Authority |
| --- | --- |
| Business meaning and process semantics | Approved business owner |
| Imported glossary/classification | Declared external catalog authority |

Flattening it lets a catalog decide a question the specification assigns to the business
owner.

**Failing scenario to encode as a test.** The process package declares `Refund` an entity
with its own lifecycle. The tenant's pre-existing OpenMetadata glossary, imported from a
legacy system, classifies `Refund` as an attribute of `Invoice`. The draft rule resolves
silently for the catalog. Correct behaviour is an unresolved review question escalated to
the business owner.

**Do.** Model authority as (information kind, source) with precedence *within* kind. The
catalog wins on imported glossary and classification; the process package wins on business
meaning and process semantics; a conflict that crosses kinds escalates to an explicit
owner decision and never auto-resolves. Cite section 9.2 in the design.

### 2. Plan 2 must not silently drop Integration Contract formation

**Problem.** The draft excludes Integration Contract compilation. The merged program
decomposition (`docs/superpowers/plans/2026-08-17-data-architect-mvp-foundation.md:46`)
assigns it to this plan: "Catalog and semantic formation: OpenMetadata provisioning,
process candidate extraction, authority resolution, ontology review, and Integration
Contract formation." No later item picks it up: item 2 is the managed data plane, item 3
is inbox fulfilment. It would be orphaned.

**Do.** Either include it in Plan 2, or amend the decomposition in the same change so the
two documents agree. Do not leave them disagreeing. See "Blocked on the user".

### 3. Specify `CatalogBinding` in the addendum before implementing it

**Problem.** The addendum defines `WarehouseBinding` (section 6.2) and its transition table
(section 6.4.1). There is no `CatalogBinding`, no catalog lifecycle, and no catalog
transition table.

**Why it matters.** Plan 1 invented a binding shape that diverged from section 6.2 and
needed retrospective ratification, then a conformance suite to stop it drifting again.
Do not repeat that order of operations.

**Do.** Add `CatalogBinding`'s field shape and lifecycle to the addendum as part of this
plan, then extend `tests/conformance/test_specification_conformance.py` to pin the new
table and field list against the implementation, exactly as sections 6.2, 6.4.1 and 13.3.1
are pinned today. The draft's testing strategy does not mention the conformance suite;
it must.

### 4. State how review lands in the shipped request contracts

**Problem.** The draft routes decisions through "the architect inbox" without saying how it
meets contracts that are already implemented and conformance-pinned.

**Facts to design against.**

- `InboxRequest.payload` is a two-member discriminated union
  (`services/request-management/src/pillarmesh_request_management/models.py:58`).
  Plan 1 records: later plans widen the union; do not collapse the discriminator into one
  permissive payload.
- Addendum section 13.2 lists the request types. An ontology review bundle is presumably
  "schema or semantic change"; name the type explicitly.
- Addendum section 13.3.1 fixes the transition table, and
  `tests/conformance/test_specification_conformance.py` asserts it equals
  `pillarmesh_request_management.service._TRANSITIONS`. Any lifecycle change must update
  spec, code and conformance test together.
- `DecisionKind` is `approve`, `reject`, `request_changes`
  (`services/request-management/src/pillarmesh_request_management/models.py`).
  The draft's five outcomes — accept, reject, revise, merge,
  unresolved — widen it.

**Do.** State the request type, the exact lifecycle path a review bundle follows
(including that `no_valid_plan` is reachable only from `investigating` per section 13.3.1),
and the `DecisionKind` widening.

### 5. Close two gaps in failure behaviour

**Post-publication catalog drift.** The draft covers observation changes *during* review
only. Addendum section 9.3 requires: "Catalog changes that may alter execution become typed
change requests. Meaning, identity, classification, access, relationship, metric,
constraint, and deprecation changes require impact analysis and approval." Design what
happens when the glossary is edited in OpenMetadata *after* an approved semantic version is
published.

**Teardown discipline.** Plan 2 provisions real infrastructure. The M0 acceptance work
required a private resource ledger recording creation state, retention deadline and cleanup
status so authorized cleanup acts only on exact recorded identifiers. The draft lists
provisioning, suspension and retirement but no cleanup evidence. Add it.

## Smaller corrections

- The flow diagram's fences are nested (a bare fence wrapping a `mermaid` fence). It will
  render as a code block. Use one `mermaid` fence.
- State the identity rule. Plan 1 forbids clock-derived identity: derive from tenant, a
  domain tag, and a repository-assigned sequence, so identities are reproducible and cannot
  collide under a frozen test clock.
- Justify the extractor. Addendum section 8.4 already has the manifest carrying process
  name, owner, participants, outcomes, entities, events, states, rules, source references
  and unresolved questions. Say what deterministic extraction derives from the *narrative*
  that the manifest does not already state; otherwise the candidate and review apparatus
  may be over-built for the MVP.
- The draft's boundary correctly implements only the bundled OpenMetadata path. Keep it
  that way and cite section 9.1: when a supported catalog already exists, PillarMesh
  connects read-only first and "does not also provision OpenMetadata by default."

## Already correct — keep

- Every artefact tenant-scoped, immutable, canonically serialized, digest-addressed, with
  operational secrets excluded.
- Cross-tenant lookup denied before artifact retrieval.
- Extractor rerun creates a new candidate-set revision rather than replacing evidence.
- Explicit `No Valid Plan` when required meaning is unresolved.
- Idempotent publication retry converging without duplicate semantic identities.

## Blocked on the user

Do not decide these alone; both change what the specification says.

1. **Authority model (correction 1).** Confirm that on a business-meaning question the
   uploaded process package outranks an imported catalog glossary, with cross-kind
   conflicts escalating to the owner.
2. **Scope (correction 2).** Include Integration Contract formation in Plan 2, or amend
   the program decomposition to move it.

## Definition of done for the design specification

- Every correction above is addressed or explicitly declined with a reason.
- Every new durable artefact has a stated field shape, lifecycle, and tenant-scoping rule.
- Every artefact shape documented in the addendum has a matching check in
  `tests/conformance/`.
- Each capability has a success case and at least one denial, invalid-input,
  immutable-state, or cross-tenant case, per `AGENTS.md`.
- Spec and code cannot disagree silently: the addendum, the implementation and the
  conformance suite change together.
