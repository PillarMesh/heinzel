# Data Architect Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the first production-shaped PillarMesh console: a complete fixture-backed architect and requester journey, deterministic screenshots, and an honest governed-local adapter over capabilities already implemented on `main`.

**Architecture:** `apps/console/web` is a React/TypeScript SPA. `apps/console/server` is a thin Starlette adapter whose strict Pydantic contracts generate the browser's JSON Schema and TypeScript vocabulary. Both `FixtureConsoleBackend` and `GovernedConsoleBackend` implement one `ConsoleBackend` protocol; services remain authoritative and no real failure falls back to fixture data.

**Tech Stack:** Python 3.13, Pydantic 2, Starlette 1.6, Uvicorn 0.52, Node 24.20.0 Active LTS, React 19.2.8, React Router 7.18.3, TypeScript 6.0.3, Vite 8.2.2, Ajv 8.20.0, Vitest 4.1.11, Testing Library, Playwright 1.62.1, axe-core, npm lockfiles, CSS custom properties.

**Spec:** `docs/superpowers/specs/2026-09-01-data-architect-console-design.md`

## Global Constraints

- Preserve `Integration Contract -> Semantic IIR -> Physical Plan -> Execution Graph`; the console never owns semantic, legality, execution, or evidence authority.
- Put product composition only in `apps/console`; call public service interfaces rather than service tables when an interface exists.
- Use `apps/console/.node-version` with exact content `24.20.0`; never add a root `.node-version`.
- When the host Node is not 24.20.0, run npm under the pin with `npx --yes node@24.20.0 "$(command -v npm)"`; CI uses `actions/setup-node` with the same exact version.
- Every API model is frozen Pydantic with `extra="forbid"`; every browser response is Ajv-validated before rendering.
- `data_provenance` is exactly `demo_fixture` or `governed_local` and is visible on every product route.
- Never expose credentials, endpoints, provider-local or private operation identities, raw provider responses, source values, or unauthorized proposal content.
- Derive tenant and actor from trusted server context. Browser JSON never supplies either as authority.
- `Idempotency-Key` correlates and serializes concurrent submissions only. Owning service domain identity remains replay authority.
- No optimistic success. Pending, accepted, ambiguous, fixture, unavailable, and completed states remain distinct.
- `No Valid Plan`, stale revision, authorization denial, transient failure, malformed response, and ambiguous outcome remain distinct typed states.
- TDD is mandatory: write each behavioral test, run it red for the expected reason, then add minimum production code and run it green.
- Fixture content is synthetic, deterministic, resettable, and permanently labeled. Governed-local failures never fall back to fixtures.
- The architect surface uses previews and authenticated deep links. Governed Superset embedding remains an explicit outstanding analyst-surface MVP obligation.
- No UI framework, remote font, charting library, general state library, WebSocket, SSE, SQL editor, DAG canvas, catalog authoring, or dashboard authoring.
- Included journeys must meet WCAG 2.2 AA contrast, keyboard, focus, labeling, reduced-motion, and status-announcement requirements.
- Keep all existing Python gates. Add npm lock, lint, typecheck, schema drift, Vitest, production build, Playwright, accessibility, and screenshot gates.

---

### Task 1: Instantiate the console component and toolchains

**Files:**
- Modify: `.gitignore`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `apps/console/README.md`
- Create: `apps/console/.node-version`
- Create: `apps/console/package.json`
- Create: `apps/console/package-lock.json`
- Create: `apps/console/tsconfig.json`
- Create: `apps/console/vite.config.ts`
- Create: `apps/console/eslint.config.js`
- Create: `apps/console/.npmrc`
- Create: `apps/console/web/index.html`
- Create: `apps/console/web/src/main.tsx`
- Create: `apps/console/web/src/app.tsx`
- Create: `apps/console/web/src/test/setup.ts`
- Create: `apps/console/server/pyproject.toml`
- Create: `apps/console/server/src/pillarmesh_console/__init__.py`
- Create: `apps/console/server/src/pillarmesh_console/app.py`
- Create: `apps/console/server/src/pillarmesh_console/py.typed`
- Test: `apps/console/server/tests/test_app.py`
- Test: `apps/console/web/src/app.test.tsx`

**Interfaces:**
- Produces: importable `pillarmesh_console`, `create_app() -> Starlette`, npm scripts `lint`, `typecheck`, `test`, `build`, `dev`, and `test:e2e`.
- Consumes: no console interfaces.

