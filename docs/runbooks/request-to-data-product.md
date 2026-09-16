# Request-to-data-product incident recovery

This runbook covers diagnosis and recovery for a request that has not reached its durable terminal
result, catalog publication, dashboard publication, or access revocation. State is authoritative for
run progress and incidents. The service named by an incident's `source_service` remains authoritative
for the failed operation and its source record. The console only projects those records and submits
commands to the owning service.

Never infer success from a healthy process, an HTTP success response, an existing warehouse table,
or an existing dashboard. A recovery is complete only after a fresh or replayed transaction reaches
its terminal state and the owning receipt is read back.

## Safety rules

1. Work within one tenant. Resolve the tenant from the authenticated operator session; do not accept
   a tenant identifier copied from an untrusted report.
2. Capture the incident revision before acting. Submit exactly one action allowed by that revision.
   If the revision changes, reload and diagnose the new record.
3. Do not rerun a provider mutation when its outcome is unknown. Reconcile the provider's durable
   state by its stable key and expected digest first.
4. Do not edit incident, run, checkpoint, result, catalog, or BI-control database rows. Their payloads
   are canonical owning records, and direct edits break replay and integrity checks.
5. Do not disclose credentials, secret references, endpoints, SQL statements, raw source values,
   result values outside the approved result, or provider account and object identifiers in tickets
   or evidence.
6. Preserve the original failure. If incident persistence, evidence persistence, or the provider
   inspection path fails, stop and escalate rather than replacing the original classification.

The Operations page is the normal operator interface. It exposes a temporary console handle in
place of the private incident identity and offers only the run recovery actions currently wired by
the state service. Direct database inspection below is read-only diagnostic work for an operator
with approved host access.

## Incident contract

Every incident is an append-only revision with these diagnostic fields:

| Field | Meaning | Operator use |
| --- | --- | --- |
| `revision` | Current optimistic-concurrency revision | Use this exact value with a recovery command. |
| `kind` | Failed capability | Select the owning service and capability-specific checks. |
| `classification` | What is known about the failure | Determines whether retry, reconciliation, contract change, or escalation is valid. |
| `last_successful_stage` | Last stage backed by durable evidence, if any | Resume after this boundary; do not repeat earlier effects. |
| `failed_stage` | Exact stage that did not complete | Start log and receipt correlation here. |
| `user_impact` | Privacy-safe effect visible to the requester | Use for status communication; do not replace it with raw provider text. |
| `next_automatic_action` | State-owned automatic action, if one is admitted | Let it run unless its deadline is exceeded or evidence conflicts. |
| `allowed_operator_actions` | Actions admitted for this exact state | Treat this tuple as authoritative. An absent action is forbidden. |
| `source_service` and `source_record_ref` | Owning service and durable failure record | Correlate the incident to the owning receipt. |
| `run_id` and `run_attempt_number` | Exact run attempt, when the incident is run-backed | Required for transient run retry. |
| `evidence_refs` | Sanitized records that support the projection | Read all of them before acting. |
| `opened_at` and `updated_at` | UTC lifecycle timestamps | Establish ordering and whether automatic recovery is overdue. |

The classifications have deliberately different consequences:

| Classification | Meaning | Default response |
| --- | --- | --- |
| `transient` | Transport, availability, or throttling failure whose effect is known | Wait for the stated automatic action, or retry only when offered for the exact failed attempt. |
| `ambiguous_outcome` | A mutation may have committed even though its response was lost | Inspect and reconcile by stable identity and digest. Never issue a blind create or write. |
| `authorization_denied` | Current authority did not permit the operation | Recheck current binding, grant, and role evidence. Escalate or change authority through its owning workflow. |
| `integrity_failure` | Stored or provider-returned state cannot be trusted | Fence the affected path and escalate immediately. Do not retry. |
| `conflict` | Current authority or generation differs from the attempted one | Re-resolve current authority and use a compatible replan or contract supersession if offered. |
| `permanent` | The exact attempt cannot succeed unchanged | Correct or supersede its authoritative input. Do not retry the same attempt. |
| `no_valid_plan` | The compiler proved that no legal physical plan exists | Preserve the terminal decision. Use only a separately reviewed compatible replan or contract supersession. |

