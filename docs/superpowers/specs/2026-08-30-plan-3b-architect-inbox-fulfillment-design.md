# Plan 3B Architect Inbox Fulfillment Design

**Status:** proposed for review

**Date:** 2026-08-30

**Authority:** This design refines sections 13, 14, 18, 20, and 21 of
`docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`.
The addendum remains normative. Any field shape or decision table introduced here must be ratified
in the addendum and pinned by conformance tests in the implementation change.

## 1. Problem

Plan 1 created a durable typed inbox. Plan 2 created approved semantic versions, catalog
publication receipts, authority observations, and semantic-review approval bindings. Plan 3A
created managed PostgreSQL and ClickHouse warehouse lifecycles. These capabilities do not yet let
the data engineering architect process the two most common stakeholder requests safely:

1. prepare a governed answer from approved assets; and
2. prepare the least-privilege access scope required for a stated purpose.

The existing request service can move a ticket through states and record a decision against an
opaque digest. It cannot prove what was proposed, which immutable catalog and semantic versions
grounded it, why a particular authority was required, whether every cited asset was in scope, or
whether the requester was allowed to see the candidate. A permissive implementation would turn the
inbox into an unrestricted query or access-grant surface and would make an AI-generated candidate
look authoritative.

## 2. User and outcome

The primary user is a one-person data engineering architecture team operating a PillarMesh-managed
environment. The architect needs one review surface that answers:

- what the requester asked and for what purpose;
- which approved data products, metrics, classifications, lineage, freshness, and quality
  observations support the proposal;
- what the requester is authorized to receive;
- which independent authority roles must approve the exact proposal;
- what dependency blocks an answer when governed assets are missing; and
- whether the proposal is merely a candidate, approved for execution, or actually delivered.

Plan 3B succeeds when every acceptance request deterministically reaches exactly one of these
outcomes without direct catalog or warehouse administration:

- an immutable proposal awaiting exact approvals;
- an approved execution-ready handoff;
- a typed dependent request for missing governed capability;
- a policy-grounded denial proposal; or
- `No Valid Plan` with attributable constraints.

The primary release metric is that all committed Plan 3B acceptance fixtures reach one of those
outcomes while zero unapproved answer text or access scope is exposed through the requester-facing
read boundary.

### 2.1 User stories

- As a data engineering architect, I want a proposed answer with exact governed support and
  limitations so that I can approve a stakeholder response without manually reconciling catalog
  screens.
- As a data engineering architect, I want an effective-scope preview and explicit exclusions so
  that I can see whether an access request is truly least privilege.
- As a data owner or policy authority, I want my decision bound to one exact proposal and role so
  that a later edit cannot reuse my approval.
- As a requester, I want a clear approved outcome or clarification without seeing internal drafts,
  restricted metadata, or another tenant's information.
- As a platform operator, I want missing capability, denied disclosure, and invalid authority to
  remain distinct so that automation cannot hide a policy or semantic failure.

### 2.2 Priority and effort

Plan 3B is a P1 next-milestone capability. It is high impact and high effort because it establishes
the approval and privacy boundary every later query and grant executor must consume. The design is
one architectural slice, while the implementation should be delivered as the reviewable tasks in
section 16 rather than one change. The smallest shippable increment is an approved semantic-answer
handoff plus requester-view denial; access preview builds on the same committed artifact and
transaction boundaries before Plan 3B is declared complete.

## 3. Why now

The required upstream authorities are committed on `main`:

- Plan 1 owns tenant-scoped requests, conversations, state transitions, and append-only decisions;
- Plan 2 owns approved semantic versions, authority-role checks, managed OpenMetadata publication,
  and round-trip publication receipts; and
- Plan 3A owns immutable warehouse bindings and separated warehouse principal classes.

The addendum's delivery sequence places architect-inbox proposals and approval binding before
source acquisition, destination data movement, query execution, and grant application. Plan 3B
therefore defines the control-plane handoff those later capabilities must consume rather than
pulling their effects forward.

## 4. Approved design decisions

The following decisions were approved in the design conversation:

1. Stakeholder-answer proposals and access-scope previews ship together because they share one
   inbox lifecycle, authority boundary, dependency model, and immutable approval mechanism.
2. Generated candidates are visible only to the data engineering architect and required approvers.
   Requesters may see clarification conversations, denial outcomes after approval, and later
   verified delivery, but never an unapproved candidate.
3. Every proposal binds immutable, already-published Plan 2 semantic and catalog receipts. Live
   OpenMetadata discovery may identify candidates but can never change a proposal under review.
4. Missing governed data or meaning creates a typed dependent data-product or semantic-change
   request and leaves the parent under investigation.
5. Insufficient authorization creates a denial proposal. PillarMesh never creates a request for
   broader access on the requester's behalf.
6. Contradictory, stale, unverifiable, or tenant-inconsistent authority produces `No Valid Plan`.
7. SQL execution, warehouse grants, delivery, grant verification, expiry, and revocation remain
   later plans. An approved Plan 3B proposal is execution-ready, not evidence of an effect.

## 5. Scope

### 5.1 Included

