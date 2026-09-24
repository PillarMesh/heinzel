import {expect, test} from "@playwright/test"

const ARCHITECT_ORIGIN = "http://127.0.0.1:8130"
const FIXED_ANSWER = "Net revenue is gross revenue less approved refunds."

function requestIdFromHref(href: string | null): string {
  const match = href?.match(/^\/requests\/(req-[a-z0-9_-]+)$/)
  if (match === null || match === undefined) {
    throw new Error(`could not read a request identity from route: ${String(href)}`)
  }
  return match[1]!
}

test("unsupported MRR question stops at No Valid Plan and can seed a new request", async ({
  page,
}) => {
  await page.goto("/requests")
  await page.getByRole("radio", {name: "Stakeholder question"}).check()
  await page.getByRole("textbox", {name: "Request title"}).fill("Test")
  await page.getByRole("textbox", {name: "Purpose"}).fill("This is a test request")
  await page.getByRole("textbox", {name: "Question"}).fill("What is the current MRR")
  await page.getByRole("button", {name: "Submit request"}).click()
  const firstStatus = page.getByRole("status").filter({
    has: page.getByRole("link", {name: "View request"}),
  })
  await expect(firstStatus).toHaveText("Request submitted. View request.")
  await expect(firstStatus).not.toContainText("req-")
  await expect(firstStatus).not.toContainText("revision")
  const firstRequestId = requestIdFromHref(
    await firstStatus.getByRole("link", {name: "View request"}).getAttribute("href"),
  )

  await page.goto(`${ARCHITECT_ORIGIN}/inbox`)
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
  await expect(detail).toContainText("published_semantic_term_not_found")
  await expect(detail).toContainText("No Valid Plan: published_semantic_term_not_found.")
  await expect(detail).toContainText(
    "Required change: Ask about one term in the workspace's current approved semantic publication.",
  )
  await expect(detail).not.toContainText(FIXED_ANSWER)

  await page.goto("http://127.0.0.1:8131/requests")
  await page.getByRole("link", {name: "Open request"}).first().click()
  await expect(page.getByText("What is the current MRR")).toBeVisible()
  await expect(
    page.getByText(
      "The current governed catalog does not contain one unambiguous term for this question.",
    ),
  ).toBeVisible()
  await expect(page.getByText(/published_semantic_term_not_found/)).toHaveCount(0)
  await expect(page.getByText(/Ask about one term/)).toHaveCount(0)
  await expect(page.getByText(FIXED_ANSWER)).toHaveCount(0)

  await page.getByRole("button", {name: "Start revised request"}).click()
  const revisedQuestion = page.getByRole("textbox", {name: "Question"})
  await expect(revisedQuestion).toHaveValue("What is the current MRR")
  await revisedQuestion.fill("What was MRR for the last closed month?")
  await page.getByRole("button", {name: "Submit request"}).click()

  const secondStatus = page.getByRole("status").filter({
    has: page.getByRole("link", {name: "View request"}),
  })
  await expect(secondStatus).toHaveText("Revised request submitted. View request.")
  await expect(secondStatus).not.toContainText("req-")
  await expect(secondStatus).not.toContainText("revision")
  const secondRequestId = requestIdFromHref(
    await secondStatus.getByRole("link", {name: "View request"}).getAttribute("href"),
  )
  expect(secondRequestId).not.toBe(firstRequestId)
})
