# ADR-0007: Separate Enterprise Entitlements from Applied Access Grants

- Status: Proposed
- Date: 2026-09-12
- Context: [Architecture](../../architecture.md), [ADR-0003](ADR-0003-managed-data-engineering-platform.md)

## Context

Heinzel treats a connected enterprise policy system as authoritative for enterprise policy. It
requires current requester entitlements during proposal preparation, answer validation, policy admission, query execution, and verified delivery. The
existing implementation can carry entitlement references and digests, but it has no durable reader
for the connected authority. Contract approval identifiers have consequently been available at
composition sites even though an approval proves a decision about one artifact and says nothing
about the requester's current access.

Heinzel also needs its own `AccessGrant` lifecycle. That grant is a capability Heinzel
applies after request-management admission. It is not the enterprise entitlement against which the
proposal was checked. A third concept, the warehouse principal, is an engine identity provisioned
and probed by warehouse-control. Conflating any two of these lets a local approval manufacture its
own prerequisite authority or lets a database role become an access-policy record.

The repository layout assigned grants to runtime, although grant application, expiry, revocation,
and receipts belong with access control. Warehouse-control already
owns principal provisioning and private credential handles. These ownership statements must be
reconciled before access effects or governed-answer composition are enabled.

## Decision

Create `services/access-control`. It owns two distinct durable lifecycles:

1. immutable observations and deterministic current snapshots obtained from the tenant's connected
   enterprise policy authority; and
2. governed `AccessGrant` desired state, expiry, revocation, and provider-effect receipts.

The external system remains authoritative for entitlement. Access-control records what it observed
and the authenticated provenance of that observation. It never creates an allow decision from a
request approval, contract approval, local role label, catalog record, or previously cached
observation when the connected authority is unavailable.

### Connected entitlement records

An `EnterpriseEntitlementAssertion` contains:

- tenant, principal, and a digest of the exact request purpose;
- active or revoked disposition;
- exact product-version and semantic artifact references;
- closed filter domains and the `view`, `query`, `download`, or `dashboard` permissions granted;
- effective and expiry timestamps; and
- authenticated provenance: connected-authority and connection-binding references, monotonically
  increasing source revision, source-payload digest, authentication method and key reference,
  authentication-evidence digest, and adapter version reference.

Access-control persists the assertion as an `EnterpriseEntitlementObservation`. The repository
returns an exact replay at the same source revision, rejects different content at that revision as
equivocation, and rejects a lower revision as rollback. A revoked or expired observation is
persisted before resolution denies access so the denial cannot be bypassed by a later stale read.
Tenant or principal mismatches are rejected before persistence.

A `CurrentEntitlementSnapshot` is the deterministic, immutable scope used by consumers. Its
semantic digest binds tenant, principal, purpose, connected authority, source revision,
source-payload digest, exact scope, effective time, and expiry. It deliberately excludes the local
observation identifier, snapshot identifier, observation time, and resolution time. Re-reading
unchanged authenticated authority therefore yields the same semantic digest, while a new source
revision, changed scope, revocation, or expiry cannot compare equal.

Every current resolution calls an `AuthenticatedConnectedPolicyAuthority`. Missing or unavailable
authority fails closed even when an earlier observation remains in SQLite. The SQLite ledger alone
is historical evidence and is never a live entitlement adapter. Before the console's governed-local
mode or a live deployment can claim entitlement enforcement, it must compose a concrete
authenticated connected-policy adapter for the tenant's declared enterprise policy system. That adapter must
authenticate the upstream response, verify its source revision and payload digest, normalize exact
Heinzel artifact references and closed filter domains, and return the strict assertion model.
Scripted or fixture assertions cannot satisfy this requirement.

### Applied access grants

An `AccessGrant` is a Heinzel-owned applied capability. It is created only from an admitted
request-management access proposal and is narrowed against a freshly resolved current entitlement
snapshot. In addition to its own scope and term, it binds that snapshot's digest. Append-only grant
revisions carry `pending`, `active`, `revocation_pending`, `revoked`, or `failed` state; exact
provider-effect receipts identify each application or cleanup effect without exposing provider
identifiers publicly.

Approval evidence authorizes creation of the proposed grant. It never becomes entitlement evidence.
An active grant remains usable only while its own term and the connected enterprise entitlement are
current. Expiry or revocation denies synchronously in access-control even while provider cleanup is
pending. State owns the deterministic time-based intent that wakes reconciliation; it does not own
the grant or decide access.