- immutable grounding snapshots over approved semantic and catalog publication receipts;
- non-authoritative stakeholder-answer candidates with complete governed citations;
- deterministic least-privilege access previews;
- policy-grounded denial proposals;
- typed required-approval sets and role-scoped decision bindings;
- material-edit invalidation and proposal revision history;
- requester-safe and reviewer-safe read models;
- typed parent-to-dependent-request edges for missing semantic or data-product capability;
- atomic approval admission into an execution-ready handoff;
- fail-closed `No Valid Plan` records;
- privacy-safe evidence receipts containing digests and reason codes rather than answer text; and
- one end-to-end control-plane acceptance journey for each request type.

### 5.2 Out of scope

- natural-language-to-SQL, SQL generation, SQL execution, or query-result validation;
- PostgreSQL or Stripe source acquisition;
- warehouse role creation, `GRANT` or `REVOKE`, access probes, expiry, or revocation;
- answer or report delivery, email, Slack, Superset, or console UI;
- unrestricted raw values, row samples, query results, or credentials in control-plane artifacts;
- AI implementations, prompt design, or AI authority;
- arbitrary user-authored approval workflows or dependency DAGs;
- live catalog state as approval authority;
- changes to compiler legality rules; and
- new top-level services or repository areas.

## 6. Architecture

### 6.1 Placement

`services/request-management` remains the owner of inbox fulfillment. It gains cohesive modules
rather than expanding `RequestManagementService` into a single orchestration class:

```text
services/request-management/
  models.py                  existing request and conversation contracts
  service.py                 existing intake and lifecycle service
  fulfillment_models.py      snapshots, proposals, requirements, approvals, admissions
  fulfillment_protocols.py   injected authority, policy, candidate, and snapshot boundaries
  fulfillment_policy.py      deterministic requirement and denial compiler
  fulfillment_repository.py  append-only SQLite persistence and atomic admissions
  fulfillment_service.py     proposal, dependency, approval, and handoff orchestration
  requester_view.py          fail-closed requester/reviewer projections
```

One prerequisite is not optional. Section 10 requires the proposal, the request revision, and the
transition event to commit together, but `SQLiteRequestRepository.__init__` takes a database path
and opens its own connection, and two connections to one SQLite file cannot share a transaction.
The constructor must first be changed to accept an injected `sqlite3.Connection`, with the
path-taking form retained as a thin classmethod so existing callers keep working. That refactor
touches **42 construction sites across 9 files**
(`services/request-management/tests/test_service.py`, `test_conversation.py`,
`test_semantic_review.py`, `services/semantic-registry/tests/test_publication.py`, `test_review.py`,
`tests/acceptance/plan2_orchestration.py`, `tests/acceptance/run_plan2.py`,
`tests/end-to-end/test_data_architect_foundation.py`, and
`tests/integration/test_openmetadata_publication_live.py`). It is the first implementation task, not
a detail of the persistence task, and it must land with no behavior change and the existing Plan 1
and Plan 2 suites green.

`services/semantic-registry` already depends on `request-management`; reversing that dependency
would create a cycle. It therefore provides an adapter that loads exact Plan 2 semantic and catalog
publication records and constructs the narrow grounding input defined by `request-management`:

```text
services/semantic-registry/
  fulfillment_adapter.py
```

The adapter may query Plan 2 repositories. It may not send private OpenMetadata identifiers,
provider responses, or live mutable catalog objects across the boundary.

`FulfillmentEvidenceReceipt` is defined in `request-management` beside the outcomes that produce it,
and `services/evidence` gains `pillarmesh-request-management` as a dependency to package it.
`services/evidence` currently depends on compiler, contract-model, execution-graph, iir, and
provider-sdk, so this is a new edge and must be declared in `services/evidence/pyproject.toml` in
the same change. The alternative, moving the receipt into `packages/contract-model`, is rejected:
one consumer does not meet the two-consumer promotion rule, and the receipt's shape is owned by the
lifecycle that emits it.

No new shared package is justified. There is one consumer of this fulfillment model.

### 6.2 Component responsibilities

`FulfillmentSnapshotResolver`
: Resolves exact persisted Plan 2 inputs and approved policy inputs into strict grounding and policy
  snapshots or a closed resolution failure. It never treats current provider state as authority.

`AnswerCandidateProvider`
: Optionally prepares non-authoritative answer text from the admitted snapshot. Plan 3B supplies a
  deterministic fixture implementation only. The fulfillment service independently validates every
  cited reference and classification.

`FulfillmentPolicyCompiler`
: Pure deterministic logic that compiles authorization disposition, effective access scope,
  exclusions, and required authority roles from the request and approved policy snapshot. It cannot
  grant access or approve its own output.

`FulfillmentService`
: Coordinates proposal creation, dependencies, approval decisions, material revisions, denial, and
  execution-ready admission. It owns no provider SDK and performs no data-plane effect.

`FulfillmentRepository`
: Persists the snapshot, proposal, approval, admission, dependency edge, and associated request
  revision in transactions that preserve replay and compare-and-swap semantics.

`FulfillmentReadService`
: Produces role-aware projections. Reviewer views may include candidate content; requester views do
  not include a candidate until a later verified-delivery receipt exists.

## 7. Durable contracts

