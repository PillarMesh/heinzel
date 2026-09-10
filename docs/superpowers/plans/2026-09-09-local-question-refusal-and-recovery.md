# Local Question Refusal and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make governed-local stakeholder questions fail honestly when the configured scenario cannot answer them, explain that outcome safely, and provide a pre-filled new-request recovery path.

**Architecture:** A governed-console-only authority resolver rejects unsupported local questions before candidate generation, while request-management persists and projects a separately modeled requester-safe explanation. The console adds the original question and explanation to its strict requester contract and reuses the existing intake command to create a separate recovery request.

**Tech Stack:** Python 3.13, frozen Pydantic 2 contracts, SQLite request and fulfillment repositories, Starlette console adapter, React 19, TypeScript 6, Vitest, Testing Library, Playwright, Ajv-generated validators, `uv`, and npm.

**Spec:** `docs/superpowers/specs/2026-09-09-local-question-refusal-and-recovery-design.md`

## Global Constraints

- Preserve `Integration Contract -> Semantic IIR -> Physical Plan -> Execution Graph`; the console never owns fulfillment authority.
- Keep `RequestState.NO_VALID_PLAN` terminal and create a new request for recovery.
- Store requester-safe failure copy separately from internal reason codes, constraints, and smallest changes.
- Compose the local allowlist only in `GovernedConsoleDeployment`; do not narrow the Plan 3B acceptance resolver.
- Match the supported purpose `semantic definition` and question `What does net revenue mean?` exactly.
- Never call the answer candidate provider after authority resolution returns `ResolutionFailure`.
- Derive question text from `StakeholderQuestion`; do not derive it from title, purpose, or conversation.
- Do not expose tenant, actor, internal references, candidate answers, or provider details through the requester projection.
- The recovery action pre-fills the existing intake form and performs no mutation until explicit submission.
- Do not add a supersession relationship, reopen transition, external service, or dependency.
- TDD is mandatory: observe every new regression test fail for the expected reason before production edits.
- Keep generated JSON Schema, TypeScript types, and Ajv validators synchronized.
- Preserve unrelated changes and work in an isolated worktree from freshly verified `main`.

---

### Task 1: Persist and project requester-safe `No Valid Plan` copy

**Files:**
- Modify: `services/request-management/src/pillarmesh_request_management/fulfillment_protocols.py`
- Modify: `services/request-management/src/pillarmesh_request_management/fulfillment_policy.py`
- Modify: `services/request-management/src/pillarmesh_request_management/fulfillment_models.py`
- Modify: `services/request-management/src/pillarmesh_request_management/fulfillment_service.py`
- Modify: `services/request-management/src/pillarmesh_request_management/requester_view.py`
- Modify: `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`
- Test: `services/request-management/tests/test_fulfillment_approval.py`
- Test: `services/request-management/tests/test_fulfillment_repository.py`
- Test: `services/request-management/tests/test_requester_view.py`
- Test: `tests/fault-injection/test_inbox_fulfillment_repository.py`

**Interfaces:**
- Consumes: existing `ResolutionFailure`, `NoValidPlanCompilation`, `RequestNoValidPlan`, and `FulfillmentReadService.requester_view` flow.
- Produces: optional `requester_safe_explanation: str | None` on failure and durable outcome models; `RequesterRequestView.no_valid_plan_explanation: str | None`.

- [ ] **Step 1: Add a failing fulfillment-service propagation test**

Create a `ResolutionFailure` with safe copy, make the static snapshot resolver return it during
`propose_answer`, and assert the stored record preserves it:

```python
failure = ResolutionFailure(
    reason_codes=("local_scenario_not_supported",),
    constraint_refs=(),
    smallest_changes=("Configure an authoritative source.",),
    requester_safe_explanation=(
        "This local environment has no authoritative source configured for that question."
    ),
)

result = fulfillment.propose_answer(
    tenant_id="tenant-a",
    request_id=prepared.request_id,
    actor_id="architect-a",
    expected_revision=prepared.revision,
)

assert result.requester_safe_explanation == failure.requester_safe_explanation
```

- [ ] **Step 2: Run the focused test and confirm the missing-field failure**

Run:

```bash
uv run pytest services/request-management/tests/test_fulfillment_approval.py \
  -k requester_safe_explanation -q
```

Expected: FAIL because `ResolutionFailure` rejects the unknown
`requester_safe_explanation` field or `RequestNoValidPlan` lacks it.