### Runtime and warehouse boundaries

Warehouse-control provisions, rotates, and positively and negatively probes principal classes. It
owns private credentials and connection handles. The dedicated `answer_runtime` service principal
is not a requester grant: it reads approved consumption objects only for an already admitted
governed query and has no write authority. Access-control never issues or resolves this credential.

Runtime verifies a signed plan, consumes an exact authorization decision, executes through the
principal supplied by warehouse-control, and records execution and result evidence. It owns no
grant state and cannot infer entitlement from a plan, approval, or database permission. It asks
access-control to resolve current authority before provider access.

Request-management asks access-control for a current snapshot before deterministic answer
validation, compares that exact digest during policy admission, and resolves it again before
verified delivery. Result metadata, pages, downloads, dashboard links, delegated-agent calls, and
standing customer SQL access perform the same current check before disclosure. A governed answer
does not require a customer `AccessGrant`; its authority is the exact policy or reviewed admission
intersected with the current enterprise entitlement. Standing customer, result, or dashboard access
does require an active grant.

Provider adapters translate typed access effects for PostgreSQL, ClickHouse, or Superset and return
classified receipts. They neither select scope nor decide authority. Access-control retains desired
state and reconciles only missing effects after partial failure.

## Initial implementation boundary

The initial implementation declares access-control and implements the strict assertion,
observation, snapshot, SQLite repository, current resolver, and normalized signed HTTP transport.
It proves exact replay, authenticated provenance, equivocation and rollback rejection, revocation
and expiry, tenant and principal isolation, verified scope and signature, and fail-closed missing,
invalid, or unavailable authority.

It did not implement an enterprise-vendor integration, local-development authority server,
`AccessGrant`, provider effects, expiry scheduling, or live composition. `AccessGrant` and its
provider effects have since been added to access-control; [capability status](../../status.md)
records what works today. A tenant must configure and operate a conforming authority before
replacing any caller-supplied entitlement digest in the console's governed-local mode.

The first concrete transport is the vendor-neutral `SignedHttpConnectedPolicyAuthority`. It sends
an exact tenant, principal, and purpose lookup to a configured HTTPS
`POST /entitlements/current` endpoint with a read-only bearer credential, explicit timeout, and
configured TLS trust. Its strict response envelope identifies one configured Ed25519 key and holds
only a signed entitlement body and signature. The source payload digest covers the canonical body
claims excluding that digest field. The signature has the
`heinzel-enterprise-entitlement-v1` domain and covers the complete body including the digest.
Authority reference, connection binding, authentication method and key, evidence digest, and
adapter reference come from verified bytes and adapter configuration rather than response claims.

This protocol makes a dedicated local-development authority usable for acceptance testing. That
server may issue an operator-configured grant for only its test tenant and purpose, but it remains a
test integration of the normalized protocol rather than verification of an enterprise vendor. A
production tenant still requires external authority setup, a least-privilege read credential, TLS
trust material, a pinned signing key, rotation procedures, and validation against that authority's
real policy and revision semantics. The local authority must not derive grants from answer or
contract approvals and must not expose a public grant-mutation endpoint.

## Consequences

- A fresh governed answer can no longer treat an approval identifier or arbitrary digest as a
  requester entitlement.
- Current checks depend on availability of the connected authority and fail closed during an
  outage. Cached observations remain audit evidence, not authorization fallback.
- The same semantic authority remains digest-stable across harmless read-time changes.
- Access-control becomes the single owner of access desired state across result, warehouse, and BI
  surfaces, while provider IDs and credentials remain private to their established boundaries.
- Live access or governed-answer delivery can be claimed only with a configured connected-policy
  authority in addition to database and Superset effect adapters.

## Alternatives Considered

### Persist entitlement observations in semantic-registry

Rejected because the semantic registry resolves approved business meaning. Putting current user
access there would split one access decision between semantic-registry and access-control and make
expiry and grant reconciliation depend on a service that owns neither.

### Persist entitlement snapshots in request-management

Rejected because request-management owns the approvals that consume entitlement evidence. Letting
it author that evidence would allow the requesting lifecycle to manufacture its own prerequisite.

### Treat provider roles or contract approvals as entitlement

Rejected because provider permissions are effects and approval records bind artifact decisions.
Neither proves the enterprise authority's current decision for a tenant, principal, and purpose.
