import AxeBuilder from "@axe-core/playwright"
import {expect, test, type Locator, type Page} from "@playwright/test"

import {MAJOR_ROUTES, expectFixtureBanner, openRequest, resetDemoFixture} from "./demo-fixture"

// See `architect-journey.spec.ts`: the served bundle does not execute under the
// console's own Content-Security-Policy. That defect is pinned there; these checks
// are about the rendered product.

const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]

test.beforeEach(async ({request}) => {
  await resetDemoFixture(request)
})

async function auditRoute(page: Page, route: string) {
  await page.goto(route)
  await expectFixtureBanner(page)
  const results = await new AxeBuilder({page}).withTags(WCAG_TAGS).analyze()
  expect(
    results.violations.map((violation) => `${violation.id}: ${violation.nodes.length} node(s)`),
    `${route} must have no WCAG A or AA violation`,
  ).toEqual([])
}

/**
 * Presses Tab until the target holds focus, so the assertion is that the element is
 * genuinely reachable in sequential order rather than that it can be focused
 * programmatically.
 */
async function tabTo(page: Page, target: Locator, limit = 40): Promise<void> {
  for (let step = 0; step < limit; step += 1) {
    if (await target.evaluate((element) => element === document.activeElement)) {
      return
    }
    await page.keyboard.press("Tab")
  }
  await expect(target, "the element must be reachable with Tab alone").toBeFocused()
}

for (const route of MAJOR_ROUTES) {
  test(`axe reports no WCAG A or AA violation on ${route}`, async ({page}) => {
    await auditRoute(page, route)
  })
}

test("every page says which page it is, in its heading and in its title", async ({page}) => {
  // Neither is an axe rule at WCAG A or AA: `document-title` only asks that a title exists,
  // and `page-has-heading-one` is a best-practice rule outside the tags above. Both were
  // failing -- every route was titled `Heinzel`, and the two inbox pages had no page heading,
  // which also left the shell announcing the literal words "Current work" on arrival.
  const titles = new Map<string, string>()
  for (const route of MAJOR_ROUTES) {
    await page.goto(route)
    await expectFixtureBanner(page)

    const headings = page.getByRole("main").getByRole("heading", {level: 1})
    await expect(headings, `${route} must have exactly one page heading`).toHaveCount(1)

    const heading = (await headings.first().textContent())?.trim() ?? ""
    await expect
      .poll(async () => page.title(), {message: `${route} must be titled after its heading`})
      .toBe(`${heading} · Heinzel`)
    titles.set(route, await page.title())
  }
  // Not one name for everything. `/inbox` and the first request in its queue are the same
  // page and share a title legitimately, so this asserts that the titles vary rather than
  // that they are all distinct -- and that none is the bare product name.
  expect(new Set(titles.values()).size).toBeGreaterThan(titles.size / 2)
  expect([...titles.values()]).not.toContain("Heinzel")
})

test("axe reports no violation on the foundation stage after a binding is submitted", async ({
  page,
}) => {
  await page.goto("/setup")
  await page.getByRole("radio", {name: /PostgreSQL/}).check()
  await page
    .getByRole("checkbox", {
      name: "I understand that this warehouse binding is immutable after confirmation.",
    })
    .check()
  await page.getByRole("button", {name: "Confirm warehouse binding"}).click()
  await expect(page.getByRole("status", {name: "Warehouse operation"})).toBeVisible()

  const results = await new AxeBuilder({page}).withTags(WCAG_TAGS).analyze()
  expect(results.violations.map((violation) => violation.id)).toEqual([])
})

test("axe reports no violation with the medium-width evidence drawer open", async ({page}) => {
  await page.setViewportSize({width: 1024, height: 768})
  await openRequest(page, "request-access")
  await page.getByRole("button", {name: "Show evidence"}).click()
  await expect(page.getByRole("dialog", {name: "Decision evidence"})).toBeVisible()

  const results = await new AxeBuilder({page}).withTags(WCAG_TAGS).analyze()
  expect(results.violations.map((violation) => violation.id)).toEqual([])
})

test("the skip link is the first stop of the keyboard order and moves focus to the current work", async ({
  page,
}) => {
  await page.goto("/setup")

  // The shell moves focus into `main` on every route change, so the order is walked
  // backwards from there: the skip link must be reachable, and nothing may precede it.
  const skipLink = page.getByRole("link", {name: "Skip to current work"})
  for (let step = 0; step < 20; step += 1) {
    if (await skipLink.evaluate((element) => element === document.activeElement)) {
      break
    }
    await page.keyboard.press("Shift+Tab")
  }
  await expect(skipLink).toBeFocused()

  // The skip link comes before the product navigation, so it is reached before any
  // of the chrome it exists to skip.
  await page.keyboard.press("Tab")
  await expect(page.getByRole("link", {name: "Workspace setup"})).toBeFocused()

  await page.keyboard.press("Shift+Tab")
  await expect(skipLink).toBeFocused()
  await page.keyboard.press("Enter")
  await expect(page.getByRole("main", {name: "Heinzel console"})).toBeFocused()
})