- [ ] **Step 3: Add the optional field and copy it through every compilation path**

This mirrors an existing pattern. `RequestDenial` and `DisclosureDenial` already declare
`requester_safe_explanation: str = Field(min_length=1, max_length=1000)`, and `requester_view`
already projects it as `denial_explanation`. Copy that shape; do not invent a second idiom.

Add this field to `ResolutionFailure`, `NoValidPlanCompilation`, and `RequestNoValidPlan`:

```python
requester_safe_explanation: str | None = Field(default=None, min_length=1, max_length=1000)
```

`max_length=1000` matches the two existing denial models. The string reaches a requester, so it is
bounded where it is produced.

In each conversion from `ResolutionFailure` to `NoValidPlanCompilation`, pass:

```python
NoValidPlanCompilation(
    requester_safe_explanation=resolution.requester_safe_explanation,
)
```

In `_store_no_valid_plan`, pass:

```python
RequestNoValidPlan(
    requester_safe_explanation=compilation.requester_safe_explanation,
)
```

Leave compiler-created failures at the default `None` unless that producing boundary already has
explicit requester-safe copy.

- [ ] **Step 4: Run the propagation test green**

Run the command from Step 2.

Expected: PASS, and the request state is `RequestState.NO_VALID_PLAN`.

- [ ] **Step 5: Add failing requester visibility tests**

In the requester read-service tests, store a current `RequestNoValidPlan` with safe copy and assert:

```python
view = reads.requester_view(
    tenant_id="tenant-a",
    request_id=request_id,
    actor_id="requester-a",
)

assert view.no_valid_plan_explanation == (
    "This local environment has no authoritative source configured for that question."
)
assert not hasattr(view, "no_valid_plans")
```

Add a second test whose record uses `requester_safe_explanation=None` and assert the projected value
is `None`. Keep the existing wrong-requester denial assertion.

- [ ] **Step 6: Run requester visibility tests red**

Run:

```bash
uv run pytest services/request-management/tests/test_requester_view.py \
  -k no_valid_plan -q
```

Expected: FAIL because `RequesterRequestView` has no `no_valid_plan_explanation`.

Do **not** align the existing `denial_explanation` projection with this one while you are here. It
has no revision guard, this one does, and reconciling them decides what a requester is told about a
rejection — a separate outcome, deserving its own change and its own review. See §4.1.1 of the spec.

- [ ] **Step 7: Implement the minimum safe requester projection**

Add to the service `RequesterRequestView`:

```python
no_valid_plan_explanation: str | None
```

Compute it only from the current terminal record:

```python
no_valid_plans = self._repository.list_no_valid_plans(tenant_id, request_id)
current_refusal = no_valid_plans[-1] if no_valid_plans else None
no_valid_plan_explanation = (
    current_refusal.requester_safe_explanation
    if request.state is RequestState.NO_VALID_PLAN
    and current_refusal is not None
    and current_refusal.resulting_request_revision == request.revision
    else None
)
```

Return only this string. Do not add internal refusal artifacts to the requester model.

- [ ] **Step 8: Update direct model construction and persistence fixtures**

Add `requester_safe_explanation=None` to direct `RequestNoValidPlan` and service
`RequesterRequestView` constructors where explicit arguments improve contract clarity. Update the
repository round-trip assertion so a non-null safe explanation survives SQLite serialization.
Add `requester_safe_explanation` to the canonical `RequestNoValidPlan` field list in addendum
section 13.7 and state that only this optional, 1,000-character-bounded field may be disclosed by a
requester-facing refusal projection.

- [ ] **Step 9: Kill mutations of both requester projection guards**

Apply each temporary mutation independently in `requester_view.py`:

```python
# Mutant A: terminal-state guard is negated.
request.state is not RequestState.NO_VALID_PLAN

# Mutant B: current-record revision guard is negated.
current_refusal.resulting_request_revision != request.revision
```

Before running each mutant, clear cached bytecode so CPython cannot reuse restored same-size source:

```bash
find services/request-management apps/console/server tests -type d -name __pycache__ \
  -prune -exec rm -rf {} +
uv run pytest services/request-management/tests/test_requester_view.py \
  -k no_valid_plan -q
```

Expected: each mutant makes the focused tests FAIL for the guard it negates. Restore the exact
production expression after each run, clear `__pycache__` again, and rerun the command green. A
surviving mutant requires a stronger test before this task can commit.