All models are frozen, reject unknown fields, use timezone-aware UTC timestamps, and carry
tenant-qualified identities. Every digest is lowercase SHA-256 over canonical serialization.
Sequence allocation, never a clock or random value, contributes to durable IDs.

Revision fields have one meaning throughout this design:

- a proposal's `request_revision` is the resulting `proposed` request revision written with it;
- an approval's `request_revision` is the current `awaiting_approval` revision it authorizes;
- an admission or denial disposition names both the authorized `source_request_revision` and the
  `resulting_request_revision`; and
- a dependency edge names the unchanged investigating parent revision and the newly created child
  revision.

### 7.1 Grounding snapshot

```text
FulfillmentGroundingSnapshot
  schema_version = "1"
  snapshot_id
  tenant_id
  catalog_publication_id
  catalog_publication_intent_digest
  catalog_round_trip_observation_digest
  semantic_version_ref
  integration_contract_ref
  process_package_ref
  governed_dataset_refs
  metric_refs
  classification_refs
  lineage_refs
  freshness_observation_ref | null
  quality_observation_refs
  authorization_policy_ref
  data_observation_refs
  as_of
  created_at
```

The semantic version, integration contract, process package, and every governed reference must be
reachable from the exact persisted publication intent and receipt. `data_observation_refs` and the
freshness and quality references are interfaces for later execution plans. Their absence may still
support a semantic-definition answer, but it cannot support a factual data answer.

The adapter verifies round-trip publication, approved semantic status, contract formation status,
tenant ownership, reference reachability, and exact digests before returning a snapshot. A snapshot
is immutable. Provider IDs and live OpenMetadata object payloads remain private to the adapter.

The resolver also returns the exact authorization input:

```text
FulfillmentPolicySnapshot
  schema_version = "1"
  snapshot_id
  tenant_id
  requester_id
  requester_principal_ref
  purpose_digest
  approved_policy_refs
  entitlement_observation_refs
  classification_rule_refs
  permitted_data_product_refs
  permitted_access_modes
  maximum_expiry | null
  policy_authority_classifications
  observed_at
  valid_until
```

The snapshot is built only from approved tenant policy and current entitlement observations. The
purpose itself remains private request content; `purpose_digest` is tenant-private and must not be
exported in public evidence. Missing, expired, conflicting, or cross-tenant policy input is a
closed resolution failure, never an empty allowlist that callers may override.

### 7.2 Proposal subject

`FulfillmentProposal` wraps exactly one discriminated subject:

```text
FulfillmentProposal
  schema_version = "1"
  proposal_id
  tenant_id
  request_id
  request_revision
  revision
  prior_proposal_digest | null
  clarified_outcome_digest
  grounding_snapshot_digest
  policy_snapshot_digest
  subject
  required_approvals
  created_at

StakeholderAnswerDraft
  subject_kind = "stakeholder_answer"
  answer_text
  governed_dataset_refs
  metric_refs
  as_of
  freshness_disposition = current | stale | unknown | not_applicable
  material_quality_limitations
  lineage_refs
  disclosure_classifications

AccessScopePreview
  subject_kind = "access_scope"
  requester_principal_ref
  data_product_ref
  access_mode = query | dashboard | export
  requested_fields
  effective_object_refs
  effective_fields
  excluded_scopes
  classifications
  expires_at

DisclosureDenial
  subject_kind = "disclosure_denial"
  reason_code
  requester_safe_explanation
  denied_scope_digest
```

`answer_text` is private request content, not evidence content. It may contain approved aggregate or
semantic values intended for the requester, but never raw rows, credentials, provider diagnostics,
or unrestricted query output. Every dataset, metric, lineage, classification, freshness, and
quality reference in the draft must appear in the grounding snapshot. A factual answer requires at
least one data observation and a non-unknown freshness disposition.

`freshness_disposition` is derived, never asserted. `current` and `stale` require
`freshness_observation_ref` to be present in the grounding snapshot, and the value must be computed
from that observation against the data product's declared freshness objective; a candidate that
claims `current` with a null observation is rejected as an invalid citation, exactly like a metric
reference that is absent from the snapshot. `unknown` is the only disposition permitted when the
observation is null, and it cannot support a factual answer. A semantic-definition answer may use
`not_applicable` freshness and no data observation.

An access preview may narrow the request but never widen it. Effective fields must be a subset of
requested fields, effective objects must belong to the requested data product, expiry cannot exceed
the request, and `excluded_scopes` must explicitly include the governed surfaces that remain denied.
No warehouse role name, credential, endpoint, or provider identifier is part of the preview.

A binding carries `subject_digest` as well as `proposal_digest` because admission matches on it:
the requirement names what was approved, and a requester's requirement names the clarified outcome
statement rather than the candidate subject, so the two digests differ within one proposal.

`ApprovalRequirement.subject_digest` is the tenant-private digest of the discriminated subject,
not the digest of the containing proposal. This avoids a self-reference because the proposal also
contains its requirements. `proposal_digest` is computed only after the complete proposal exists.
Neither digest is exported outside the tenant evidence boundary.

### 7.3 Approval requirements and bindings