- [ ] **Step 1: Write the failing Python smoke test**

```python
def test_create_app_exposes_loopback_health_without_product_state() -> None:
    response = TestClient(create_app()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 2: Run the Python test red**

Run: `uv run pytest apps/console/server/tests/test_app.py -q`
Expected: FAIL because `pillarmesh_console` is not importable.

- [ ] **Step 3: Write the failing browser smoke test**

```tsx
test("renders the PillarMesh product landmark", () => {
  render(<App />)
  expect(screen.getByRole("main")).toHaveAccessibleName("PillarMesh console")
})
```

- [ ] **Step 4: Create the minimum workspace and app shells**

Add `apps/console/server` to `[tool.uv.workspace].members` and `pillarmesh_console` to mypy packages. Add `node_modules/`, `apps/console/dist/`, `apps/console/test-results/`, and `apps/console/playwright-report/` to `.gitignore`. Set `.node-version` to `24.20.0`, `.npmrc` to `engine-strict=true`, and `package.json.engines.node` to `24.20.0`. Use these exact npm versions: `react@19.2.8`, `react-dom@19.2.8`, `react-router-dom@7.18.3`, `ajv@8.20.0`, `@vitejs/plugin-react@6.1.1`, `vite@8.2.2`, `typescript@6.0.3`, `vitest@4.1.11`, `jsdom@30.0.1`, `@testing-library/react@16.3.3`, `@testing-library/jest-dom@7.0.1`, `@testing-library/user-event@14.6.6`, `eslint@10.9.1`, `@eslint/js@10.0.1`, `typescript-eslint@8.69.0`, `eslint-plugin-react-hooks@7.1.1`, `eslint-plugin-react-refresh@0.5.5`, `@playwright/test@1.62.1`, `@axe-core/playwright@4.13.0`, and `json-schema-to-typescript@16.0.0`. Declare direct Python dependencies `pydantic>=2.11,<3`, `starlette>=1.6,<2`, and `uvicorn>=0.52,<1`. Record the direct dependencies' MIT, ISC, BSD, or Apache-compatible licenses and their purpose in `apps/console/README.md`; stop this task if a direct dependency is incompatible or unmaintained. Serve only `/healthz` from the initial Starlette app and render only the accessible main landmark from React.

- [ ] **Step 5: Lock and run focused green checks**

Run:

```bash
uv lock
uv sync --locked --all-packages
cd apps/console && npm install
cd ../.. && uv run pytest apps/console/server/tests/test_app.py -q
cd apps/console && npm run test -- --run web/src/app.test.tsx
npm run typecheck
npm run lint
npm run build
```

Expected: all commands exit 0.

- [ ] **Step 6: Verify repository structure and commit**

Run: `./tests/repository-structure/test.sh`
Expected: `OK: repository structure is valid`.

Commit: `feat(console): establish application foundation`

### Task 2: Define the strict console contract and generated browser vocabulary

**Files:**
- Create: `apps/console/server/src/pillarmesh_console/contracts.py`
- Create: `apps/console/server/src/pillarmesh_console/schema.py`
- Create: `apps/console/server/tests/test_contracts.py`
- Create: `apps/console/server/tests/test_schema.py`
- Create: `apps/console/schema/console-api-v1.json`
- Create: `apps/console/scripts/generate-contracts.mjs`
- Create: `apps/console/web/src/api/generated.ts`
- Create: `apps/console/web/src/api/schema.ts`
- Create: `apps/console/web/src/api/schema.test.ts`
- Modify: `apps/console/package.json`

**Interfaces:**
- Produces: `ConsoleEnvelope[T]`, `SessionView`, `WorkspaceView`, `SetupView`, `ReviewView`, `InboxView`, `RequestDetailView`, `RequesterRequestView`, `ConversationView`, `ClarifiedOutcomeView`, `OperationView`, command models, `ConsoleApiSchema`, and generated TypeScript types.
- Consumes: Pydantic only.

- [ ] **Step 1: Write strict-model and privacy boundary tests**

```python
def test_decision_rejects_browser_supplied_tenant_authority() -> None:
    with pytest.raises(ValidationError):
        DecisionCommand.model_validate(
            {
                "tenant_id": "tenant-other",
                "expected_revision": 3,
                "reviewed_digest": "a" * 64,
                "active_role": "data_architect",
                "decision": "approve",
            }
        )