- [ ] **Step 10: Run the component suite and commit**

Run:

```bash
uv run pytest services/request-management/tests tests/fault-injection/test_inbox_fulfillment_repository.py -q
uv run ruff check services/request-management tests/fault-injection/test_inbox_fulfillment_repository.py
uv run ruff format --check services/request-management tests/fault-injection/test_inbox_fulfillment_repository.py
uv run mypy
```

Expected: all commands exit 0.

Commit:

```bash
git add services/request-management tests/fault-injection/test_inbox_fulfillment_repository.py
git commit -m "feat(requests): project safe no-valid-plan explanations"
```

### Task 2: Refuse unsupported questions in the governed-local scenario

**Files:**
- Modify: `tests/acceptance/run_console_governed.py`
- Test: `tests/acceptance/test_run_console_governed.py`

**Interfaces:**
- Consumes: Task 1 `ResolutionFailure.requester_safe_explanation`; existing `ScenarioAuthorityResolver.resolve(*, tenant_id, request)` and `AnswerCandidateProvider`.
- Produces: `_ConsoleScenarioAuthorityResolver.resolve(*, tenant_id, request) -> FulfillmentAuthorityObservation | ResolutionFailure`; its composition in `GovernedConsoleDeployment`; optional constructor injection `answer_candidate_provider: AnswerCandidateProvider | None = None` for the existing answer-provider seam.

**Scope note.** Only the resolver and its composition are new. The architect-facing refusal notes
already exist in `GovernedConsoleBackend._preparation_notes`, already guarded on
`resulting_request_revision == request.revision`, so no console change is needed to show them. This
task makes an unsupported local question reach that existing path; the architect assertions below
are regression coverage.

`ScenarioAuthorityResolver` lives in `tests/acceptance/run_plan3b.py`, and
`run_console_governed.py` already imports it from there. The wrapper goes beside the existing import
rather than moving either harness.

- [ ] **Step 1: Write the failing exact MRR regression test**

Add a test-only `_RecordingAnswerProvider` that delegates to `ScenarioAnswerProvider` after
incrementing `calls`. Let `GovernedConsoleDeployment.__init__` accept an optional
`answer_candidate_provider`; compose the supplied provider or the existing default. Build a governed
deployment with the recording provider, submit the user's exact request through the console command
boundary, clarify it through the governed backend, and prepare it through that same boundary:

```python
created = backend.create_request(
    requester_context,
    CreateRequestCommand(
        expected_revision=1,
        request_digest=request_digest,
        active_role="requester",
        title="Test",
        request=StakeholderQuestionInput(
            kind="stakeholder_question",
            purpose="This is a test request",
            question="What is the current MRR",
        ),
    ),
)
clarified = backend.clarify_request(
    architect_context,
    created.request_id,
    RequestClarificationCommand(
        expected_revision=created.revision,
        active_role="data_architect",
        restated_request="Report current monthly recurring revenue.",
        in_scope_summary="The current governed MRR metric only.",
        out_of_scope_summary="Customer-level subscription records.",
    ),
)
prepared = backend.prepare_request_proposal(
    architect_context,
    created.request_id,
    ProposalPreparationCommand(
        expected_revision=clarified.revision,
        active_role="data_architect",
    ),
)

assert prepared.state == "closed"
assert deployment.requests.get(TENANT, created.request_id).state is RequestState.NO_VALID_PLAN
assert deployment.fulfillment_repository.list_proposals(TENANT, created.request_id) == ()
assert deployment.fulfillment_repository.list_no_valid_plans(TENANT, created.request_id)[
    -1
].reason_codes == ("local_scenario_not_supported",)
assert answer_provider.calls == 0
```

Assert the stored safe explanation equals the spec text. The explicit recording-provider assertion
proves candidate generation was never entered; zero proposals alone is only a proxy.

Add a parameterized boundary test for the two partial matches:

```python
@pytest.mark.parametrize(
    ("purpose", "question"),
    (
        ("This is a test request", "What does net revenue mean?"),
        ("semantic definition", "What is the current MRR"),
    ),
)
def test_local_question_requires_both_exact_supported_fields(
    deployment: GovernedConsoleDeployment,
    answer_provider: _RecordingAnswerProvider,
    purpose: str,
    question: str,
) -> None:
    prepared, request_id = _prepare_question(
        deployment,
        purpose=purpose,
        question=question,
    )

    assert prepared.state == "closed"
    assert deployment.requests.get(TENANT, request_id).state is RequestState.NO_VALID_PLAN
    assert deployment.fulfillment_repository.list_proposals(TENANT, request_id) == ()
    assert answer_provider.calls == 0
```