## Diagnose an incident

### 1. Read the current projection

Open **Operations** as a data architect or data owner. Record the displayed kind, classification,
last successful stage, failed stage, user impact, next automatic action, allowed actions, revision,
and update time. Keep the response `X-Correlation-ID` if the page or action returns an error.

An empty Operations page means only that no current incidents were returned for the authenticated
tenant. It does not prove that all requests completed. Cross-check the request's owning lifecycle
and the terminal receipt for the capability being investigated.

When the console is unavailable, inspect the incident repository read-only. Set these variables in
the operator shell; do not put credentials in them:

```sh
export PILLARMESH_INCIDENT_DB=/approved/path/to/state.sqlite3
export PILLARMESH_TENANT_ID=tenant-from-authenticated-session
```

Then print the current canonical incidents for that tenant:

```sh
uv run python - <<'PY'
import json
import os
import sqlite3
from pathlib import Path

database_uri = Path(os.environ["PILLARMESH_INCIDENT_DB"]).resolve().as_uri() + "?mode=ro"
tenant_id = os.environ["PILLARMESH_TENANT_ID"]
connection = sqlite3.connect(database_uri, uri=True)
rows = connection.execute(
    "SELECT revisions.payload "
    "FROM incident_current AS current "
    "JOIN incident_revisions AS revisions "
    "ON revisions.tenant_id = current.tenant_id "
    "AND revisions.incident_id = current.incident_id "
    "AND revisions.revision = current.current_revision "
    "WHERE current.tenant_id = ? ORDER BY current.incident_id COLLATE BINARY",
    (tenant_id,),
).fetchall()
for (payload,) in rows:
    incident = json.loads(bytes(payload))
    print(json.dumps(incident, indent=2, sort_keys=True))
PY
```

Before trusting the store, run integrity checks against the same read-only connection:

```sh
uv run python - <<'PY'
import os
import sqlite3
from pathlib import Path

database_uri = Path(os.environ["PILLARMESH_INCIDENT_DB"]).resolve().as_uri() + "?mode=ro"
connection = sqlite3.connect(database_uri, uri=True)
print("quick_check", connection.execute("PRAGMA quick_check").fetchone()[0])
print("foreign_key_violations", len(connection.execute("PRAGMA foreign_key_check").fetchall()))
PY
```

Any result other than `quick_check ok` and zero foreign-key violations is an integrity escalation.

### 2. Follow durable references

Use `source_service` to select the owning store. Locate `source_record_ref` and every
`evidence_refs` entry by exact equality. Never search by display title, partial digest, timestamp
alone, or provider object name. Verify that tenant, contract or request identity, run attempt,
revision, generation, and digest agree across the chain.

For a run-backed incident, inspect the run repository read-only. The canonical payloads contain the
claim epoch, lease expiration, completion outcome, durable boundary, cancellation, and retry
authority:

```sh
export PILLARMESH_RUN_DB=/approved/path/to/runs.sqlite3
export PILLARMESH_RUN_ID=run-id-from-the-incident

uv run python - <<'PY'
import json
import os
import sqlite3
from pathlib import Path

database_uri = Path(os.environ["PILLARMESH_RUN_DB"]).resolve().as_uri() + "?mode=ro"
run_id = os.environ["PILLARMESH_RUN_ID"]
connection = sqlite3.connect(database_uri, uri=True)
for table, order_by in (
    ("runs", "rowid"),
    ("run_attempt_claims", "attempt_number"),
    ("run_attempt_completions", "attempt_number"),
    ("run_cancellations", "rowid"),
    ("run_retry_requests", "failed_attempt_number"),
):
    rows = connection.execute(
        f"SELECT payload FROM {table} WHERE run_id = ? ORDER BY {order_by}",
        (run_id,),
    ).fetchall()
    print(table)
    for (payload,) in rows:
        print(json.dumps(json.loads(bytes(payload)), indent=2, sort_keys=True))
PY
```

Check these invariants:

- the incident attempt is the latest claim;
- the claim epoch is the latest epoch and its worker matches any completion;
- a retryable completion is `failed` with `failure_classification` equal to `transient`;
- an active, unexpired lease is not a stuck lease;
- no cancellation exists before retry;
- the `durable_boundary_ref` names the last effect known to have completed; and
- at most one retry request exists for the failed attempt.

