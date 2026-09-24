import {expect, test} from "@playwright/test"

import {
  MAJOR_ROUTES,
  decideRequestOutOfBand,
  expectFixtureBanner,
  openRequest,
  readSetup,
  resetDemoFixture,
} from "./demo-fixture"

/**
 * The architect half of the browser acceptance journey: enter a fresh workspace,
 * select and confirm the warehouse binding, reach the returning-user inbox,
 * review and approve a governed stakeholder-answer proposal, see a proposal whose
 * clarified outcome is unaccepted blocked on the requester, review an access
 * preview, reload a stale proposal, inspect a `No Valid Plan` outcome, and reset
 * the demo.
 *
 * One boundary of the shipped fixture shapes what can be asserted here:
 * `FixtureConsoleBackend` never advances `SetupView.active_stage`, and the
 * trusted context the server issues holds `data_architect` alone. Observing
 * managed-service and source validation, uploading the fixture package, resolving
 * a semantic question, approving meaning, data product and activation, and the
 * requester's own question, clarification reply and acceptance therefore have no
 * reachable browser state and are recorded as `fixme` rather than asserted
 * against a weaker claim.
 */

test.describe("production content security policy", () => {
  test("the compiled bundle boots under the policy the console itself sends", async ({page}) => {
    // The bundle used to compile its response validators with Ajv at runtime,
    // which builds them with `new Function`; the policy sends `default-src 'self'`
    // with no `'unsafe-eval'`, so the module threw before React mounted and the
    // served page stayed an empty `<div id="root">`. The validators are
    // precompiled now. The dev server sends no policy, and jsdom enforces none,
    // so this is the only place that can catch a regression.
    const failures: string[] = []
    page.on("pageerror", (error) => failures.push(error.message))

    await page.goto("/")

    await expect(page).toHaveURL(/\/setup$/)
    expect(failures, "the bundle must not violate its own policy").toEqual([])
  })
})