Factor the submit, clarify, and prepare sequence shown above into
`_prepare_question(deployment, *, purpose, question) -> tuple[RequestDetailView, str]`. A fixture
constructs the deployment with the same `_RecordingAnswerProvider` supplied to the test. The two
rows are load-bearing: together they kill an `and`-to-`or` mutation in the allowlist.

- [ ] **Step 2: Run the MRR regression test red**

Run:

```bash
uv run pytest tests/acceptance/test_run_console_governed.py \
  -k unsupported_local_question -q
```

Expected: FAIL because the request currently receives the fixed net-revenue proposal.

- [ ] **Step 3: Implement the local authority wrapper**

Add this focused adapter beside `GovernedConsoleDeployment`:

```python
class _ConsoleScenarioAuthorityResolver:
    def __init__(self, delegate: ScenarioAuthorityResolver) -> None:
        self._delegate = delegate

    def resolve(
        self, *, tenant_id: str, request: InboxRequest
    ) -> FulfillmentAuthorityObservation | ResolutionFailure:
        payload = request.payload
        purpose_matches = payload.purpose == "semantic definition"
        question_matches = (
            isinstance(payload, StakeholderQuestion)
            and payload.question == "What does net revenue mean?"
        )
        if not (purpose_matches and question_matches):
            return ResolutionFailure(
                reason_codes=("local_scenario_not_supported",),
                constraint_refs=(),
                smallest_changes=(
                    "Configure an authoritative source for this question or submit the documented "
                    "net-revenue semantic-definition scenario.",
                ),
                requester_safe_explanation=(
                    "This local environment has no authoritative source configured for that "
                    "question."
                ),
            )
        return self._delegate.resolve(tenant_id=tenant_id, request=request)
```

Import `FulfillmentAuthorityObservation`, `ResolutionFailure`, and `StakeholderQuestion` from their
owning public packages. Compose the wrapper only around the authority resolver passed by
`GovernedConsoleDeployment` to `SemanticFulfillmentSnapshotAdapter`.

- [ ] **Step 4: Run the MRR regression test green**

Run the command from Step 2.

Expected: PASS with no proposal and one committed `RequestNoValidPlan`.

- [ ] **Step 5: Prove the supported scenario and Plan 3B remain unchanged**

Add or retain assertions that the exact supported question compiles to `FulfillmentProposal` and
that the offline Plan 3B result is complete.

Run:

```bash
uv run pytest tests/acceptance/test_run_console_governed.py \
  tests/acceptance/test_run_plan3b.py -q
```

Expected: PASS. This is the boundary test that proves the console wrapper did not narrow the Plan
3B scenario resolver.

- [ ] **Step 6: Kill mutations of the exact local-scenario predicate**

Apply each temporary mutation independently in `_ConsoleScenarioAuthorityResolver.resolve`:

```python
# Mutant A: admit a wrong purpose.
payload.purpose != "semantic definition"

# Mutant B: admit a wrong question.
payload.question != "What does net revenue mean?"

# Mutant C: either matching field is sufficient.
purpose_matches or question_matches
```

The production code names the two exact comparisons as `purpose_matches` and `question_matches` so
Mutant C is a single controlled edit. Before every mutant run, clear cached bytecode and run both
boundary suites:

```bash
find tests/acceptance apps/console/server services -type d -name __pycache__ \
  -prune -exec rm -rf {} +
uv run pytest tests/acceptance/test_run_console_governed.py \
  tests/acceptance/test_run_plan3b.py -q
```

Expected: every mutant makes at least one designated regression test FAIL. Restore the exact `==`
comparisons joined by `and`, clear `__pycache__`, and rerun green. A surviving mutant blocks the
task until its missing boundary test is added.

- [ ] **Step 7: Run focused static checks and commit**

Run:

```bash
uv run ruff check tests/acceptance/run_console_governed.py \
  tests/acceptance/test_run_console_governed.py
uv run ruff format --check tests/acceptance/run_console_governed.py \
  tests/acceptance/test_run_console_governed.py
uv run mypy
```