def test_fixture_envelope_cannot_claim_real_evidence() -> None:
    with pytest.raises(ValidationError, match="fixture responses cannot carry evidence"):
        ConsoleEnvelope[OperationView](
            meta=ApiMeta(data_provenance="demo_fixture", correlation_id="corr-0001"),
            data=OperationView(operation_id="op-0001", state="succeeded", evidence_ref="ev-0001"),
        )
```

- [ ] **Step 2: Run contract tests red**

Run: `uv run pytest apps/console/server/tests/test_contracts.py -q`
Expected: FAIL because the contract module is absent.

- [ ] **Step 3: Add exact closed vocabularies and projections**

Use these exact literals:

```python
type DataProvenance = Literal["demo_fixture", "governed_local"]
type ActorRole = Literal[
    "requester", "data_architect", "data_owner", "policy_approver", "budget_approver"
]
type CapabilityState = Literal["ready", "blocked", "degraded", "not_delivered"]
type OperationState = Literal["accepted", "running", "succeeded", "failed", "outcome_unknown"]
type ReviewKind = Literal["meaning", "data_product", "activation"]
type RequestKind = Literal["stakeholder_question", "data_access"]
type RequestState = Literal[
    "submitted",
    "clarifying",
    "investigating",
    "proposed",
    "awaiting_approval",
    "execution_ready",
    "denied",
    "closed",
]


class DecisionCommand(StrictModel):
    expected_revision: int = Field(ge=1)
    reviewed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_role: ActorRole
    decision: Literal["approve", "reject", "request_changes"]
```

All public identifiers use `Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")`; digests use lowercase 64-character SHA-256 patterns. Define every feature projection as a typed frozen model, not `dict[str, object]`. `SessionView` contains actor display data, roles, active role, tenant/workspace display references, and CSRF token. It contains no command tenant or actor fields.

- [ ] **Step 4: Generate JSON Schema and TypeScript and prove drift detection**

`python -m pillarmesh_console.schema --output apps/console/schema/console-api-v1.json` writes canonical sorted JSON. `npm run generate:contracts` runs `json-schema-to-typescript` with deterministic banner suppression. `npm run check:contracts` regenerates into temporary files and exits nonzero on any diff.

Run the schema test once before generation; expected FAIL for missing snapshots. Generate files, then rerun:

```bash
uv run pytest apps/console/server/tests/test_schema.py -q
cd apps/console && npm run check:contracts
npm run test -- --run web/src/api/schema.test.ts
```

Expected: PASS. The browser test must feed an unknown field and an invalid enum to Ajv and assert both are rejected before data reaches a feature.

- [ ] **Step 5: Run package checks and commit**

Run: `uv run mypy && cd apps/console && npm run lint && npm run typecheck && npm run test -- --run`
Expected: all exit 0.

Commit: `feat(console): define strict API contract`

### Task 3: Implement secure Starlette routing and the deterministic fixture backend

**Files:**
- Create: `apps/console/server/src/pillarmesh_console/auth.py`
- Create: `apps/console/server/src/pillarmesh_console/backend.py`
- Create: `apps/console/server/src/pillarmesh_console/errors.py`
- Create: `apps/console/server/src/pillarmesh_console/fixture_backend.py`
- Create: `apps/console/server/src/pillarmesh_console/fixture_data.py`
- Create: `apps/console/server/src/pillarmesh_console/routes/__init__.py`
- Create: `apps/console/server/src/pillarmesh_console/routes/read.py`
- Create: `apps/console/server/src/pillarmesh_console/routes/commands.py`
- Modify: `apps/console/server/src/pillarmesh_console/app.py`
- Test: `apps/console/server/tests/test_fixture_backend.py`
- Test: `apps/console/server/tests/test_routes.py`
- Test: `apps/console/server/tests/test_security.py`

**Interfaces:**
- Produces: `ConsoleBackend` protocol covering every route in spec section 6.5; `FixtureConsoleBackend`; `TrustedActorContext`; typed error envelope; security headers; demo-only reset route.
- Consumes: Task 2 contract models.

- [ ] **Step 1: Write failing lifecycle and denial tests**