test.describe("architect journey", () => {
  test.beforeEach(async ({request}) => {
    await resetDemoFixture(request)
  })

  test("step 1: a fresh workspace opens the foundation stage behind the fixture banner", async ({
    page,
  }) => {
    await page.goto("/")

    await expect(page).toHaveURL(/\/setup$/)
    await expectFixtureBanner(page)
    await expect(
      page.getByRole("heading", {level: 1, name: "Choose the managed warehouse"}),
    ).toBeVisible()
    await expect(page.getByRole("navigation", {name: "Setup progress"})).toBeVisible()
    await expect(page.getByRole("button", {name: "Confirm warehouse binding"})).toBeDisabled()
  })

  test("step 2: the PostgreSQL binding is confirmable only after the immutable effect is acknowledged, and it then becomes immutable", async ({
    page,
    request,
  }) => {
    await page.goto("/setup")

    const postgres = page.getByRole("radio", {name: /PostgreSQL/})
    const clickhouse = page.getByRole("radio", {name: /ClickHouse/})
    await postgres.check()

    const confirm = page.getByRole("button", {name: "Confirm warehouse binding"})
    await expect(confirm).toBeDisabled()
    await page
      .getByRole("checkbox", {
        name: "I understand that this warehouse binding is immutable after confirmation.",
      })
      .check()
    await expect(confirm).toBeEnabled()
    await confirm.click()

    // The stage does not claim the binding from its own submission. It shows the
    // accepted operation and only reports immutability once a server-issued setup
    // projection carries the binding.
    await expect(page.getByRole("status", {name: "Warehouse operation"})).toBeVisible()
    await expect(page.getByText(/warehouse binding is immutable\./)).toHaveCount(0)

    await page.reload()
    await expect(page.getByText("The PostgreSQL warehouse binding is immutable.")).toBeVisible()
    await expect(postgres).toBeDisabled()
    await expect(clickhouse).toBeDisabled()
    await expect(page.getByRole("button", {name: "Confirm warehouse binding"})).toHaveCount(0)

    const setup = await readSetup(request)
    expect(setup.warehouse_binding?.engine).toBe("postgresql")
    expect(setup.warehouse_binding?.immutable).toBe(true)
  })

  test("step 2 honesty: an accepted provisioning operation is never displayed as a completed effect", async ({
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

    const operation = page.getByRole("status", {name: "Warehouse operation"})
    await expect(operation).toBeVisible()
    await expect(operation).toContainText("Request accepted")
    await expect(operation).not.toContainText("Provisioning succeeded")
    await expect(page.getByText("Provisioning succeeded")).toHaveCount(0)

    // The governance spine must keep saying that no managed effect exists.
    const spine = page.getByRole("complementary", {name: "Governance spine"})
    await expect(spine).toContainText("Downstream activation")
    await expect(spine).toContainText("Not delivered")
  })

  test("the same immutable-binding path admits ClickHouse", async ({page, request}) => {
    await page.goto("/setup")

    await page.getByRole("radio", {name: /ClickHouse/}).check()
    await page
      .getByRole("checkbox", {
        name: "I understand that this warehouse binding is immutable after confirmation.",
      })
      .check()
    await page.getByRole("button", {name: "Confirm warehouse binding"}).click()

    // Capability state, not destination equivalence: the fixture cannot prove that
    // a ClickHouse warehouse was provisioned, and the console must not imply it.
    await expect(page.getByRole("status", {name: "Warehouse operation"})).toContainText(
      "Request accepted",
    )
    await expect(page.getByText("Provisioning succeeded")).toHaveCount(0)

    const setup = await readSetup(request)
    expect(setup.warehouse_binding?.engine).toBe("clickhouse")

    await page.reload()
    await expect(page.getByText("The ClickHouse warehouse binding is immutable.")).toBeVisible()
    await expect(page.getByRole("radio", {name: /PostgreSQL/})).toBeDisabled()
  })

  test.fixme(
    "steps 3 to 6: managed services, sources, the process package, and the three approval gates",
    async () => {
      // GAP. `FixtureConsoleBackend` records the warehouse binding and the process
      // package but never changes `SetupView.active_stage`, which stays `foundation`,
      // and `SetupWorkbench` renders strictly by `active_stage`. `/reviews/:reviewId`
      // therefore also renders the foundation stage. In addition `get_review` admits a
      // review only to a role listed in its `required_authorities`, and the trusted
      // context holds `data_architect` alone, so `review-data-product` (data_owner)
      // and `review-activation` (budget_approver) answer 404. These screens have no
      // reachable browser state in fixture mode.
    },
  )

  test("step 7: the returning-user command center presents the prioritized decision queue", async ({
    page,
  }) => {
    await page.goto("/inbox")
    await expectFixtureBanner(page)

    const queue = page.getByRole("region", {name: "Decision queue"})
    await expect(queue).toBeVisible()
    await expect(
      page.getByRole("listbox", {name: "Prioritized requests"}).getByRole("option"),
    ).toHaveCount(5)
    await expect(queue).toContainText("Stakeholder question")
    await expect(queue).toContainText("Data access")

    // The server's own selection opens first; the browser does not pick for it.
    await expect(page.getByRole("region", {name: "Request detail"})).toContainText("Answer")
  })

  test("step 9: a governed stakeholder-answer proposal is approved only against its exact digest", async ({
    page,
    request,
  }) => {
    await openRequest(page, "request-answer")

    const detail = page.getByRole("region", {name: "Request detail"})
    await expect(detail).toContainText("Proposed stakeholder answer")
    await expect(detail).toContainText("net-revenue-v1")
    await expect(detail.getByRole("list", {name: "Required authorities"})).toContainText(
      "Not recorded",
    )

    const approve = detail.getByRole("button", {name: "Approve", exact: true})
    await expect(approve).toBeDisabled()
    await detail.getByRole("checkbox", {name: /I confirm the exact reviewed digest/}).check()
    await expect(approve).toBeEnabled()

    // No terminal state is displayed before the server returns one.
    await expect(detail).toContainText("proposed")
    await approve.click()

    // Approval is not delivery. The request stays awaiting approval and the console
    // offers admission, which is the owning transaction that admits the proposal to
    // execution; the demonstration used to jump straight to `execution_ready`, which
    // claimed unimplemented answer-delivery behaviour as live, which the console
    // must never do.
    await expect(detail).toContainText("awaiting approval")
    await expect(detail).toContainText("No decision is admissible from this projection.")
    const midway = await (await request.get("/api/v1/inbox/request-answer")).json()
    expect(midway.data.state).toBe("awaiting_approval")
    expect(midway.data.admission).toEqual({available: true, blocking_reason: null})

    await detail.getByRole("button", {name: "Admit to execution"}).click()

    await expect(detail).toContainText("execution ready")
    const committed = await (await request.get("/api/v1/inbox/request-answer")).json()
    expect(committed.data.state).toBe("execution_ready")
    expect(committed.data.admission).toBeNull()
  })

  test("step 10: a proposal blocked on requester acceptance offers the architect no admitting action", async ({
    page,
  }) => {
    await page.goto("/inbox")
    await expect(page.getByRole("option", {name: /Blocked Acceptance/})).toContainText(
      "Blocked on requester",
    )

    await openRequest(page, "request-blocked-acceptance")
    const detail = page.getByRole("region", {name: "Request detail"})

    await expect(detail).toContainText("clarifying")
    await expect(detail).toContainText("No decision is admissible from this projection.")
    await expect(detail.getByRole("button", {name: "Approve", exact: true})).toHaveCount(0)
    await expect(detail.getByRole("button", {name: "Reject"})).toHaveCount(0)
    await expect(detail.getByRole("button", {name: "Request changes"})).toHaveCount(0)
    await expect(
      detail.getByRole("checkbox", {name: /I confirm the exact reviewed digest/}),
    ).toHaveCount(0)
  })

  test("step 11: a least-privilege access preview separates the requested fields from the effective scope", async ({
    page,
  }) => {
    await openRequest(page, "request-access")

    const preview = page.getByRole("region", {name: "Effective access preview"})
    await expect(preview).toBeVisible()
    await expect(preview.getByRole("list", {name: "Requested fields"})).toContainText(
      "recognized_at",
    )
    await expect(preview.getByRole("list", {name: "Effective scope"})).toContainText("invoice_id")
    await expect(preview.getByRole("list", {name: "Effective scope"})).not.toContainText(
      "recognized_at",
    )
    await expect(preview.getByRole("list", {name: "Exclusions"})).toContainText("recognized_at")
    await expect(preview.getByRole("list", {name: "Denied checks"})).toContainText(
      "Cannot query excluded recognition timestamps.",
    )
    await expect(preview.getByRole("list", {name: "Required authorities"})).toContainText(
      "Not recorded",
    )
  })

  test("step 12: a stale proposal is reloaded safely and the unsubmitted comment survives", async ({
    page,
    request,
  }) => {
    await openRequest(page, "request-stale")

    const detail = page.getByRole("region", {name: "Request detail"})
    await detail.getByRole("textbox", {name: "Review comment"}).fill("Scope looks narrow enough.")
    await detail.getByRole("checkbox", {name: /I confirm the exact reviewed digest/}).check()

    // Somebody else decides the same revision first.
    await decideRequestOutOfBand(request, "request-stale", "request_changes")

    await detail.getByRole("button", {name: "Approve", exact: true}).click()

    await expect(detail).toContainText("Review refreshed at revision 4; your comment was preserved.")
    await expect(detail.getByRole("textbox", {name: "Review comment"})).toHaveValue(
      "Scope looks narrow enough.",
    )
    // The refreshed projection is authoritative and nothing was recorded twice.
    await expect(detail).toContainText("clarifying")
    await expect(
      detail.getByRole("checkbox", {name: /I confirm the exact reviewed digest/}),
    ).toHaveCount(0)
  })

  test("step 13: a No Valid Plan outcome is shown without an admissible action", async ({page}) => {
    await openRequest(page, "request-no-valid-plan")

    const detail = page.getByRole("region", {name: "Request detail"})
    await expect(detail).toContainText("investigating")
    await expect(detail).toContainText("No proposal has been issued for this request.")
    await expect(detail).toContainText("No decision is admissible from this projection.")

    const evidence = page.getByRole("region", {name: "Decision evidence"})
    await expect(evidence).toContainText("No Valid Plan")
    await expect(evidence).toContainText("No immutable evidence reference exists yet.")
  })

  test("step 14: the demo reset restores the seeded fixture and the browser shows the pristine workspace", async ({
    page,
    request,
  }) => {
    await page.goto("/setup")
    await page.getByRole("radio", {name: /ClickHouse/}).check()
    await page
      .getByRole("checkbox", {
        name: "I understand that this warehouse binding is immutable after confirmation.",
      })
      .check()
    await page.getByRole("button", {name: "Confirm warehouse binding"}).click()
    await expect(page.getByRole("status", {name: "Warehouse operation"})).toBeVisible()
    await page.reload()
    await expect(page.getByText("The ClickHouse warehouse binding is immutable.")).toBeVisible()

    const restored = await resetDemoFixture(request)
    expect(restored.warehouse_binding).toBeNull()
    expect(restored.active_stage).toBe("foundation")

    await page.goto("/setup")
    await expectFixtureBanner(page)
    await expect(page.getByRole("button", {name: "Confirm warehouse binding"})).toBeVisible()
    await expect(page.getByRole("radio", {name: /PostgreSQL/})).toBeEnabled()
    await expect(page.getByText(/warehouse binding is immutable\./)).toHaveCount(0)
  })

  test("every reachable route carries the fixture banner", async ({page}) => {
    for (const route of MAJOR_ROUTES) {
      await page.goto(route)
      await expectFixtureBanner(page)
      await expect(page.getByRole("main", {name: "Heinzel console"})).toBeVisible()
    }
  })
})