Expected: all commands exit 0.

Commit:

```bash
git add tests/acceptance/run_console_governed.py \
  tests/acceptance/test_run_console_governed.py
git commit -m "fix(console): refuse unsupported local questions"
```

### Task 3: Expose the original question and safe refusal in the requester console

**Files:**
- Modify: `apps/console/server/src/pillarmesh_console/contracts.py`
- Modify: `apps/console/server/src/pillarmesh_console/governed_backend.py`
- Modify: `apps/console/server/src/pillarmesh_console/fixture_backend.py`
- Modify: `apps/console/server/tests/test_governed_backend.py`
- Modify: `apps/console/server/tests/test_governed_commands.py`
- Modify: `apps/console/server/tests/test_contracts.py`
- Modify: `apps/console/schema/console-api-v1.json`
- Modify: `apps/console/web/src/api/generated.ts`
- Modify: `apps/console/web/src/api/generated-validators.d.ts`
- Modify: `apps/console/web/src/api/generated-validators.js`
- Modify: `apps/console/web/src/features/requests/my-requests.tsx`
- Modify: `apps/console/web/src/features/requests/requester-surface.test.tsx`

**Interfaces:**
- Consumes: Task 1 `ServiceRequesterRequestView.no_valid_plan_explanation`; current request payload.
- Produces: console `RequesterRequestView.question` and `.no_valid_plan_explanation`, synchronized generated browser contracts, and visible requester detail copy.

- [ ] **Step 1: Write failing backend projection tests**

For a `StakeholderQuestion`, assert:

```python
view = next(
    item
    for item in backend.get_requester_requests(requester_context)
    if item.request_id == request.request_id
)

assert view.question == "What is the current MRR"
assert view.no_valid_plan_explanation == (
    "This local environment has no authoritative source configured for that question."
)
```

For a `DataAccessRequest`, assert both fields are `None`. Include an internal reason canary such as
`artifact-private-001` in the architect refusal fixture and assert it is absent from the serialized
requester response.

- [ ] **Step 2: Run backend tests red**

Run:

```bash
uv run pytest apps/console/server/tests/test_governed_backend.py \
  apps/console/server/tests/test_governed_commands.py -k "question or no_valid_plan" -q
```

Expected: FAIL because the console requester contract lacks both fields.

- [ ] **Step 3: Add and map the strict console fields**

Add to console `RequesterRequestView`:

```python
question: NonEmptyText | None = None
no_valid_plan_explanation: NonEmptyText | None = None
```

Map them in `GovernedConsoleBackend._requester_request`:

```python
RequesterRequestView(
    question=(
        request.payload.question if isinstance(request.payload, StakeholderQuestion) else None
    ),
    no_valid_plan_explanation=view.no_valid_plan_explanation,
)
```

Update fixture construction explicitly. Do not derive either field in the browser.

- [ ] **Step 4: Run backend tests green**

Run the command from Step 2.

Expected: PASS, including the privacy canary assertion.

- [ ] **Step 5: Write the failing requester rendering test**

Extend the `ownRequest` fixture and assert the selected request detail displays both values:

```typescript
expect(screen.getByText("What is the current MRR")).toBeVisible()
expect(
  screen.getByText(
    "This local environment has no authoritative source configured for that question.",
  ),
).toBeVisible()
```

Add a data-access fixture and assert `queryByText("Question")` is null for its selected detail.

- [ ] **Step 6: Run the browser component test red**

Run:

```bash
cd apps/console
npm run test -- --run web/src/features/requests/requester-surface.test.tsx
```

Expected: FAIL because `MyRequests` does not render either field.

- [ ] **Step 7: Render labeled question and refusal copy**

In the selected request summary, render the question only when non-null:

```tsx
{selected.question == null ? null : (
  <div className="request-summary__question">
    <p className="eyebrow">Original question</p>
    <p>{selected.question}</p>
  </div>
)}
{selected.no_valid_plan_explanation == null ? null : (
  <p className="request-summary__no-valid-plan" role="status">
    {selected.no_valid_plan_explanation}
  </p>
)}
```

Use existing typography and status tokens in `requests.css`; do not add a new color vocabulary.

- [ ] **Step 8: Regenerate contracts and run drift checks**

Run:

```bash
cd apps/console
npm run generate:contracts
npm run check:contracts
cd ../..
uv run pytest apps/console/server/tests/test_schema.py apps/console/server/tests/test_contracts.py -q
```