For a query failure, inspect `answer_execution_attempts` by the exact tenant and request identity.
The latest receipt is authoritative. A successful receipt must have a matching result snapshot;
other outcomes must not have one. Do not print `answer_results.payload` during routine diagnosis
because it can contain approved result values.

```sh
export PILLARMESH_RESULT_DB=/approved/path/to/results.sqlite3
export PILLARMESH_REQUEST_ID=request-id-from-owning-record

uv run python - <<'PY'
import json
import os
import sqlite3
from pathlib import Path

database_uri = Path(os.environ["PILLARMESH_RESULT_DB"]).resolve().as_uri() + "?mode=ro"
connection = sqlite3.connect(database_uri, uri=True)
rows = connection.execute(
    "SELECT receipt FROM answer_execution_attempts "
    "WHERE tenant_id = ? AND request_id = ? ORDER BY attempt",
    (os.environ["PILLARMESH_TENANT_ID"], os.environ["PILLARMESH_REQUEST_ID"]),
).fetchall()
for (payload,) in rows:
    receipt = json.loads(bytes(payload))
    safe = {
        key: receipt.get(key)
        for key in (
            "receipt_id",
            "attempt",
            "plan_digest",
            "outcome",
            "provider_error_classification",
            "result_ref",
            "result_digest",
            "started_at",
            "completed_at",
        )
    }
    print(json.dumps(safe, indent=2, sort_keys=True))
PY
```

### 3. Correlate logs without widening disclosure

Search structured service logs first by the exact `source_record_ref`, then by the run ID and
attempt number, and finally by the console `X-Correlation-ID` for console-boundary failures. Limit
the time range to `opened_at` through `updated_at`, with a small allowance for clock skew. Confirm
the service name and tenant-scoped authority in the owning record before accepting any match.

Logs are supporting diagnostics, not lifecycle authority. A warning or exception can explain a
receipt, but it cannot replace one. Do not paste an entire trace into an incident. Retain only the
error class, privacy-safe reason code, service/version, UTC time, stable correlation reference, and
the owning receipt reference. If the deployment has no structured query that can match the exact
reference, escalate the observability gap instead of using a broad text search as evidence.

### 4. Select the owning recovery path

Use the incident's `allowed_operator_actions`, not the following table by itself. The table explains
the usual owner and why an action may be offered.

| Incident kind | Owning checks | Valid recovery when explicitly offered |
| --- | --- | --- |
| `stuck_lease` | Latest claim, epoch, lease expiry, completion, cancellation | Retry the exact transient failed attempt after lease expiry. A claimed run cannot use `cancel_unstarted_work`. |
| `source_unavailable` | Current source binding, credential authority, provider health, last checkpoint | Retry only a known transient attempt. Authorization and permanent configuration failures require their owning setup workflow. |
| `checkpoint_conflict` | Stored checkpoint revision, proposed checkpoint, source continuity evidence | Approve a compatible replan or supersede the contract. Never overwrite or decrement a checkpoint. |
| `schema_drift` | Activated contract schema, fresh source observation, classification policy | Supersede the contract, or approve a compatible replan whose authority cites the new schema. |
| `no_valid_plan` | Compiler constraints and cited authority snapshot | Approve a compatible replan or supersede the contract. Generic retry is forbidden. |
| `transform_rejection` | Signed model, input generations, dbt receipt, expected and observed schema digests | Replan or supersede. Retry only when the incident separately classifies a transient invocation failure and offers it. |
| `catalog_pending` | Committed materialization receipt, stable consumption object, catalog operation and receipt | Reconcile the external effect. Do not rerun the transform solely to publish the catalog entry. |
| `query_failure` | Latest answer receipt, signed plan, generation, current entitlement, provider classification | Follow the classification. The current query incident projection offers no direct recovery action; use the owning request/query workflow after diagnosis. |
| `dashboard_drift` | Signed dashboard contract, current desired revision and digest, provider receipt, stable external key | Reconcile the external effect. Conflicting or duplicate managed objects are an integrity escalation. |
| `revocation_pending` | Authoritative grant state and each provider-specific revocation receipt | Keep access denied and reconcile only the missing provider effects. Do not reactivate the grant to make cleanup pass. |

