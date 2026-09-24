import {expect, test} from "@playwright/test"

import {expectFixtureBanner, readSession, resetDemoFixture} from "./demo-fixture"

/**
 * The requester half of the browser acceptance journey: submit a stakeholder
 * question as the requester, answer one clarification, and accept the clarified
 * outcome, so that the architect later sees that acceptance as a satisfied
 * required authority.
 *
 * GAP. There is no way to act as a requester in this build. `create_app` installs
 * `_default_context`, a fixed `TrustedActorContext` whose roles are
 * `("data_architect",)`, and nothing in the browser or the server can change it:
 * the shell renders no role switch, and no request header, cookie, query parameter,
 * or environment variable is consulted. Every requester-owned command and
 * projection is scoped to the `requester` role, so intake, clarification reply, and
 * clarified-outcome acceptance are unreachable from the browser.
 *
 * What is asserted here is the denial itself, which is a real and load-bearing
 * boundary, plus the resulting shape of the architect's view. The unreachable steps
 * are recorded as `fixme`.
 */


test.beforeEach(async ({request}) => {
  await resetDemoFixture(request)
})

test("the console issues one architect identity and no requester role", async ({request}) => {
  const session = await readSession(request)

  expect(session.active_role).toBe("data_architect")
  expect(session.roles).toEqual(["data_architect"])
  expect(session.roles).not.toContain("requester")
})

test("the requester surface refuses to render for a reviewer identity", async ({page, request}) => {
  await page.goto("/requests")
  await expectFixtureBanner(page)

  // A reviewer must not be handed requester-owned projections, and the browser
  // must not render a partial requester surface when the read is denied.
  await expect(page.getByRole("alert")).toHaveText("Your requests could not be displayed safely.")
  await expect(page.getByRole("heading", {name: "My requests"})).toHaveCount(0)
  await expect(page.getByRole("heading", {name: "Ask Heinzel for something"})).toHaveCount(0)

  expect((await request.get("/api/v1/requests/mine")).status()).toBe(404)
})

test("a requester-owned request is not enumerable through the requester route", async ({page}) => {
  await page.goto("/requests/request-blocked-acceptance")
  await expectFixtureBanner(page)

  // The denial is identical to the denial for a request that never existed, so
  // nothing on this route can confirm that another requester's request exists.
  await expect(page.getByRole("alert")).toHaveText("Your requests could not be displayed safely.")
})

test.fixme(
  "step 8: submit a stakeholder question, answer one clarification, and accept the clarified outcome",
  async () => {
    // GAP, see the file comment. `create_request`, `append_conversation_message`
    // and `accept_clarified_outcome` all call `_authorize(context, ("requester",))`
    // and `_require_command_role`, and the served context is `data_architect` only.
  },
)

test.fixme(
  "step 9: the requester acceptance appears on the architect's proposal as a satisfied required authority",
  async () => {
    // GAP, two causes.
    //
    // First, step 8 cannot run, so no acceptance can be recorded from the browser.
    //
    // Second, the fixture cannot express the assertion even if it could. The
    // stakeholder-answer proposal in `fixture_data._request_details` carries a
    // single required authority for `data_owner`. No requester authority appears in
    // `required_authorities` at all, satisfied or not, so
    // `outstandingRequesterAuthority` in `decision-workspace.tsx` never matches and
    // the "Blocked on the requester" panel in the request detail is dead code in
    // fixture mode. The queue's own blocked label still renders and is asserted in
    // `architect-journey.spec.ts`.
  },
)