Expected: all commands exit 0 and only the schema, generated types, and generated validators change.

- [ ] **Step 9: Run component checks and commit**

Run:

```bash
uv run pytest apps/console/server/tests -q
cd apps/console
npm run lint
npm run typecheck
npm run test -- --run web/src/features/requests/requester-surface.test.tsx
npm run build
```

Expected: all commands exit 0.

Commit:

```bash
git add apps/console/server apps/console/schema apps/console/web/src/api \
  apps/console/web/src/features/requests
git commit -m "feat(console): explain unsupported requester questions"
```

### Task 4: Add the pre-filled new-request recovery action

**Files:**
- Modify: `apps/console/web/src/features/requests/request-intake.tsx`
- Modify: `apps/console/web/src/features/requests/my-requests.tsx`
- Modify: `apps/console/web/src/features/requests/requests.css`
- Modify: `apps/console/web/src/features/requests/requester-surface.test.tsx`

**Interfaces:**
- Consumes: Task 3 `RequesterRequestView.question`; existing `RequesterClient.createRequest`, `canonicalRequestIntakeContent`, and mutation context.
- Produces: exported `StakeholderQuestionDraft`, optional `RequestIntake.initialDraft`, and a `Start revised request` action for terminal unsupported stakeholder questions.

- [ ] **Step 1: Write the failing pre-fill and no-mutation tests**

Render a selected `no_valid_plan` stakeholder question, activate **Start revised request**, and assert:

```typescript
await user.click(screen.getByRole("button", {name: "Start revised request"}))

expect(screen.getByRole("radio", {name: "Stakeholder question"})).toBeChecked()
expect(screen.getByRole("textbox", {name: "Request title"})).toHaveValue("Test")
expect(screen.getByRole("textbox", {name: "Purpose"})).toHaveValue("This is a test request")
expect(screen.getByRole("textbox", {name: "Question"})).toHaveValue(
  "What is the current MRR",
)
expect(client.createRequest).not.toHaveBeenCalled()
```

Add a data-access selected-detail test and assert the action is absent.

- [ ] **Step 2: Run the recovery tests red**

Run:

```bash
cd apps/console
npm run test -- --run web/src/features/requests/requester-surface.test.tsx
```

Expected: FAIL because the recovery action and intake draft do not exist.

- [ ] **Step 3: Add the typed draft and initialize intake state**

Export:

```typescript
export interface StakeholderQuestionDraft {
  readonly kind: "stakeholder_question"
  readonly title: string
  readonly purpose: string
  readonly question: string
}
```

Add `initialDraft?: StakeholderQuestionDraft` to `RequestIntakeProps` and initialize state directly:

```typescript
const [kind, setKind] = useState<RequestKind | null>(initialDraft?.kind ?? null)
const [title, setTitle] = useState(initialDraft?.title ?? "")
const [purpose, setPurpose] = useState(initialDraft?.purpose ?? "")
const [question, setQuestion] = useState(initialDraft?.question ?? "")
```

Do not add an effect that overwrites edits after a reload. `MyRequests` must give the recovery
intake a stable key based on the original request ID.

- [ ] **Step 4: Render recovery inline on the terminal request detail**

Track whether the user opened recovery. Show the button only when:

```typescript
selected.state === "closed" &&
selected.kind === "stakeholder_question" &&
selected.question != null &&
selected.no_valid_plan_explanation != null
```

After activation, render `RequestIntake` with:

```typescript
initialDraft={{
  kind: "stakeholder_question",
  title: selected.title,
  purpose: selected.requested_outcome,
  question: selected.question,
}}
```

On successful creation, reuse `reload` so the new request appears in **My requests**. Keep the old
detail and its terminal state visible until the user navigates.

- [ ] **Step 5: Prove edited content, digest binding, and distinct identity**

In the component test, edit the question to `What was MRR for the last closed month?`, submit, and
assert the captured command contains the edited value and the digest helper received the canonical
content for that value. Return `request-0002` from the fake client and assert the success status
says `Revised request submitted`, exposes a `View request` link to `/requests/request-0002`, and
does not render the identifier, revision, or owning state as user-facing receipt copy. The original
request remains shown unchanged.