test("keyboard alone completes the setup foundation stage", async ({page, request}) => {
  await page.goto("/setup")

  const postgres = page.getByRole("radio", {name: /PostgreSQL/})
  await tabTo(page, postgres)

  // Arrow keys move within the radio group, as a native radio group requires.
  await page.keyboard.press("ArrowDown")
  await expect(page.getByRole("radio", {name: /ClickHouse/})).toBeChecked()
  await page.keyboard.press("ArrowUp")
  await expect(postgres).toBeChecked()

  const acknowledgement = page.getByRole("checkbox", {
    name: "I understand that this warehouse binding is immutable after confirmation.",
  })
  await tabTo(page, acknowledgement)
  await page.keyboard.press("Space")
  await expect(acknowledgement).toBeChecked()

  const confirm = page.getByRole("button", {name: "Confirm warehouse binding"})
  await tabTo(page, confirm)
  await page.keyboard.press("Enter")

  await expect(page.getByRole("status", {name: "Warehouse operation"})).toContainText(
    "Request accepted",
  )
  const setup = await (await request.get("/api/v1/setup")).json()
  expect(setup.data.warehouse_binding.engine).toBe("postgresql")
})

test("keyboard alone drives the decision queue and hands focus to the request detail", async ({
  page,
}) => {
  await page.goto("/inbox")

  const queue = page.getByRole("listbox", {name: "Prioritized requests"})
  const options = queue.getByRole("option")
  await tabTo(page, options.first())

  await page.keyboard.press("ArrowDown")
  await expect(options.nth(1)).toBeFocused()
  await expect(options.nth(1)).toHaveAttribute("aria-selected", "true")

  await page.keyboard.press("End")
  await expect(options.last()).toBeFocused()
  await page.keyboard.press("Home")
  await expect(options.first()).toBeFocused()

  await page.keyboard.press("ArrowDown")
  await page.keyboard.press("Enter")
  await expect(page.getByRole("region", {name: "Request detail"})).toBeFocused()
})

test("keyboard alone opens, reads, and dismisses the medium-width evidence drawer", async ({
  page,
}) => {
  await page.setViewportSize({width: 1024, height: 768})
  await openRequest(page, "request-answer")

  // The label toggles between "Show evidence" and "Hide evidence", so the trigger
  // has to be addressed by a locator that survives the toggle.
  const trigger = page.getByRole("button", {name: /^(Show|Hide) evidence$/})
  await tabTo(page, trigger)
  await expect(trigger).toHaveAttribute("aria-expanded", "false")
  await page.keyboard.press("Enter")

  const drawer = page.getByRole("dialog", {name: "Decision evidence"})
  await expect(drawer).toBeVisible()
  await expect(drawer).toBeFocused()
  await expect(trigger).toHaveAttribute("aria-expanded", "true")

  await page.keyboard.press("Escape")
  await expect(drawer).toHaveCount(0)
  // Focus returns to the control that opened the drawer, not to the document.
  await expect(trigger).toBeFocused()
  await expect(trigger).toHaveAttribute("aria-expanded", "false")
})

test("keyboard alone records a decision against the confirmed digest", async ({page, request}) => {
  await openRequest(page, "request-answer")

  const detail = page.getByRole("region", {name: "Request detail"})
  const digest = detail.getByRole("checkbox", {name: /I confirm the exact reviewed digest/})
  await tabTo(page, digest)
  await page.keyboard.press("Space")
  await expect(digest).toBeChecked()

  const approve = detail.getByRole("button", {name: "Approve", exact: true})
  await tabTo(page, approve)
  await page.keyboard.press("Enter")

  // Approving records one authority's approval; admission is the separate
  // transaction that carries the proposal into execution, and it is reachable by
  // keyboard alone too.
  await expect(detail).toContainText("awaiting approval")
  const admit = detail.getByRole("button", {name: "Admit to execution"})
  await tabTo(page, admit)
  await page.keyboard.press("Enter")

  await expect(detail).toContainText("execution ready")
  const committed = await (await request.get("/api/v1/inbox/request-answer")).json()
  expect(committed.data.state).toBe("execution_ready")
})

test("the clarification thread is reachable by keyboard while the requester is awaited", async ({
  page,
}) => {
  // This test used to assert that both intervention controls were permanently
  // disabled, with the panel blaming the server for a conversation digest the
  // fixture supplies. That was the wiring defect written down as a requirement:
  // the request's clarification conversation is where the architect observes,
  // intervenes in, or takes over a business-meaning question routed to the
  // requester. Being blocked on the requester removes the architect's
  // *decision*, not their voice.
  await openRequest(page, "request-blocked-acceptance")

  const conversation = page.getByRole("region", {name: "Clarification conversation"})
  await expect(conversation).toBeVisible()
  await expect(conversation).toContainText("Awaiting requester")
  await expect(conversation).not.toContainText("Intervention is unavailable")

  // The send control stays disabled until there is something to send, and the
  // reason is the empty message rather than a claim about the server.
  const compose = conversation.getByRole("textbox", {name: "Architect message"})
  const send = conversation.getByRole("button", {name: "Send architect message"})
  await expect(compose).toBeEnabled()
  await expect(send).toBeDisabled()

  await tabTo(page, compose)
  await compose.pressSequentially("Following up with the requester.")
  await expect(send).toBeEnabled()

  const comment = page
    .getByRole("region", {name: "Request detail"})
    .getByRole("textbox", {name: "Review comment"})
  await tabTo(page, comment)
  await comment.pressSequentially("Waiting on the requester.")
  await expect(comment).toHaveValue("Waiting on the requester.")
})

test.fixme("keyboard-only request intake and clarified-outcome acceptance", async () => {
  // GAP. Both live on the requester surface, which the served
  // `data_architect`-only context cannot open. See `requester-journey.spec.ts`.
})