```text
ApprovalRequirement
  authority_ref
  reason_code
  subject_digest

FulfillmentApprovalBinding
  approval_id
  tenant_id
  request_id
  request_revision
  proposal_id
  proposal_revision
  proposal_digest
  subject_digest
  actor_id
  authority_ref
  decision = approve | reject | request_changes
  created_at
```

`FulfillmentApprovalBinding` does not replace the existing `DecisionBinding`. The two coexist with a
hard boundary: `DecisionBinding`, written through `RequestManagementService.record_decision`,
remains the Plan 2 semantic-review record and keeps its current callers in
`services/semantic-registry/publication.py` and `review.py`. It is never read as authority for a
Plan 3B proposal, and `record_decision` must refuse a request whose current revision carries a
fulfillment proposal, so an approval cannot be recorded against the wrong table. Conversely a
`FulfillmentApprovalBinding` never satisfies a Plan 2 semantic review. Admission counts fulfillment
bindings only; a `DecisionBinding` on the same request neither satisfies nor blocks a requirement.
The requester projection in section 8 shows the union of both records that name the requester as
actor, labelled by which lifecycle produced them, so "own decisions" is unambiguous.

The deterministic policy compiler emits a sorted, unique requirement for each authority role. A
single decision satisfies exactly one requirement. One person may hold several tenant roles, but
must make a distinct attributable decision for each `authority_ref`; evidence remains role-scoped.
The actor must hold the role at decision time, and approval admission revalidates that role before
the proposal becomes execution-ready.

Every answer requires `role:data_engineering_architect`. Classified disclosure adds the applicable
`role:policy_authority`; a material semantic exception is not approvable in this plan and instead
creates a semantic dependency. Every access preview requires the exact data-product-owner authority
bound by the approved semantic version. Classified, finance, residency, retention, masking, or
widened-access implications add `role:policy_authority`. A configured material-cost requirement is
out of scope because Plan 3B does not execute or price effects.

An answer deliberately does not require the data-product owner, and the asymmetry is intentional.
Addendum section 14 assigns the data owner the decisions that *change* a product - keys, joins,
lifecycle, history, deletion, and metrics - and an answer changes none of them: it restates values
the owner already approved, at a metric version the owner already bound, to a requester the policy
snapshot already entitles. An access preview is different in kind because it creates standing
capability against the owner's product for a principal the owner has not previously admitted, which
is why it binds the owner every time. If a tenant wants owner review of disclosure as well, that is
a policy decision to ratify in section 14 rather than a default of this design.

Addendum section 14 assigns "clarified outcome and acceptance criteria" to the requester, and
section 20.7 includes requester outcome approval in the MVP. That authority is required here, not
deferred - but it binds what was asked, never what was drafted:

```text
ClarifiedOutcomeStatement
  schema_version = "1"
  statement_id
  tenant_id
  request_id
  request_revision
  restated_request
  purpose_digest
  in_scope_summary
  out_of_scope_summary
  created_at
```

The statement is written during investigation, before any candidate exists, and contains no answer
text, effective scope, object list, classification detail, or private digest, so it is
requester-safe by construction. Every proposal carries `clarified_outcome_digest` and every
requirement set includes one requirement whose `authority_ref` is the requesting principal and whose
`subject_digest` is the statement digest rather than the candidate subject digest. The requester
therefore accepts the restated question and its boundaries, and still cannot see the proposal.

A material edit to the candidate does not invalidate the acceptance; a change to the restated
request, purpose, or scope summaries creates a new statement and requires fresh acceptance. What
remains deferred to delivery is *outcome* acceptance - the requester confirming the delivered answer
met the need - which is a different decision and cannot substitute for architect, data-owner, or
policy approval here.

A rejection moves the request to `rejected`. `request_changes` returns the request to
`investigating`; the next material proposal has a new revision and prior digest. Existing approvals
remain in history but cannot satisfy the new proposal.

### 7.4 Execution-ready admission

```text
FulfillmentAdmissionReceipt
  admission_id
  tenant_id
  request_id
  source_request_revision
  resulting_request_revision
  proposal_id
  proposal_revision
  proposal_digest
  grounding_snapshot_digest
  policy_snapshot_digest
  approval_ids
  admitted_at
  execution_status = ready_for_execution
```

Admission verifies, as one predicate over current state:

1. the request is `awaiting_approval` and the proposal is its latest revision;
2. the request, proposal, snapshots, and every binding carry the same `tenant_id`;
3. the grounding and policy snapshots still match their immutable digests;
4. `policy_snapshot.valid_until` is still in the future at the admission clock;
5. for every requirement, exactly one binding exists whose `authority_ref` equals the
   requirement's `authority_ref`, whose `subject_digest` equals the requirement's `subject_digest`,
   whose `proposal_revision` equals the proposal's current revision, and whose `decision` is
   `approve`;
6. no binding against the current proposal revision carries `reject` or `request_changes`; and
7. the actor of each binding still holds that `authority_ref`.

Matching on `authority_ref` alone is not sufficient: without the `subject_digest` and
`proposal_revision` terms, a binding recorded against an earlier revision or a different subject
would satisfy a requirement it never saw. That predicate is the authority boundary of this plan and
is stated here so it cannot be re-derived loosely in code.