Test these literal outcomes against a real `FixtureConsoleBackend`: fresh setup begins at `foundation`; confirming PostgreSQL returns `accepted`, never `succeeded`; repeating the same domain command returns the same operation; changing the immutable engine raises `ConsoleConflict(code="immutable_warehouse_binding")`; a requester projection excludes answer candidate, effective access scope, reviewer identities, and evidence; an unaccepted clarification blocks the architect action.

- [ ] **Step 2: Run fixture tests red**

Run: `uv run pytest apps/console/server/tests/test_fixture_backend.py -q`
Expected: FAIL because the backend is absent.

- [ ] **Step 3: Implement deterministic fixture state**

Use a frozen seed plus a lock-protected process-local state object. Inject a fixed clock and monotonic sequence allocator. Use domain identities `(workspace_id, resource_id, expected_revision, digest)` for replay. Use `Idempotency-Key` only in an in-flight key set protected by the same lock. Include both warehouse engines, seven setup stages, three approval gates, stakeholder-answer and access fixtures, stale and `No Valid Plan` fixtures, one blocked requester-acceptance item, and typed unavailable downstream capabilities.

- [ ] **Step 4: Write routes and security tests red**

Tests must prove: session returns a bound CSRF token; commands reject a missing or wrong token; command JSON containing `tenant_id` or `actor_id` is rejected; wrong-role and wrong-tenant reads both return a non-enumerating 404 envelope; `POST /demo/reset` exists only in fixture mode; CSP has `default-src 'self'; img-src 'self'; frame-src 'self'`; raw exceptions and canary strings never appear.

- [ ] **Step 5: Implement route translation and safe errors**

Register every read and command route from spec section 6.5. Add `GET /api/v1/previews/{preview_ref}` for same-origin image bytes and `GET /api/v1/links/{link_ref}` for an authorized redirect; both consume opaque tenant-scoped references and return non-enumerating 404 for wrong actors. Map validation to 422, unauthenticated to 401, non-enumerating denial to 404, stale or domain conflict to 409, transient unavailability to 503, and accepted long work to 202. Every envelope receives server-generated provenance and correlation ID. Mint opaque console operation handles and keep private operation mappings inside the backend.

- [ ] **Step 6: Run focused and package checks, then commit**

Run:

```bash
uv run pytest apps/console/server/tests -q
uv run ruff check apps/console/server
uv run ruff format --check apps/console/server
uv run mypy
```

Expected: all exit 0.

Commit: `feat(console): add secure fixture API`

### Task 4: Build the validated browser client and product shell

**Files:**
- Create: `apps/console/web/src/api/client.ts`
- Create: `apps/console/web/src/api/client.test.ts`
- Create: `apps/console/web/src/components/app-shell.tsx`
- Create: `apps/console/web/src/components/mode-banner.tsx`
- Create: `apps/console/web/src/components/capability-state.tsx`
- Create: `apps/console/web/src/components/error-boundary.tsx`
- Create: `apps/console/web/src/components/skip-link.tsx`
- Create: `apps/console/web/src/components/capability-summary-page.tsx`
- Create: `apps/console/web/src/routes/router.tsx`
- Create: `apps/console/web/src/routes/loading-page.tsx`
- Create: `apps/console/web/src/routes/recovery-page.tsx`
- Create: `apps/console/web/src/styles/tokens.css`
- Create: `apps/console/web/src/styles/global.css`
- Modify: `apps/console/web/src/app.tsx`
- Test: `apps/console/web/src/components/app-shell.test.tsx`
- Test: `apps/console/web/src/routes/router.test.tsx`

**Interfaces:**
- Produces: `ConsoleApiClient`, `AppShell`, server-authorized landing-route selection, persistent provenance banner, capability status, fail-closed malformed-response page.
- Consumes: generated Task 2 types and Ajv validators; Task 3 routes.

- [ ] **Step 1: Write failing API boundary tests**

Test real `fetch` responses through a stub transport: valid envelope returns typed data; unknown field and invalid state throw `MalformedConsoleResponse`; 409 stale returns `ConsoleApiError` with recovery action; mutating calls send CSRF, exact digest, expected revision, active role, and `Idempotency-Key`; no method accepts tenant or actor parameters.

- [ ] **Step 2: Run client tests red, then implement the minimum client**

Run: `cd apps/console && npm run test -- --run web/src/api/client.test.ts`
Expected: FAIL because `ConsoleApiClient` is absent.

Use native fetch, generated types, and Ajv. Never return raw JSON from the client.