Assert the original is unchanged by its **safe explanation still being rendered**, not by a
lifecycle label: the console projects `RequestState.NO_VALID_PLAN` as `closed`, which is also what a
delivered or cancelled request shows, so a state assertion here would pass against the wrong
outcome.

- [ ] **Step 6: Run frontend checks and commit**

Run:

```bash
cd apps/console
npm run lint
npm run typecheck
npm run test -- --run web/src/features/requests/requester-surface.test.tsx
npm run build
```

Expected: all commands exit 0.

Commit:

```bash
git add apps/console/web/src/features/requests
git commit -m "feat(console): prefill revised stakeholder requests"
```

### Task 5: Prove the complete local product journey and update operator guidance

**Files:**
- Create: `apps/console/e2e-governed/unsupported-question.spec.ts`
- Create: `apps/console/playwright.governed.config.ts`
- Create: `apps/console/scripts/serve-governed-e2e.mjs`
- Modify: `apps/console/package.json`
- Modify: `docs/console/testing.md`
- Modify: `docs/console/known-gaps.md`

**Interfaces:**
- Consumes: Tasks 1 through 4 and the existing governed-local server commands.
- Produces: `npm run test:e2e:governed`, a browser regression for the exact MRR scenario, documented supported local scenario, and repeatable commands for starting the updated requester and architect UIs.

- [ ] **Step 1: Add the failing governed-local browser journey**

Create a governed Playwright config with requester base URL `http://127.0.0.1:8131`, one Chromium
worker, and `webServer.command: "node scripts/serve-governed-e2e.mjs"`. The server script must:

1. create a directory with `mkdtemp(join(tmpdir(), "pillarmesh-governed-e2e-"))`;
2. spawn the built governed-local harness twice with `--no-seed`, ports 8130 and 8131, and fixed
   actors `architect-a` and `requester-a`;
3. forward `SIGINT` and `SIGTERM` to both children;
4. close both children and remove only the directory it created; and
5. exit nonzero if either child exits unexpectedly.

Add this package script:

```json
"test:e2e:governed": "npm run build && playwright test -c playwright.governed.config.ts"
```

Then execute a new browser transaction:

```typescript
test("unsupported MRR question stops at No Valid Plan and can seed a new request", async ({
  page,
}) => {
  await page.goto("http://127.0.0.1:8131/requests")
  await page.getByRole("radio", {name: "Stakeholder question"}).check()
  await page.getByRole("textbox", {name: "Request title"}).fill("Test")
  await page.getByRole("textbox", {name: "Purpose"}).fill("This is a test request")
  await page.getByRole("textbox", {name: "Question"}).fill("What is the current MRR")
  await page.getByRole("button", {name: "Submit request"}).click()
  const created = page.getByRole("status")
  await expect(created).toHaveText("Request submitted. View request.")
  await expect(created).not.toContainText("req-")

  await page.goto("http://127.0.0.1:8130/inbox")
  await page.getByRole("option", {name: /Test/}).click()
  await page.getByRole("textbox", {name: "Clarified request"}).fill(
    "Report current monthly recurring revenue.",
  )
  await page.getByRole("textbox", {name: "In scope"}).fill(
    "The current governed MRR metric only.",
  )
  await page.getByRole("textbox", {name: "Out of scope"}).fill(
    "Customer-level subscription records.",
  )
  await page.getByRole("button", {name: "Record clarification"}).click()
  await page.getByRole("button", {name: "Prepare answer proposal"}).click()

  const detail = page.getByRole("region", {name: "Request detail"})
  // The lifecycle label reads "closed": the console maps `RequestState.NO_VALID_PLAN`
  // to `closed` and renders `state.replaceAll("_", " ")`, so asserting "no valid plan"
  // here fails on both the value and the capitalisation of the note below. The reason
  // code is what distinguishes this terminal state from a delivered one.
  await expect(detail).toContainText("local_scenario_not_supported")
  await expect(detail).toContainText("No Valid Plan: local_scenario_not_supported.")
  await expect(detail).toContainText(
    "Required change: Configure an authoritative source for this question",
  )
  await expect(detail).not.toContainText(
    "Net revenue is gross revenue less approved refunds.",
  )

  await page.goto("http://127.0.0.1:8131/requests")
  await page.getByRole("link", {name: "Open request"}).first().click()
  await expect(page.getByText("What is the current MRR")).toBeVisible()
  await expect(
    page.getByText(
      "This local environment has no authoritative source configured for that question.",
    ),
  ).toBeVisible()
  await page.getByRole("button", {name: "Start revised request"}).click()
  await expect(page.getByRole("textbox", {name: "Question"})).toHaveValue(
    "What is the current MRR",
  )
})
```