Digest equality proves a snapshot has not changed; it does not prove the snapshot is still current.
Condition 4 exists because entitlement observations expire: a proposal whose approvals complete
after `policy_snapshot.valid_until` would otherwise admit on stale authorization. An expired policy
snapshot is not an error and not a denial. The service re-resolves the policy snapshot; if the new
snapshot is canonically equal apart from `observed_at` and `valid_until`, admission proceeds against
it, and if the authorization disposition or effective scope differs at all, the proposal is
superseded by a new revision whose approvals must be recollected. If the snapshot cannot be
re-resolved, the request reaches `No Valid Plan`.

`admission_id` derives from `{domain, tenant_id, sequence}` using a repository-allocated sequence,
as every durable identity in this repository does; no clock or random value contributes. The
admission identity for replay purposes is `(tenant_id, request_id, source_request_revision)`. Replay
under that identity returns the exact existing receipt only after verifying canonical equality of
the stored receipt with the one that would be written now; a stored receipt naming a different
`proposal_digest` or `proposal_revision` under the same identity is an integrity error, never an
overwrite.

The receipt proves only that execution is authorized. It does not prove query execution, grant
application, verification, delivery, freshness, or access.

### 7.5 Denial disposition

Approving a `DisclosureDenial` does not create an execution admission. It creates a separate
requester-safe disposition:

```text
DenialDispositionReceipt
  disposition_id
  tenant_id
  request_id
  source_request_revision
  resulting_request_revision
  proposal_id
  proposal_revision
  proposal_digest
  policy_snapshot_digest
  approval_ids
  requester_safe_explanation
  recorded_at
```

The receipt and transition to `rejected` are one transaction. Only the requester-safe explanation
becomes visible; policy internals, denied fields, candidate text, private digests, and authority
observations remain hidden. Approving a denial subject means approving that disposition. Rejecting
the proposal means rejecting the entire request; requesting changes returns it to investigation.

### 7.6 Dependencies

Plan 3B adds one typed request payload for a missing governed product capability:

```text
DataProductChangeRequest
  request_type = "data_product_change"
  purpose
  requested_outcome
  missing_capability_refs
  source_request_id
  source_request_revision
```

The existing `SchemaSemanticChangeRequest` remains the semantic dependency type.

```text
RequestDependency
  dependency_id
  tenant_id
  parent_request_id
  parent_request_revision
  child_request_id
  child_request_revision
  kind = semantic_change | data_product_change
  reason_code
  blocking = true
  created_at
```

Dependency creation stores the child request and edge atomically. Parent and child must belong to
the same tenant and must be distinct. Only the two dependency kinds above are admitted. Duplicate
active edges and cycles are rejected. A dependency is a blocking reference, not an execution edge:
completion never runs the parent automatically. The parent remains `investigating` and must be
re-evaluated against a newly resolved immutable snapshot.

### 7.7 No Valid Plan

```text
RequestNoValidPlan
  record_id
  tenant_id
  request_id
  source_request_revision
  resulting_request_revision
  reason_codes
  constraint_refs
  smallest_changes
  grounding_snapshot_digest | null
  policy_snapshot_digest | null
  created_at
```

`No Valid Plan` is required for conflicting authorities, stale mandatory authority observations,
unverifiable publication lineage, a current-tenant request that references an asset not admitted to
that tenant, malformed or contradictory policy, and any proposal whose legality cannot be decided
from approved inputs. Recording the artifact and transitioning the request to `no_valid_plan` are
one transaction. Error text is sanitized and contains no candidate answer, provider response,
private identifier, request content, or confirmation that a referenced asset exists elsewhere.

### 7.8 Public evidence receipt

Tenant-private proposal and purpose digests are unsuitable for export. Plan 3B therefore emits a
separate allowlisted receipt:

```text
FulfillmentEvidenceReceipt
  schema_version = "1"
  evidence_id
  tenant_id
  request_id
  request_revision
  outcome = execution_ready | denial | dependency | no_valid_plan | cancelled
  proposal_id | null
  proposal_revision | null
  dependency_id | null
  authority_refs
  approval_ids
  reason_codes
  resulting_state
  created_at
```

The receipt is created in the same transaction as its outcome. It contains no answer text, purpose,
field list, object scope, classification detail, private digest, policy observation, provider ID,
or credential. The evidence service may package this exact model without inspecting private
fulfillment tables.

## 8. State and visibility rules

Plan 3B adds no states and changes no edges. The ratified table in addendum section 13.3.1 stands
in full:

```text
submitted         → clarifying, investigating
clarifying        → investigating, submitted
investigating     → proposed, no_valid_plan
proposed          → awaiting_approval, investigating
awaiting_approval → executing, rejected, investigating
executing         → verifying, failed
verifying         → delivered, failed
delivered         → monitoring, retired
monitoring        → retired
```

Any non-terminal state may also move to `cancelled`. Plan 3B drives only the first five rows; the
rest belong to later plans and are reproduced here so no conformance fixture derived from this
design can pin a truncated table.

`clarifying` is where the clarified outcome statement of section 7.3 is settled. A request enters it
from `submitted` when the restated request, purpose, or scope cannot be fixed from the submission
alone, and the architect returns it to `investigating` once the statement is written, or back to
`submitted` when the requester must re-scope. No proposal may be created from `clarifying`:
grounding resolution starts only in `investigating`, so a candidate can never be built against a
question still being negotiated. Requester clarification conversations are readable in every state;
the `clarifying` state marks that the request is blocked on one.

