import {expect, test} from "@playwright/test"

import type {ConsoleEnvelopeAnswerResultPageView} from "../web/src/api/generated"
import * as generatedValidators from "../web/src/api/generated-validators.js"

function validateResult(value: unknown): boolean {
  const validator = generatedValidators["answer_result_response"]
  if (validator === undefined) throw new Error("Result response validator is missing")
  return validator(value)
}

// These browser checks isolate presentation with a schema-validated API response.
// Warehouse execution and request authorization require the separate live journey.
const result: ConsoleEnvelopeAnswerResultPageView = {
  meta: {data_provenance: "governed_local", correlation_id: "browser-result"},
  data: {
    request_id: "request-browser-result",
    title: "Revenue by region",
    answer_text: "North revenue is 1250.50.",
    status: "available",
    freshness: "current",
    as_of: "2026-09-12T12:00:00Z",
    row_count: 1,
    columns: [
      {name: "region", label: "Region", value_type: "string", allowed_operations: []},
      {name: "revenue", label: "Revenue", value_type: "decimal", allowed_operations: []},
    ],
    rows: [["North", "1250.50"]],
    next_cursor: null,
    technical_details: {
      execution_receipt_id: "receipt-browser",
      plan_digest: "a".repeat(64),
      result_digest: "b".repeat(64),
      result_schema_digest: "c".repeat(64),
    },
  },
}

test("result route presents typed rows and a downloadable CSV", async ({page}) => {
  expect(validateResult(result)).toBe(true)
  await page.route("**/api/v1/requests/request-browser-result/result?*", (route) =>
    route.fulfill({json: result}),
  )
  await page.route("**/api/v1/requests/request-browser-result/result.csv", (route) =>
    route.fulfill({
      contentType: "text/csv",
      headers: {"content-disposition": 'attachment; filename="revenue-by-region.csv"'},
      body: "region,revenue\r\nNorth,1250.50\r\n",
    }),
  )

  await page.goto("/requests/request-browser-result/result")

  await expect(page.getByRole("heading", {name: "Revenue by region"})).toBeVisible()
  await expect(page.getByRole("cell", {name: "1250.50"})).toBeVisible()
  await expect(page.getByText("receipt-browser", {exact: true})).not.toBeVisible()
  const downloaded = page.waitForEvent("download")
  await page.getByRole("link", {name: "Download CSV"}).click()
  expect((await downloaded).suggestedFilename()).toBe("revenue-by-region.csv")
})

test("result expiry during pagination removes earlier rows and download access", async ({page}) => {
  await page.route("**/api/v1/requests/request-browser-result/result*", (route) => {
    const expired = new URL(route.request().url()).searchParams.has("cursor")
    const response = {
      ...result,
      data: expired
        ? {...result.data, status: "expired", rows: [], columns: [], technical_details: null}
        : {...result.data, next_cursor: "page-cursor-browser-sequence-00000002"},
    }
    expect(validateResult(response)).toBe(true)
    return route.fulfill({json: response})
  })

  await page.goto("/requests/request-browser-result/result")
  await page.getByRole("button", {name: "Load more rows"}).click()

  await expect(page.getByRole("status").filter({hasText: "This result has expired."})).toBeVisible()
  await expect(page.getByRole("table")).toHaveCount(0)
  await expect(page.getByRole("link", {name: "Download CSV"})).toHaveCount(0)
})
