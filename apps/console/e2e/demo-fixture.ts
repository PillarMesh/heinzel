import {expect, type APIRequestContext, type Page} from "@playwright/test"

// The console admits a command only from its own configured origin and only with a
// session-bound CSRF token, so every out-of-band call the journeys make has to be
// built exactly the way the browser builds one.
export const ORIGIN = "http://127.0.0.1:8000"

// Rendered by `ModeBanner` for `demo_fixture` provenance. Every route must carry it.
export const FIXTURE_BANNER = "Demo scenario - no managed effects"

export const ARCHITECT_ROLE = "data_architect"

/**
 * Routes the shell can actually reach with the trusted architect context the
 * server issues. `/reviews/:reviewId` is deliberately absent: the fixture never
 * leaves the foundation stage, so that route renders the foundation stage.
 */
export const MAJOR_ROUTES: readonly string[] = [
  "/setup",
  "/inbox",
  "/inbox/request-answer",
  "/inbox/request-access",
  "/inbox/request-blocked-acceptance",
  "/inbox/request-stale",
  "/inbox/request-no-valid-plan",
  "/requests",
  "/data-products",
  "/runs",
  "/acquisition-receipts",
  "/catalog",
  "/dashboards",
  "/evidence",
  "/recovery",
]

interface SessionData {
  readonly csrf_token: string
  readonly active_role: string
  readonly roles: readonly string[]
}

interface SetupData {
  readonly revision: number
  readonly setup_digest: string
  readonly reset_token: string
  readonly active_stage: string
  readonly warehouse_binding: {readonly engine: string; readonly immutable: boolean} | null
}

interface RequestDetailData {
  readonly request_id: string
  readonly revision: number
  readonly state: string
  readonly proposal_digest: string | null
}

let idempotencyCounter = 0

function nextIdempotencyKey(prefix: string): string {
  idempotencyCounter += 1
  return `${prefix}-${String(idempotencyCounter).padStart(6, "0")}`
}

async function readEnvelope<Data>(request: APIRequestContext, path: string): Promise<Data> {
  const response = await request.get(path)
  expect(response.ok(), `${path} must be readable`).toBe(true)
  const body = (await response.json()) as {readonly data: Data}
  return body.data
}

export async function readSession(request: APIRequestContext): Promise<SessionData> {
  return readEnvelope<SessionData>(request, "/api/v1/session")
}

export async function readSetup(request: APIRequestContext): Promise<SetupData> {
  return readEnvelope<SetupData>(request, "/api/v1/setup")
}

export async function readRequestDetail(
  request: APIRequestContext,
  requestId: string,
): Promise<RequestDetailData> {
  return readEnvelope<RequestDetailData>(request, `/api/v1/inbox/${requestId}`)
}

async function postCommand(
  request: APIRequestContext,
  path: string,
  payload: Record<string, unknown>,
  keyPrefix: string,
) {
  const session = await readSession(request)
  return request.post(path, {
    data: payload,
    headers: {
      "content-type": "application/json",
      "idempotency-key": nextIdempotencyKey(keyPrefix),
      origin: ORIGIN,
      "x-csrf-token": session.csrf_token,
    },
  })
}

/**
 * Restores the seeded fixture so each test starts from the same authoritative
 * state. This is the fixture-mode demo reset that ends the acceptance journey; it
 * is invoked over the same versioned command the browser would use, because the
 * shell ships no control for it.
 */
export async function resetDemoFixture(request: APIRequestContext): Promise<SetupData> {
  const setup = await readSetup(request)
  const response = await postCommand(
    request,
    "/api/v1/demo/reset",
    {
      active_role: ARCHITECT_ROLE,
      expected_revision: setup.revision,
      reset_token: setup.reset_token,
      setup_digest: setup.setup_digest,
    },
    "reset-key",
  )
  expect(response.status(), "demo reset must be accepted").toBe(200)
  const body = (await response.json()) as {readonly data: SetupData}
  return body.data
}

/**
 * Records a decision without the browser, so a page holding an older projection
 * meets a genuine server conflict rather than a simulated one.
 */
export async function decideRequestOutOfBand(
  request: APIRequestContext,
  requestId: string,
  decision: "approve" | "reject" | "request_changes",
): Promise<number> {
  const detail = await readRequestDetail(request, requestId)
  expect(detail.proposal_digest, "the request must carry a proposal digest").not.toBeNull()
  const response = await postCommand(
    request,
    `/api/v1/inbox/${requestId}/decisions`,
    {
      active_role: ARCHITECT_ROLE,
      decision,
      expected_revision: detail.revision,
      reviewed_digest: detail.proposal_digest,
    },
    "out-of-band-key",
  )
  expect(response.status(), "the out-of-band decision must be recorded").toBe(200)
  return detail.revision
}

/** Every route must label itself as a demonstration before anything else is asserted. */
export async function expectFixtureBanner(page: Page): Promise<void> {
  await expect(page.getByRole("note").filter({hasText: FIXTURE_BANNER})).toBeVisible()
}

export async function openRequest(page: Page, requestId: string): Promise<void> {
  await page.goto(`/inbox/${requestId}`)
  await expectFixtureBanner(page)
  await expect(page.getByRole("region", {name: "Request detail"})).toBeVisible()
}