Cancellation is terminal and takes precedence over any open proposal. Cancelling a request with a
proposal awaiting approval records no admission and no denial disposition: the proposal and its
bindings remain in history as an abandoned revision, the required approvals are neither satisfied
nor rejected, and a `FulfillmentEvidenceReceipt` records the `cancelled` outcome. Nothing may be
admitted against a cancelled request.

The proposal is created atomically with `investigating -> proposed`. Submitting it for review moves
the request to `awaiting_approval`. Exact approval admission moves it to `executing`. Later plans
own `executing -> verifying -> delivered` after real effects and intended-plus-denied verification.

Visibility is independent of storage:

| Actor view | Clarifications | Clarified outcome statement | Candidate subject | Approval metadata | Admission receipt |
| --- | --- | --- | --- | --- | --- |
| Requester | Own request conversation | Yes, and must accept it | Never before verified delivery | Own decisions only, from both records, labelled by lifecycle | Status only |
| Data engineering architect | Yes | Yes | Yes | Yes | Yes |
| Required approver | Relevant conversation | Yes | Exact subject requiring role | Relevant role | Status |
| Unrelated tenant actor | No | No | No | No | No |

An admission receipt does not make answer text requester-visible. Only a future verified delivery
record may do that. A denial becomes requester-visible only after the required architect approval
and the request reaches `rejected`.

## 9. Outcome decision table

| Condition | Outcome | Parent request state |
| --- | --- | --- |
| Restated request, purpose, or scope cannot be fixed from the submission | Clarified outcome statement pending | `clarifying` |
| Exact approved assets and authorized scope | Answer or access proposal | `proposed`, then `awaiting_approval` |
| Missing semantic meaning or authority decision | Semantic-change dependency | `investigating` |
| Missing governed dataset, metric, or product | Data-product-change dependency | `investigating` |
| Requester lacks requested disclosure authorization | Denial proposal | `proposed`, then `awaiting_approval` |
| Conflicting, stale mandatory, unverifiable, or unadmitted authority | `No Valid Plan` | `no_valid_plan` |
| Material edit after proposal | New proposal revision; old approvals invalid | `investigating` |
| All exact requirements approved for answer or access | Admission receipt | `executing` |
| All exact requirements approved for denial | Denial disposition | `rejected` |
| Any exact requirement rejected | Rejection record | `rejected` |
| Any approver requests changes | New investigation | `investigating` |
| Policy snapshot expired with an unchanged disposition | Admission against the re-resolved snapshot | `executing` |
| Policy snapshot expired with a changed disposition or scope | New proposal revision; approvals recollected | `investigating` |
| Request cancelled with a proposal open | Abandoned proposal; no admission or disposition | `cancelled` |

The policy compiler returns one of these closed outcomes. Callers cannot substitute a different
outcome or silently weaken a requirement.

## 10. Transaction and replay design

The request, proposal, and admission state must not be split across independently committed SQLite
repositories. `SQLiteFulfillmentRepository` uses the same database and transaction boundary as the
request revisions it advances. The implementation may compose repository classes over one injected
connection, but it may not use compensation as the normal consistency mechanism inside this
service.

Required atomic operations are:

1. persist snapshot and proposal, append request revision, and append transition event;
2. persist a child request and dependency edge;
3. persist `No Valid Plan`, append request revision, and append transition event;
4. persist an approval binding only if the bound request and proposal revisions are current; and
5. persist admission or denial disposition, append request revision, and append transition event.

Every operation uses `BEGIN IMMEDIATE`, exact tenant predicates, expected revisions, and rollback
that cannot replace the original exception. Replay returns an existing artifact only after exact
canonical equality is verified. A partial record cannot be returned as complete on retry.

## 11. Security and privacy

- Every method takes `tenant_id`; bare IDs are never read authority.
- Candidate text and requester purpose are private request content. They are excluded from public
  evidence packages, logs, exception text, metrics, IDs, and digests exposed outside the tenant.
- Public evidence contains allowlisted opaque artifact references, authority refs, reason codes,
  lifecycle states, and timestamps only. Tenant-private proposal, subject, purpose, grounding, and
  policy digests are not exported because low-entropy answer or purpose content must not become
  brute-forceable evidence.
- The requester projection is an allowlist model, not a serialization followed by redaction.
- Provider responses, catalog metadata, candidate text, policy inputs, and persisted artifacts are
  validated as untrusted input.
- AI may propose answer text but cannot select approved assets, decide authorization, compile
  requirements, record approvals, create admissions, or transition terminal state.
- Policy and authority checks fail closed on absence, ambiguity, stale observations, or resolver
  errors.
- Access previews identify an opaque requester principal reference. They never include warehouse
  usernames, credentials, endpoints, role SQL, or administration identifiers.
- Focused mutation testing is required for tenant qualification, candidate visibility,
  authorization disposition, role checks, approval completeness, proposal-revision equality, and
  effective-scope subset checks.

## 12. Error model

Public service methods raise typed domain errors with sanitized messages:

```text
FulfillmentStaleRevision
FulfillmentOwnershipError
FulfillmentGroundingError
FulfillmentPolicyError
FulfillmentAuthorityError
FulfillmentIntegrityError
FulfillmentNotVisible
```

Expected missing capability is not an exception; it compiles to a dependency. Expected insufficient
authorization is not a provider failure; it compiles to a denial proposal. `No Valid Plan` is a
durable governed outcome. Storage corruption, canonical mismatch, and transaction failure are
errors and never become a denial or dependency. A caller that uses a tenant ID different from the
owned request or artifact receives `FulfillmentOwnershipError` with no durable mutation; a valid
current-tenant request containing an unadmitted opaque asset reference may reach sanitized
`No Valid Plan` without disclosing foreign ownership.

## 13. End-to-end flows

### 13.1 Stakeholder answer

1. The requester submits a stakeholder question with purpose.
2. The architect settles the clarified outcome statement, using `clarifying` when the restated
   request cannot be fixed from the submission alone, and the request reaches `investigating`.
3. The service resolves an immutable grounding snapshot.
4. The candidate provider returns private answer text and citations.
5. The fulfillment service rejects any citation, classification, freshness claim, or quality claim
   absent from the snapshot, including a `current` freshness claim with no observation.
6. The policy compiler determines disclosure disposition and exact approval requirements, always
   including the requester's acceptance of the clarified outcome statement.
7. The service stores the proposal and moves the request to `proposed`, then
   `awaiting_approval` when submitted.
8. Required role holders decide the exact proposal digest; the requester accepts the statement
   digest without seeing the candidate.
9. Complete approvals create an execution-ready admission and move the request to `executing`.
10. The requester still cannot read the candidate because no verified delivery exists.

If semantics or governed assets are missing, steps 4 through 9 are replaced by atomic dependency
creation. If disclosure is unauthorized, the only candidate is a denial. If authority is
contradictory or unverifiable, the request reaches `no_valid_plan`. Approval of a denial creates a
denial disposition and moves the request to `rejected`; it never creates an execution admission.

### 13.2 Access preview

1. The requester submits purpose, data product, fields, mode, and requested expiry.
2. The architect settles the clarified outcome statement, and the resolver binds exact semantic,
   catalog, classification, and policy inputs.
3. The policy compiler calculates the least-privilege effective subset and explicit exclusions.
4. The service proves the preview does not widen the requested scope and stores it as a proposal.
5. Data-owner and conditional policy-authority decisions bind the exact preview digest, and the
   requester accepts the clarified outcome statement.
6. Complete approvals create an execution-ready admission and move the request to `executing`.
7. No warehouse role or grant is created. A later grant executor must consume the admission and
   separately prove intended access, denied access, expiry, and revocation.

## 14. Testing and acceptance

### 14.1 Unit and property tests

- strict artifact validation, UTC, canonical digests, unknown-field rejection, and immutable IDs;
- answer citation and access-scope subset properties;
- closed outcome compilation for every policy and grounding condition;
- requester-view allowlist behavior;
- authority-role separation and material-edit invalidation;
- freshness disposition derived from the referenced observation, with `current` refused when the
  observation is null;
- the admission predicate refusing a binding that matches only on `authority_ref`; and
- deterministic IDs and sorted requirements under reordered equivalent inputs.

### 14.2 Repository and fault tests

- happy-path atomic proposal, dependency, `No Valid Plan`, approval, and admission writes;
- stale revision, cross-tenant ID, duplicate replay, identity collision, and partial-write rollback;
- failure at every write boundary followed by retry convergence;
- exact historical approvals remain visible but never authorize a revised proposal; and
- malformed persisted bytes fail closed without leaking payloads.

### 14.3 Cross-component tests

- a Plan 2 publication receipt resolves to one exact Plan 3B snapshot;
- a changed live catalog observation cannot change a proposal already under review;
- an unknown or cross-tenant publication, semantic version, contract, or policy is refused;
- a missing metric creates one semantic or data-product dependency with no proposal;
- an unauthorized finance request creates a denial and never an access-widening dependency;
- one actor holding two roles records two role-scoped approvals;
- an approved answer remains absent from requester view until a verified-delivery fixture exists;
- a `DecisionBinding` recorded against a request carrying a fulfillment proposal is refused, and an
  existing one never satisfies a fulfillment requirement;
- a proposal whose policy snapshot expired before the final approval is superseded rather than
  admitted when the re-resolved disposition differs, and admitted when it does not;
- a request cancelled while awaiting approval admits nothing and leaves its bindings in history; and
- an approved access preview produces no provider or warehouse effect.

### 14.4 Acceptance fixture

Use the existing revenue-to-cash semantic and publication fixtures. The Plan 3B acceptance test
must prove:

1. an authorized semantic-definition question reaches an approved execution-ready answer handoff;
2. an answer requiring absent governed data creates one dependent data-product request;
3. an unauthorized factual question yields an approved denial without exposing candidate content;
4. a finance access request produces a narrowed preview, distinct data-owner and policy-authority
   bindings, and an execution-ready handoff;
5. requester, tenant, and unrelated-role denial projections; and
6. no SQL, provider call, grant, credential, or external message occurs.

