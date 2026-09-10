# Local Question Refusal and Recovery Design

**Date:** 2026-09-09
**Status:** Approved for implementation planning
**Scope:** Governed-local stakeholder questions and the requester recovery experience

## 1. Purpose

The governed-local console currently composes the Plan 3B scenario provider for every stakeholder
question. That provider has one fixed answer about the meaning of net revenue. A requester can ask
an unrelated question such as "What is the current MRR" and the preparation command can produce
that fixed answer with current-freshness metadata. The result is authoritative-looking but is not
grounded in the submitted question.

The requester projection also omits the submitted question. The architect can inspect it, but the
requester cannot confirm the exact wording that PillarMesh recorded. When a request cannot be
fulfilled, the requester sees only a closed lifecycle state and receives no safe explanation or
direct recovery path.

This change makes the local product honest and testable. The local scenario admits only the one
question for which it has deterministic facts, records every unsupported question as `No Valid
Plan`, explains that outcome safely to the requester, and lets the requester start a separate
pre-filled request without rewriting the terminal record.

## 2. Governing decisions

1. Request-management remains authoritative for lifecycle state, `No Valid Plan`, and requester
   visibility.
2. The local scenario authority resolver owns the local allowlist because it knows which
   authoritative observations and answer candidate the harness can supply.
3. The supported local stakeholder question is the exact normalized pair:
   - purpose: `semantic definition`
   - question: `What does net revenue mean?`
4. Unsupported questions return `ResolutionFailure` before an answer candidate is requested.
5. The failure is stored transactionally with the transition to `no_valid_plan`; it is not a
   browser-only warning or an unrecorded provider exception.
6. Internal reason codes, constraint references, and smallest-change instructions remain visible
   to the architect. The requester receives only a separately modeled requester-safe explanation.
7. `no_valid_plan` remains terminal. Recovery creates a new request rather than reopening or
   mutating the old request.
8. The recovery form is pre-filled in browser state. This increment does not claim a durable
   supersession relationship because request-management has no such contract today.
9. The original stakeholder question is owned and projected by request-management. The console
   does not derive it from conversation text or the title.

## 3. User experience

### 3.1 Architect

The architect may leave clarification fields empty while inspecting a submitted request. Nothing is
recorded until an enabled command is submitted. After the request is clarified and the architect
chooses **Prepare answer**, an unsupported local question transitions to `No Valid Plan`.

**This projection already exists and is unchanged by this increment.**
`GovernedConsoleBackend._preparation_notes` already emits the refusal notes below, already guarded
on `resulting_request_revision == request.revision`. What is new is that a local question now
reaches this path at all. The architect assertions in the plan are therefore regression coverage,
not new behaviour.

The request detail shows:

- lifecycle state **`no_valid_plan`** (rendered `no valid plan`);
- `No Valid Plan: local_scenario_not_supported.`; and
- `Required change: Configure an authoritative source for this question or submit the documented
  net-revenue semantic-definition scenario.`

> **Superseded 2026-09-10.** This section originally recorded that the console projected
> `RequestState.NO_VALID_PLAN` as `closed`, sharing that label with `DELIVERED`, `MONITORING`,
> `CANCELLED` and `FAILED`. A live audit found requesters could not tell an answer from a refusal,
> so the console vocabulary now carries `delivered`, `no_valid_plan`, `cancelled` and `failed`
> distinctly; only `RETIRED` still projects as `closed`.

Note the capitalisation: the note reads `No Valid Plan:`, so a case-sensitive match on
`no valid plan` fails against the note, although it matches the lifecycle label.

There is no proposal, approval action, or answer candidate.

### 3.2 Requester

The requester detail shows the exact original question under the requested outcome. For the local
unsupported-question outcome it also shows:

> This local environment has no authoritative source configured for that question.

The explanation must not contain constraint references, provider identities, internal artifact
references, or an unapproved candidate answer.

The detail offers **Start revised request**. Activating it reveals the existing intake component
with the following pre-filled values:

- kind: stakeholder question;
- title: the original title;
- purpose: the original purpose; and
- question: the original question.

The requester may edit every pre-filled value. Submission uses the existing canonical intake
digest, CSRF, idempotency, and request-management submission path. Success creates a new request ID
at revision 1. The old request remains unchanged and visible.

For a data-access request, `question` is absent and this recovery action is not shown by this
increment.

## 4. Contract changes

### 4.1 Resolution and durable outcome

**This follows an existing pattern rather than introducing one.** `RequestDenial` and
`DisclosureDenial` already carry `requester_safe_explanation`, and `FulfillmentReadService.requester_view`
already projects it as `denial_explanation` when the state is `RequestState.REJECTED`. The work
below is the `no_valid_plan` counterpart of that pattern, and the implementation should copy the
established shape rather than invent a second idiom.

Add an optional `requester_safe_explanation` to these request-management models:

- `ResolutionFailure`;
- `NoValidPlanCompilation`; and
- `RequestNoValidPlan`.

Declare it exactly as the existing denial fields do, except optional:

```python
requester_safe_explanation: str | None = Field(default=None, min_length=1, max_length=1000)
```

`max_length=1000` matches `RequestDenial` and `DisclosureDenial`. This string reaches a requester,
so it is bounded at the producing boundary rather than trusted to be short.

The field is optional so existing semantic-registry failures and persisted records remain valid.
`FulfillmentService` copies the value without deriving or expanding it. A resolver that does not
provide safe copy produces no requester explanation.

The requester read model adds `no_valid_plan_explanation: str | None`. It returns the explanation
only when all of these conditions hold:

- the current state is `RequestState.NO_VALID_PLAN`;
- at least one `RequestNoValidPlan` exists;
- the latest record's `resulting_request_revision` equals the current request revision; and
- the latest record has a non-null requester-safe explanation.

The read model never projects `reason_codes`, `constraint_refs`, `smallest_changes`, grounding
digests, or policy digests to the requester.

#### 4.1.1 The revision guard, and an inconsistency it exposes

The third condition is deliberate and matches `_preparation_notes`, which already refuses to show
an architect a refusal recorded against a superseded revision.

The existing `denial_explanation` projection has **no** such guard: it takes `denials[-1]` whenever
the state is `REJECTED`. The two cannot both be right. This increment does not change the denial
path, because widening or narrowing what a requester is told about a rejection is a separate
decision about a different terminal outcome. It records the difference instead:

> `denial_explanation` may project a denial recorded against an earlier request revision. Whether
> that is a latent staleness defect or intended behaviour is unresolved, and is not decided here.

An implementer must not "make them consistent" in passing. Either both guards are correct for their
own outcome, or the denial path has a defect that deserves its own change, its own red test, and
its own review.

### 4.2 Local scenario guard

Add a governed-console-only authority resolver that wraps the existing `ScenarioAuthorityResolver`.
Its public contract remains:

```python
def resolve(
    self, *, tenant_id: str, request: InboxRequest
) -> FulfillmentAuthorityObservation | ResolutionFailure: ...
```

For the exact supported purpose and question, it delegates unchanged. For every other payload it
returns:

```python
ResolutionFailure(
    reason_codes=("local_scenario_not_supported",),
    constraint_refs=(),
    smallest_changes=(
        "Configure an authoritative source for this question or submit the documented "
        "net-revenue semantic-definition scenario.",
    ),
    requester_safe_explanation=(
        "This local environment has no authoritative source configured for that question."
    ),
)
```

The resolver is composed only by `GovernedConsoleDeployment`. Plan 3B continues to use its existing
scenario resolver directly, preserving its missing-data, unauthorized-answer, and access cases.

Matching is exact after the validation already performed by the request models. The implementation
must not use substring, keyword, case-insensitive, or AI classification. A local fixture allowlist
is deterministic and must not imply general semantic equivalence.

### 4.3 Console projection

The console `RequesterRequestView` adds:

```python
question: NonEmptyText | None = None
no_valid_plan_explanation: NonEmptyText | None = None
```

`GovernedConsoleBackend` reads `question` only from a `StakeholderQuestion` payload and maps the
safe explanation from the service requester view. Generated JSON Schema, TypeScript models, and Ajv
validators are regenerated together.

The fixture backend and TypeScript fixtures use the same public contract. They must not invent a
question for data-access requests.

### 4.4 Intake pre-fill

`RequestIntake` accepts an optional immutable `initialDraft`:

```typescript
interface StakeholderQuestionDraft {
  readonly kind: "stakeholder_question"
  readonly title: string
  readonly purpose: string
  readonly question: string
}
```

Initial state is derived from the draft when the component mounts. The component does not submit
automatically. The existing `buildRequest`, canonical digest, retry, ambiguous-outcome, and success
paths remain unchanged.

## 5. Security and authority

- Only the resolver declares whether its local source can support the question. The browser cannot
  force a request past this decision.
- The answer provider is never invoked after `ResolutionFailure`.
- Requester-safe copy is explicit at the producing boundary. Internal remediation data is not
  treated as safe merely because it is human-readable.
- The requester read service continues to require exact tenant ownership and actor identity.
- The recovery form sends no tenant or actor identity. The server derives both from trusted
  context.
- A revised request receives a new request ID and its own immutable revision history.
- No new dependency, workflow engine, metadata system, or state transition is introduced.

## 6. Failure and boundary behavior

| Condition | Required behavior |
| --- | --- |
| Exact supported semantic-definition question | Existing proposal and approval flow continues |
| MRR or any other stakeholder question | Persist `No Valid Plan`; do not call answer provider |
| Unsupported request has safe explanation | Requester sees only that explanation |
| Failure lacks safe explanation | Requester sees lifecycle state without internal details |
| Data-access request | `question` is null; revised-question action absent |
| Pre-filled form is edited | Digest binds the edited values actually submitted |
| Submission outcome is ambiguous | Existing reconciliation action remains available |
| New request succeeds | New ID at revision 1; old terminal request is unchanged |
| Stale or malformed response | Existing typed error behavior remains unchanged |

## 7. Acceptance criteria

1. A newly submitted request with title `Test`, purpose `This is a test request`, and question
   `What is the current MRR` can be clarified and prepared from the architect UI.
2. Preparation returns a committed `RequestNoValidPlan` and the request reaches terminal state
   `RequestState.NO_VALID_PLAN`. Assert this against the owning repository. (Since 2026-09-10 the
   console also projects it distinctly as `no_valid_plan`; see the note in section 3.1.)
3. The unsupported request has no `FulfillmentProposal` and the scenario answer provider is not
   invoked.
4. The architect sees the internal reason and smallest required change. This is existing
   `_preparation_notes` behaviour reached by a new path, so it is proved as a regression rather than
   claimed as new work.
5. The requester sees the exact submitted question and the requester-safe explanation.
6. **Start revised request** pre-fills the title, purpose, and question but performs no mutation
   until submission.
7. Submitting the recovery form creates a different request ID at revision 1 and leaves the old
   request unchanged.
8. The exact documented net-revenue request still reaches its existing proposal and admission
   states.
9. Data-access requester projections contain no question and show no revised-question action.
10. Strict schema generation, Python checks, frontend checks, offline tests, repository structure,
    and the governed-local Playwright journey pass.
11. Focused mutation checks prove that negating either current-refusal projection guard, widening
    either exact local-scenario comparison, or changing their conjunction causes the designated
    regression tests to fail.
12. Two partial-match cases prove that neither the supported purpose nor the supported question is
    sufficient by itself.

## 8. Scope boundaries

This increment does not add a general question-answering engine, query executor, metric lookup,
authoritative MRR source, answer delivery channel, request reopening, request supersession model,
or data-access recovery form. It does not change Plan 3B's scenario matrix. It makes the current
local capability limit explicit and recoverable.