## Execute a state-valid recovery

The currently implemented recovery command supports only `retry_transient_attempt` and
`cancel_unstarted_work`. The other modeled actions (`approve_compatible_replan`,
`reconcile_external_effect`, and `supersede_contract`) belong to their owning workflows and must
not be emulated with a generic run retry.

Use the Operations page so the authenticated tenant and actor come from trusted session context.
Provide a concise reason that states the observed condition and the intended recovery without
including payload values. The console submits:

- the incident's current `expected_revision`;
- exactly one displayed `action`;
- the active data-architect or data-owner role;
- the operator's reason; and
- a fresh idempotency key.

The server converts the public incident handle to its tenant-scoped private identity and records the
authenticated actor. A stale revision returns a reload instruction. A missing or cross-tenant handle
is non-enumerating. An action that is no longer valid returns a conflict.

### Retry a transient attempt

`retry_transient_attempt` is valid only when all of these are true:

- the incident classification is `transient`;
- the action appears in `allowed_operator_actions`;
- the incident binds an exact run ID and failed attempt number;
- that attempt is still the latest claim and has a failed transient completion;
- the run is not cancelled; and
- no retry request has already been recorded for that attempt.

The command records a `RunRetryRequest`; it does not claim a worker lease or prove that the next
attempt ran. Verify that a later worker obtains a higher attempt number and epoch, resumes from the
recorded durable boundary, and writes a terminal completion. Then read the downstream owning receipt.

### Cancel unstarted work

`cancel_unstarted_work` is valid only when the action is offered, the incident binds a run, and that
run has never had a claim. It writes a durable cancellation that prevents later claims. It cannot
cancel active, expired, completed, or failed attempts. Use a capability-specific stop or fencing
workflow for work that already reached a provider.

### Command replay

A recovery command is replay-safe only when command ID, tenant, incident, incident revision, action,
actor, and reason are byte-for-byte equivalent to the recorded evidence. Reuse the same idempotency
key only to recover a lost response to that exact command. Changing any field with the same key is a
conflict. A second command against an incident revision that already has recovery evidence is also a
conflict.

After a successful command, verify both records:

1. `incident_recovery_evidence` contains the command, exact incident revision, actor, reason, source
   record, resulting record, and UTC time.
2. The run repository contains the referenced retry request or cancellation.

The Operations page suppresses actions once recovery evidence exists for the current incident
revision. That suppression proves a command was recorded, not that downstream work completed.

## Resolve provider ambiguity

An `ambiguous_outcome` means a write may have committed. Keep the user-facing result unavailable
until the effect is verified.

### PostgreSQL and ClickHouse LAND

Use the original immutable segment, target, generation key, and idempotency key. Invoke the provider
adapter's commit inspection against the original receipt. A checkpoint may advance only when the
observation is `committed` and its receipt digest exactly matches the original receipt. If the
observation is absent or unverifiable, retain ambiguity and escalate. If replay is needed after a
verified non-commit, replay the same generation through the normal landing runner so its ledger can
return the exact prior receipt; do not issue ad hoc `INSERT`, `COPY`, or ClickHouse mutation SQL.

### PostgreSQL and ClickHouse governed queries

Queries are read-only, but a lost response still cannot become an answer. Use the durable answer
receipt. A `provider_failed` receipt with an ambiguous classification has no result snapshot and
must remain unavailable. Start a new authorized execution only through the owning query workflow,
after rechecking the signed plan, approved generation, entitlement, and ceilings. Never reconstruct
an answer from provider logs.

### Superset

A timeout or transport loss during a mutation is ambiguous. Inspect by the deterministic
`stable_external_key`, never by title. Compare the managed desired digest and lifecycle state to the
current BI-control desired record. One exact match can be reconciled and recorded. No match permits
the provider adapter to replay the same desired revision. More than one match, a mismatched managed
digest, an incomplete stable-key claim, or an unexpected provider identity is an integrity failure;
do not delete or overwrite the objects automatically. Never expose or use the stored raw external
URL as access authority.

### Stripe acquisition

