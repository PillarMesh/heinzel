import {expect, test, type Page} from "@playwright/test"

import {expectFixtureBanner, openRequest, resetDemoFixture} from "./demo-fixture"

/**
 * Generates the deterministic demonstration package described in section 12 of the
 * design. Running this file rewrites `docs/screenshots/`.
 *
 * Two of the eight named images cannot be produced: see the `fixme` entries at the
 * end and `docs/screenshots/README.md`.
 */

// See `architect-journey.spec.ts`: the served bundle does not execute under the
// console's own Content-Security-Policy, so an unbypassed capture is a blank page.

const SCREENSHOT_DIRECTORY = "docs/screenshots"

// Anything that would tie a published image to this machine, this operator, or a
// real customer. The images ship in the repository, so the check runs before every
// capture rather than as a review habit.
const FORBIDDEN_PATTERNS: readonly RegExp[] = [
  /\/Users\//,
  /\/home\/[a-z]/,
  /[A-Za-z]:\\\\/,
  /csrf_token/i,
  /Bearer\s/i,
  /password/i,
  /secret/i,
  /@[a-z0-9-]+\.(com|net|org|io)\b/i,
]

async function captureRoute(page: Page, name: string, fullPage = true): Promise<void> {
  await expectFixtureBanner(page)
  const visibleText = await page.locator("body").innerText()
  for (const pattern of FORBIDDEN_PATTERNS) {
    expect(visibleText, `${name} must not publish ${String(pattern)}`).not.toMatch(pattern)
  }
  // The bundled faces declare `font-display: swap`, so a capture taken before the
  // woff2 is applied would record the fallback stack's metrics and produce an image
  // that differs from every later run. Nothing else in the suite waits for this.
  await page.evaluate(() => document.fonts.ready)
  // Captured full page at the documented viewport width so the persistent fixture
  // banner and the decision controls appear in the same image.
  await page.evaluate(() => globalThis.scrollTo(0, 0))
  await page.screenshot({
    animations: "disabled",
    caret: "hide",
    fullPage,
    path: `${SCREENSHOT_DIRECTORY}/${name}.png`,
  })
}

test.beforeEach(async ({request}) => {
  await resetDemoFixture(request)
})

test("warehouse-foundation", async ({page}) => {
  await page.goto("/setup")
  await expect(page.getByRole("heading", {level: 1, name: "Choose the managed warehouse"})).toBeVisible()

  await captureRoute(page, "warehouse-foundation")
})

test("provisioning-progress", async ({page}) => {
  await page.goto("/setup")
  await page.getByRole("radio", {name: /PostgreSQL/}).check()
  await page
    .getByRole("checkbox", {
      name: "I understand that this warehouse binding is immutable after confirmation.",
    })
    .check()
  await page.getByRole("button", {name: "Confirm warehouse binding"}).click()
  await expect(page.getByRole("status", {name: "Warehouse operation"})).toContainText(
    "Request accepted",
  )

  await captureRoute(page, "provisioning-progress")
})

test("command-center", async ({page}) => {
  await page.goto("/inbox")
  await expect(page.getByRole("listbox", {name: "Prioritized requests"})).toBeVisible()

  await captureRoute(page, "command-center")
})

test("stakeholder-answer", async ({page}) => {
  await openRequest(page, "request-answer")
  const detail = page.getByRole("region", {name: "Request detail"})
  await expect(page.getByRole("region", {name: "Stakeholder answer proposal"})).toBeVisible()

  // The decision itself is the subject of this image, so it shows the confirmed
  // digest and the admissible actions rather than the queue landing view.
  const digest = detail.getByRole("checkbox", {name: /I confirm the exact reviewed digest/})
  await digest.check()
  await digest.scrollIntoViewIfNeeded()
  await expect(detail.getByRole("button", {name: "Approve", exact: true})).toBeEnabled()

  await captureRoute(page, "stakeholder-answer")
})

test("access-preview", async ({page}) => {
  await openRequest(page, "request-access")
  await expect(page.getByRole("region", {name: "Effective access preview"})).toBeVisible()

  await captureRoute(page, "access-preview")
})

test("no-valid-plan", async ({page}) => {
  await openRequest(page, "request-no-valid-plan")
  await expect(page.getByRole("region", {name: "Decision evidence"})).toContainText("No Valid Plan")

  await captureRoute(page, "no-valid-plan")
})

test("evidence-drawer-medium", async ({page}) => {
  // The medium viewport is where the evidence drawer stops being a column and
  // becomes a dialog, which is the behavior section 12 asks to show.
  await page.setViewportSize({width: 1024, height: 768})
  await openRequest(page, "request-access")
  await page.getByRole("button", {name: "Show evidence"}).click()
  await expect(page.getByRole("dialog", {name: "Decision evidence"})).toBeVisible()

  // Framed to the medium viewport itself, because the point of this image is what
  // that viewport shows at once, not the whole scrolled document.
  await captureRoute(page, "evidence-drawer-medium", false)
})

test.fixme("meaning-review", async () => {
  // GAP. The meaning approval gate has no reachable browser state. `SetupWorkbench`
  // renders by `SetupView.active_stage`, `FixtureConsoleBackend` never advances it
  // past `foundation`, and `/reviews/review-meaning` renders the same foundation
  // stage for the same reason. No honest image of this screen can be produced from
  // the committed fixtures.
})

test.fixme("activation-review", async () => {
  // GAP. As above, and additionally `get_review` admits a review only to a role in
  // its `required_authorities`. `review-data-product` requires `data_owner` and
  // `review-activation` requires `budget_approver`, while the served trusted context
  // holds `data_architect` alone, so both answer 404.
})