- [ ] **Step 3: Write shell and routing tests red**

Test that incomplete setup routes to `/setup`, pending activation routes to its review, activated workspace routes to `/inbox`, unavailable workspace routes to recovery, and fixture mode shows `Demo scenario - no managed effects` on every route. Test `/data-products`, `/runs`, `/catalog`, `/dashboards`, and `/evidence` as typed capability-summary routes rather than blank placeholders. Test landmarks, skip link, visible focus, active navigation, and no hover-only action.

- [ ] **Step 4: Implement shell, tokens, and responsive route composition**

Use deep navy `#10213c`, primary blue `#2563eb`, surface `#f8fafc`, text `#172033`, and semantic colors that pass AA with text labels. Three-pane layouts collapse evidence into a drawer below 1120px and become queue-detail below 760px. Honor `prefers-reduced-motion`.

- [ ] **Step 5: Run browser checks and commit**

Run: `cd apps/console && npm run lint && npm run typecheck && npm run test -- --run && npm run build`
Expected: all exit 0.

Commit: `feat(console): add governed product shell`

### Task 5: Implement the A2 architect setup workbench

**Files:**
- Create: `apps/console/web/src/features/setup/setup-workbench.tsx`
- Create: `apps/console/web/src/features/setup/foundation-stage.tsx`
- Create: `apps/console/web/src/features/setup/services-stage.tsx`
- Create: `apps/console/web/src/features/setup/sources-stage.tsx`
- Create: `apps/console/web/src/features/setup/process-stage.tsx`
- Create: `apps/console/web/src/features/setup/review-stage.tsx`
- Create: `apps/console/web/src/features/setup/activation-stage.tsx`
- Create: `apps/console/web/src/features/setup/operation-status.tsx`
- Create: `apps/console/web/src/features/setup/setup.css`
- Test: `apps/console/web/src/features/setup/setup-workbench.test.tsx`
- Test: `apps/console/web/src/features/setup/review-stage.test.tsx`

**Interfaces:**
- Produces: seven-stage A2 workbench, immutable warehouse review, process upload, three exact approval gates, operation polling presentation.
- Consumes: `SetupView`, `ReviewView`, `OperationView`, and Task 4 client.

- [ ] **Step 1: Write the foundation tests red**

Test PostgreSQL and ClickHouse choices, immutable-effect confirmation text, accepted operation shown as `Provisioning accepted` rather than success, engine-change conflict, stage progress from server state, and not-delivered Superset shown disabled with dependency text.

- [ ] **Step 2: Implement foundation, services, sources, and operation status green**

Run the focused test before and after implementation. Poll only accepted/running/outcome-unknown operations with bounded intervals `1s, 2s, 4s, 8s, 15s`; stop on terminal state or route unmount. Preserve the same operation handle and idempotency key after an ambiguous response.

- [ ] **Step 3: Write process and review tests red**

Test strict accepted file type/size messaging, original-content digest display, unresolved semantic question, material-edit invalidation warning, named role list, exact digest confirmation, no optimistic approval, stale refresh preserving an unsubmitted comment, and `No Valid Plan` constraints with permitted next action.

- [ ] **Step 4: Implement process, meaning, product, and activation views green**

Render typed sections, never generic object dumps. A single confirmation may submit several role decisions only when the session has every enumerated role. After command response, replace the view only with the returned authoritative projection.

- [ ] **Step 5: Run checks and commit**

Run: `cd apps/console && npm run lint && npm run typecheck && npm run test -- --run web/src/features/setup && npm run build`
Expected: all exit 0.

Commit: `feat(console): add architect setup workbench`

### Task 6: Implement the Q1 architect decision workspace

**Files:**
- Create: `apps/console/web/src/features/inbox/decision-workspace.tsx`
- Create: `apps/console/web/src/features/inbox/decision-queue.tsx`
- Create: `apps/console/web/src/features/inbox/stakeholder-answer-review.tsx`
- Create: `apps/console/web/src/features/inbox/access-preview-review.tsx`
- Create: `apps/console/web/src/features/inbox/evidence-drawer.tsx`
- Create: `apps/console/web/src/features/inbox/lifecycle-timeline.tsx`
- Create: `apps/console/web/src/features/inbox/catalog-evidence.tsx`
- Create: `apps/console/web/src/features/inbox/dashboard-preview.tsx`
- Create: `apps/console/web/src/features/inbox/conversation-panel.tsx`
- Create: `apps/console/web/src/features/inbox/inbox.css`
- Test: `apps/console/web/src/features/inbox/decision-workspace.test.tsx`
- Test: `apps/console/web/src/features/inbox/reviews.test.tsx`