This acceptance slice proves control-plane readiness only. It does not satisfy the addendum's final
stakeholder-answer or access-fulfillment exit criteria.

### 14.5 Required verification

Implementation completion requires focused tests, affected component tests, mutation testing of
critical authorization branches, the complete offline gate, repository-structure validation, and
an independent review. A fake candidate provider must construct and validate the real candidate
model; a permissive dictionary fake is not evidence.

## 15. Addendum and conformance amendments

The implementation plan must update the addendum and conformance suite in the same change to pin:

1. the grounding snapshot field shape;
2. the proposal subjects and immutable revision rule;
3. the approval requirement and role-scoped binding shapes;
4. the execution-ready admission boundary and its explicit non-claims;
5. the dependency shape and allowed dependency kinds;
6. the outcome decision table;
7. candidate visibility by actor class; and
8. the required architect and data-product-owner authorities, the requester's clarified-outcome
   acceptance requirement, and the rule that only *outcome* acceptance is deferred to delivery;
9. the clarified outcome statement shape and the rule that no proposal is created from `clarifying`;
10. the boundary between `DecisionBinding` and `FulfillmentApprovalBinding`, including that neither
    satisfies the other's requirement;
11. the admission predicate, including the `policy_snapshot.valid_until` re-check and the
    supersession rule when a re-resolved snapshot changes disposition or scope;
12. that material recurring cost approval under section 14 is deliberately deferred, because Plan 3B
    neither executes nor prices an effect, so a later plan inherits it rather than rediscovering it;
    and
13. the rule that requester delivery, query execution, grant application, verification, expiry, and
    revocation remain later stages.

No conformance amendment may imply that control-plane approval proves a data-plane effect.

## 16. Delivery decomposition

This is one architectural slice but should be implemented in reviewable tasks:

1. change `SQLiteRequestRepository` to accept an injected `sqlite3.Connection`, keeping a
   path-taking classmethod, with no behavior change and the existing Plan 1 and Plan 2 suites green
   across all 42 construction sites;
2. ratify the artifacts and decision tables in the addendum and conformance fixtures;
3. add strict fulfillment, dependency, approval, and read-view models;
4. add append-only persistence and atomic request/fulfillment transactions;
5. add deterministic policy and authority evaluation;
6. add immutable Plan 2 snapshot resolution through the semantic-registry adapter;
7. add stakeholder-answer proposal and dependency outcomes;
8. add access preview, denial, and authority outcomes;
9. add exact approval admission and requester/reviewer projections;
10. add the cross-component acceptance and fault matrix; and
11. run full verification and independent review.

Each task ends with focused success and denial tests and one logical Conventional Commit. The
implementation plan may split tasks further but may not combine later SQL, grant, UI, or delivery
effects into Plan 3B.

## 17. Risks and mitigations

**Risk: approval is mistaken for execution.**
: The admission receipt has only `ready_for_execution`; requester visibility and terminal delivery
  require later effect and verification receipts.

**Risk: mutable catalog state changes the proposal.**
: The snapshot binds persisted publication intent, receipt, semantic, contract, and round-trip
  digests. Discovery is never authority.

**Risk: request-management becomes a dependency hub.**
: It defines narrow protocols; the semantic-registry adapter implements the existing outward
  dependency direction. Provider SDKs remain outside request-management.

**Risk: candidate content leaks through evidence or errors.**
: Candidate storage is private and evidence/read projections are structural allowlists.

**Risk: separate approvals collapse into one generic click.**
: Each requirement needs an exact role-scoped binding, even when one person holds multiple roles.

**Risk: dependencies become a workflow engine.**
: Only two typed blocking references exist; they do not run parents, carry schedules, branch, join,
  or execute arbitrary steps.

## 18. Definition of done

Plan 3B is complete only when:

- the addendum, durable models, decision tables, and conformance tests agree;
- proposals bind immutable Plan 2 receipts and cannot change under live catalog drift;
- candidate content is unavailable to requesters before verified delivery;
- missing capability, insufficient authorization, and `No Valid Plan` remain distinct outcomes;
- every material edit invalidates prior approvals, and a changed clarified outcome requires fresh
  requester acceptance;
- every required role has an exact current decision binding matched on authority, subject digest,
  and proposal revision; - admission refuses an expired policy snapshot and supersedes the proposal
  when re-resolution changes the disposition or effective scope; - no proposal is created from
  `clarifying`, and a cancelled request admits nothing; - `DecisionBinding` and
  `FulfillmentApprovalBinding` never satisfy each other's requirements; - proposal admission and
  request transition are atomic and replay-safe; - cross-tenant, stale, malformed, and contradictory
  inputs fail closed; - access previews can narrow but never widen requested scope; - evidence
  contains no answer text, request purpose, raw data, private provider identifier, credential,
  endpoint, or grant statement; - no Plan 3B path executes SQL, applies a grant, sends a result, or
  claims delivery; - the committed acceptance journey passes without direct OpenMetadata or
  warehouse operation; - focused mutation tests leave no surviving critical authorization mutation;
  - all offline and structure gates pass; and - independent review has no unresolved blocking
  finding.

Plan 3B then provides the stable authority handoff required by later source acquisition, query
execution, access application, Superset delivery, and full witnessed acceptance.