Transport, availability, and throttling failures can be retried only from the last durable
checkpoint. `stripe_event_cursor_expired` and `stripe_event_overlap_gap` require the explicit
`resynchronization_required` path; they are not ordinary transient retries. Preserve the old
checkpoint, acquire a newly authorized snapshot, and let the state-owned snapshot-to-CDC transition
establish the next checkpoint.

### Catalog publication

When materialization is committed but publication is pending, retain the committed generation and
stable consumption view. Reconcile the original catalog operation by its operation identity and
stable managed resources. Do not rerun transformation just to recreate a catalog response. Unknown
cleanup status or multiple resources claiming the same managed identity requires escalation.

## Rollback limits

PillarMesh does not roll back by deleting authoritative history.

- A request decision, compiler `No Valid Plan`, run claim, completion, retry request, cancellation,
  acquisition checkpoint, materialization receipt, answer receipt, incident revision, and recovery
  evidence are durable facts. Correct them with a new owning revision or superseding transaction.
- An acknowledged LAND generation is immutable. Do not remove rows or rewind its checkpoint.
- A committed product generation remains addressable through its receipt and retention policy.
  Restore a prior stable view only through an authorized cutover that verifies its exact generation.
- Catalog and dashboard changes are reconciled from their recorded desired state. Do not rename,
  delete, or recreate provider objects by display name.
- Revocation is fail-closed. User access remains denied while provider cleanup is pending.
- Cancellation applies only before the first claim. It is not a rollback for an active provider
  effect.

When a safe rollback is unavailable, fence new work on the affected binding or contract, preserve
evidence, and escalate for a reviewed supersession or migration.

## Privacy-safe recovery evidence

Retain enough evidence to reconstruct the decision without retaining business payloads:

- tenant-safe request, contract, plan, run, attempt, generation, incident, and receipt references;
- the incident revision and recovery command ID;
- canonical digests, provider and compiler versions, and stable managed keys where applicable;
- lifecycle transitions and UTC timestamps;
- failure classification and an allowlisted reason code;
- authenticated actor, role, and a concise operator reason;
- current policy, entitlement, binding, and contract revision decisions; and
- the terminal owning receipt read back after recovery.

Exclude credentials, tokens, secret references, endpoints, SQL, source rows, unapproved result
values, raw provider responses, local database paths, stack traces containing payloads, and provider
account or object identifiers. Validate or scan an evidence package before it leaves the approved
operator environment.

## Escalate

Escalate immediately and do not retry when any of these is true:

- the incident or owning payload fails canonical validation or store integrity checks;
- an incident reference cannot be resolved exactly within its tenant;
- two records claim the same stable identity, or a provider object conflicts with its expected digest;
- a provider mutation remains ambiguous after its supported inspection path;
- a stale epoch attempts to complete, a completion conflicts with recorded outcome, or a checkpoint
  would need to move backward;
- an authorization denial cannot be explained by the current grant, binding, role, and policy;
- a permanent failure or `No Valid Plan` is offered a generic retry;
- recovery evidence exists but its resulting run record is absent;
- a user can access a result or dashboard while revocation is pending; or
- the exact transaction cannot be followed through structured logs and durable receipts.

The escalation should include the privacy-safe evidence list above, the expected and observed state,
the last verified durable boundary, and the explicit condition that prevents further action. Keep
the affected result unavailable and fence new work when authority or integrity is uncertain.

## Post-recovery verification

For retry, follow the new attempt through a higher epoch to a terminal completion and read the
destination, materialization, catalog, answer, dashboard, or revocation receipt that the request
requires. For cancellation, verify the cancellation readback and prove a later claim is denied. For
reconciliation, read both the provider observation and the owning receipt with matching stable key,
revision, and digest.

Run the focused state checks after changes to the recovery path:

```sh
uv run pytest services/state/tests/test_incident_models.py \
  services/state/tests/test_incident_repository.py \
  services/state/tests/test_recovery_service.py -q
```

Use the fresh transaction procedures in
[PostgreSQL governed-answer local acceptance](../delivery/postgresql-answer-query-live.md),
[PostgreSQL product materialization live acceptance](../delivery/postgresql-product-materialization-live.md),
and the [request-to-data-product acceptance ledger](../delivery/request-to-data-product-acceptance.md)
before claiming an end-to-end capability. A skipped live test remains an unresolved gap.