**Interfaces:**
- Produces: risk/deadline/dependency queue, typed answer/access reviews, evidence and timeline, still-image preview, blocked-on-requester state.
- Consumes: `InboxView`, `RequestDetailView`, Task 4 client.

- [ ] **Step 1: Write queue and keyboard tests red**

Test server order preservation, type/state filters, roving keyboard selection, focus transfer to detail, blocked-on-requester label, and absence of architect approval when requester acceptance is missing.

- [ ] **Step 2: Implement queue and workspace green**

Use queue, detail, and evidence regions with accessible names. At medium width, evidence becomes a focus-managed drawer; at narrow width, selection navigates to a detail route and Back restores queue focus.

- [ ] **Step 3: Write proposal and evidence tests red**

Stakeholder answer must show purpose, candidate, metric version, as-of time, freshness, quality limitation, datasets, lineage, authorization, and required roles. Access preview must show purpose, effective scope, exclusions, expiry, intended checks, denied checks, and authority. Dashboard preview accepts only a same-origin opaque image route, shows unavailable state on render failure, and never inserts a Superset URL. OpenMetadata and Superset deep links render only when the server supplies an authenticated, same-tenant opaque redirect route; the browser never builds a provider URL.

- [ ] **Step 4: Implement typed review components green**

Render requester conversation in its own panel. Disable an approval when required evidence is unavailable. Stale decisions refresh exact revisions; transient errors retain prior authoritative content; malformed payloads fail the affected view closed.

- [ ] **Step 5: Run checks and commit**

Run: `cd apps/console && npm run lint && npm run typecheck && npm run test -- --run web/src/features/inbox && npm run build`
Expected: all exit 0.

Commit: `feat(console): add architect decision workspace`

### Task 7: Implement the bounded requester surface

**Files:**
- Create: `apps/console/web/src/features/requests/request-intake.tsx`
- Create: `apps/console/web/src/features/requests/my-requests.tsx`
- Create: `apps/console/web/src/features/requests/request-conversation.tsx`
- Create: `apps/console/web/src/features/requests/clarified-outcome.tsx`
- Create: `apps/console/web/src/features/requests/requests.css`
- Modify: `apps/console/web/src/routes/router.tsx`
- Test: `apps/console/web/src/features/requests/requester-surface.test.tsx`
- Test: `apps/console/server/tests/test_requester_routes.py`

**Interfaces:**
- Produces: typed stakeholder-question/data-access intake, direct clarification conversation, clarified-outcome acceptance, own-request follow view.
- Consumes: Task 3 requester routes and Task 4 client.

- [ ] **Step 1: Write privacy and intake tests red**

Test required purpose and payload fields, explicit request-type selection, own-request-only list, no candidate answer, no effective access scope, no reviewer metadata, no evidence, and no cross-request existence leak.

- [ ] **Step 2: Implement intake and own-request views green**

The server, not browser, supplies requester identity. A submitted request returns its authoritative revision. The UI shows requested outcome and lifecycle state, not internal work.

- [ ] **Step 3: Write clarification and acceptance tests red**

Test requester reply, architect observation/intervention/takeover, exact clarified-outcome digest display, no proposal while unaccepted, acceptance bound to requester role and expected revision, and proposal availability only after committed acceptance.

- [ ] **Step 4: Implement conversation and acceptance green**

Render user text as text only. Distinguish PillarMesh question, requester reply, and architect intervention by label as well as color. Do not expose later verified answer until a delivery receipt exists.

- [ ] **Step 5: Run checks and commit**

Run:

```bash
uv run pytest apps/console/server/tests/test_requester_routes.py -q
cd apps/console && npm run lint && npm run typecheck && npm run test -- --run web/src/features/requests && npm run build
```

Expected: all exit 0.

Commit: `feat(console): add bounded requester experience`

### Task 8: Add fixture browser acceptance, accessibility, screenshots, and console CI