Continue the test by editing the question and submitting the form. Read both request IDs from the
`View request` link destinations without displaying them in receipt copy. Assert the destinations
differ, and prove the second request is revision 1 at the owning repository boundary. Add an
HTTP-only test fixture route only if needed to query the owning repository; it must be registered
only by the governed E2E harness and return counts and lifecycle state, never artifact content.
Prefer asserting the persisted terminal record in `test_run_console_governed.py` and using the
browser suite for the UI boundary. Do not infer the original terminal success from a toast alone.

- [ ] **Step 2: Run the new journey red**

Run:

```bash
cd apps/console
npm run test:e2e:governed -- --grep "unsupported MRR question"
```

Expected: FAIL before the implementation because the old path produces a proposal or lacks the
requester recovery controls.

- [ ] **Step 3: Complete browser assertions for both roles**

Assert the architect sees `local_scenario_not_supported` and the required change. Assert the
requester sees the exact original question and only the safe explanation. Assert the fixed answer
`Net revenue is gross revenue less approved refunds.` never appears on either unsupported-request
page.

Assert on the reason code and the note text, never on the lifecycle label. `NO_VALID_PLAN`,
`DELIVERED`, `MONITORING` and `CANCELLED` all render as `closed`, so a lifecycle assertion cannot
tell a refusal from a delivered answer and would pass against the wrong outcome.

The architect notes themselves are **not new** — `_preparation_notes` already emits them, already
revision-guarded. These assertions are regression coverage proving a local question now reaches that
path; they are not evidence that this increment built the architect projection.

- [ ] **Step 4: Document the delivered boundary and test procedure**

In `docs/console/testing.md`, document:

- the exact supported local question;
- unsupported questions terminate as `No Valid Plan`;
- the two loopback URLs and actor roles;
- use of a fresh state directory for repeatable verification; and
- the terminal repository assertions required after the browser flow.

In `docs/console/known-gaps.md`, replace any statement implying arbitrary local stakeholder answers
work. State that authoritative sources for arbitrary factual questions and answer delivery remain
undelivered. Do not claim MRR is available.

- [ ] **Step 5: Run focused browser tests green**

Run:

```bash
cd apps/console
npm run test:e2e:governed -- --grep "unsupported MRR question"
npm run test:e2e
```

The first command proves the governed-local two-role journey. The second preserves the existing
fixture, accessibility, screenshot, and denial coverage.

Expected: all included tests pass; existing explicitly skipped tests remain reported as gaps.

- [ ] **Step 6: Run the complete offline verification gates**

Run:

```bash
uv sync --locked --all-packages
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
cd apps/console
npm run check:contracts
npm run lint
npm run typecheck
npm run test -- --run
npm run build
npm run test:e2e
npm run test:e2e:governed
```

Expected: every command exits 0. Report every Playwright skip by test name; a skip is a gap rather
than passing evidence.

- [ ] **Step 7: Review security and final diff**

Run:

```bash
git diff --check
git status --short
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- \
  services/request-management tests/acceptance apps/console docs/console
```

Confirm the diff has no `.playwright-cli`, databases, state directories, credentials, raw provider
responses, unrelated files, or generated caches. Confirm no requester response contains internal
constraint references or a candidate answer.

- [ ] **Step 8: Commit the journey and documentation**

```bash
git add apps/console/e2e-governed apps/console/playwright.governed.config.ts \
  apps/console/scripts/serve-governed-e2e.mjs apps/console/package.json docs/console
git commit -m "test(console): verify unsupported question recovery"
```

- [ ] **Step 9: Restart the two local product UIs for manual testing**

Stop only the requester and architect processes recorded in the current product-test session. Start
fresh governed-local servers from the implementation worktree with a new state directory, binding
requester to `127.0.0.1:8131` and architect to `127.0.0.1:8130`. Verify:

```bash
curl --fail --silent http://127.0.0.1:8130/healthz
curl --fail --silent http://127.0.0.1:8131/healthz
```

Expected: both commands return the healthy response. Open `/requests` for the requester and `/inbox`
for the architect. Perform one new exact MRR transaction and trace it to its persisted terminal
state before declaring the UI ready for testing.