**Files:**
- Create: `apps/console/playwright.config.ts`
- Create: `apps/console/e2e/architect-journey.spec.ts`
- Create: `apps/console/e2e/requester-journey.spec.ts`
- Create: `apps/console/e2e/accessibility.spec.ts`
- Create: `apps/console/e2e/screenshots.spec.ts`
- Create: `apps/console/scripts/dev.mjs`
- Create: `apps/console/scripts/serve-built.mjs`
- Create: `apps/console/docs/screenshots/README.md`
- Create: `apps/console/docs/screenshots/warehouse-foundation.png`
- Create: `apps/console/docs/screenshots/provisioning-progress.png`
- Create: `apps/console/docs/screenshots/meaning-review.png`
- Create: `apps/console/docs/screenshots/activation-review.png`
- Create: `apps/console/docs/screenshots/command-center.png`
- Create: `apps/console/docs/screenshots/stakeholder-answer.png`
- Create: `apps/console/docs/screenshots/access-preview.png`
- Create: `apps/console/docs/screenshots/no-valid-plan.png`
- Create: `.github/workflows/console.yml`
- Create: `tests/ci/test_console_workflow.py`
- Modify: `apps/console/package.json`
- Modify: `apps/console/README.md`
- Modify: `apps/console/server/src/pillarmesh_console/app.py`

**Interfaces:**
- Produces: one-command local demo, production same-origin build proof, deterministic visual evidence, path-filtered fail-closed CI gate.
- Consumes: Tasks 1-7.

- [ ] **Step 1: Write workflow contract tests red**

Mirror the existing warehouse detector pattern. Assert the workflow always has `changes` and `gate` jobs; unknown range yields `affected=true`; console paths, package files, server pyproject, root Python lock/config, console spec/plan, and the workflow itself are affected; unrelated docs are false; gate requires decided detector and matching test result.

- [ ] **Step 2: Implement the console workflow green**

The affected job runs Node 24.20.0, `npm ci`, `npm run lint`, `npm run typecheck`, `npm run check:contracts`, `npm run test -- --run`, `npm run build`, installs the pinned Playwright Chromium, and runs `npm run test:e2e`. Keep Python workflows unchanged.

- [ ] **Step 3: Write Playwright journeys red**

Implement the exact 14-step spec acceptance journey plus ClickHouse binding coverage. Use role switches only in persistent fixture-mode chrome. Assert no completion before terminal response, blocked requester acceptance, stale recovery, `No Valid Plan`, reset, and fixture banner on every route.

- [ ] **Step 4: Add accessibility and screenshot assertions**

Run axe on every major route and keyboard-only assertions for setup, queue, drawers, dialogs, intake, clarification, and decisions. Fix clock, fixture IDs, browser locale, reduced motion, viewport, and animations. Capture the eight named desktop screenshots at 1440x1024 and medium evidence-drawer screenshots at 1024x768.

- [ ] **Step 5: Prove local and built demos**

Run:

```bash
cd apps/console
npm run test:e2e
npm run build
npm run serve:built -- --check
```

Expected: Playwright passes, screenshots match, and the built server returns the SPA plus `/api/v1/session` from one origin.

- [ ] **Step 6: Commit**

Commit: `test(console): prove fixture architect journey`

### Task 9: Add governed-local projections without fallback

**Files:**
- Create: `apps/console/server/src/pillarmesh_console/governed_backend.py`
- Create: `apps/console/server/src/pillarmesh_console/governed_adapters.py`
- Create: `apps/console/server/src/pillarmesh_console/operation_handles.py`
- Modify: `apps/console/server/pyproject.toml`
- Test: `apps/console/server/tests/test_governed_backend.py`
- Test: `apps/console/server/tests/test_operation_handles.py`
- Test: `tests/integration/test_console_governed_reads.py`

**Interfaces:**
- Produces: `GovernedConsoleBackend` read projections and tenant-scoped opaque operation-handle repository.
- Consumes: public `WarehouseControlService`, `CatalogControlService`, semantic repositories/read adapters, `RequestManagementService`, `FulfillmentReadService`, acquisition state/evidence interfaces, and Task 2 projections.

- [ ] **Step 1: Write no-fallback and privacy tests red**

Test a real adapter over disposable SQLite repositories. A missing downstream implementation returns `not_delivered`; a repository failure returns typed degraded/unavailable state or safe error according to classification; neither path reads `FixtureConsoleBackend`. Assert provider resource handles, endpoints, operation IDs, cursors, source rows, proposal text in requester views, and canary values are absent from serialized projections.

- [ ] **Step 2: Implement narrow read adapters green**

Inject explicit protocols for each owning service. Resolve bindings and operations through their public `get`/load interfaces, semantic publication receipts through semantic-registry adapters, inbox and histories through request-management services, and acquisition through public receipts. Do not open another service's SQLite database inside console code.

- [ ] **Step 3: Implement opaque operation handles red-green**

Store `(tenant_id, console_handle, capability_kind, private_identity)` in a private injected repository. Public handle format is `op_` plus 32 lowercase hex characters and is random, not a hash of private identity. Wrong-tenant lookup is non-enumerating. Serialization exposes phase, state, safe summary, and public evidence reference only.

- [ ] **Step 4: Run focused and affected Python suites**

Run:

```bash
uv run pytest apps/console/server/tests tests/integration/test_console_governed_reads.py -q
uv run pytest services/warehouse-control/tests services/catalog-control/tests services/semantic-registry/tests services/request-management/tests services/state/tests services/evidence/tests -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Expected: all exit 0.

- [ ] **Step 5: Commit**

Commit: `feat(console): expose governed local projections`

### Task 10: Wire governed commands and prove terminal browser transactions

**Files:**
- Modify: `apps/console/server/src/pillarmesh_console/governed_backend.py`
- Modify: `apps/console/server/src/pillarmesh_console/governed_adapters.py`
- Test: `apps/console/server/tests/test_governed_commands.py`
- Create: `tests/end-to-end/test_console_governed_journey.py`
- Create: `docs/console/acceptance-run.md`
- Create: `docs/console/known-gaps.md`
- Modify: `apps/console/README.md`

**Interfaces:**
- Produces: real command delegation for implemented warehouse, process, semantic review, request intake/conversation/acceptance, inbox decisions, and safe retry operations; terminal evidence-backed local acceptance.
- Consumes: Tasks 3 and 9, owning service domain identities and transactions.

- [ ] **Step 1: Write command delegation tests red**

For each command, assert exact expected revision, digest, role, and trusted identity reach the owning service. Assert duplicate `Idempotency-Key` serializes only concurrent calls, while sequential replay still delegates and lets the service return canonical replay. Assert transient errors never become terminal denial or non-conformance. Assert private operation identities never leave the handle repository.

- [ ] **Step 2: Implement only currently supported commands green**

Wire `WarehouseControlService`, `ProcessPackageService`, `SemanticReviewService`, `RequestManagementService`, and `FulfillmentService` through narrow adapters. If an owning transaction or evidence receipt does not exist, return `not_delivered`; do not approximate it in console code. Superset render, transformation, destination effects, stakeholder answer delivery, access grant application/expiry/revocation, and analyst embedding stay explicit in `known-gaps.md`.

- [ ] **Step 3: Write the governed end-to-end test red**

Start a real Starlette `governed_local` app over disposable repositories, submit fresh requests through HTTP, clarify and accept one outcome, create and approve the supported exact proposal, and independently reload owning repositories to verify terminal state and receipts. Exercise one warehouse lifecycle command only to the terminal state the current local provider harness can prove. Assert every unsupported capability remains `not_delivered`.

- [ ] **Step 4: Implement composition and run integrated green checks**

Run:

```bash
uv run pytest apps/console/server/tests tests/end-to-end/test_console_governed_journey.py -q
cd apps/console && npm ci && npm run lint && npm run typecheck && npm run check:contracts && npm run test -- --run && npm run build && npm run test:e2e
cd ../.. && uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
```

Expected: every command exits 0. Skipped live tests remain reported gaps, not passing evidence.

- [ ] **Step 5: Privacy and diff review**

Scan generated schema, screenshots, logs, docs, and test artifacts for credential canaries, local paths, provider IDs, endpoints, source values, and private operation identities. Inspect `git diff --stat origin/main...HEAD` and the full diff for unrelated files and reversions.

- [ ] **Step 6: Commit**

Commit: `feat(console): integrate governed architect workflow`

## Completion notes

- The analyst Superset embedding surface remains a separate, explicit MVP obligation. This plan delivers only architect review previews and deep links.
- Production SSO, deployment topology, external exposure, live provider accounts, and complete data-plane execution remain outside this plan.
- A live capability claim requires a new transaction traced to terminal state with sanitized evidence. Fixture screenshots and offline repository tests do not establish a live claim.
