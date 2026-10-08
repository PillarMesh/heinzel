import {readFile} from "node:fs/promises"

import {expect, test} from "@playwright/test"

test("a fresh governed warehouse answer is presented and downloads safely", async ({page}) => {
  await page.goto("/requests")

  await expect(page.getByRole("heading", {name: "My requests"})).toBeVisible()
  await expect(page.getByRole("heading", {name: "Current revenue by region"})).toBeVisible()
  await page.getByRole("link", {name: "Open Current revenue by region"}).click()

  const requestId = new URL(page.url()).pathname.split("/").at(-1)
  expect(requestId).toMatch(/^req-[a-z0-9-]+$/)
  await expect(page.getByRole("heading", {name: "Current revenue by region"})).toBeVisible()
  await page.getByRole("link", {name: "View results"}).click()

  await expect(page.getByRole("heading", {name: "Current revenue by region"})).toBeVisible()
  await expect(page.getByText("2 verified result rows are available.")).toBeVisible()
  await expect(page.getByRole("row", {name: "east 99.00"})).toBeVisible()
  await expect(page.getByRole("row", {name: "west 30.00"})).toBeVisible()
  await expect(page.getByText("Technical details", {exact: true})).toHaveCount(0)
  await expect(page.getByText(requestId!, {exact: true})).toHaveCount(0)

  const downloadEvent = page.waitForEvent("download")
  await page.getByRole("link", {name: "Download CSV"}).click()
  const download = await downloadEvent
  expect(download.suggestedFilename()).toBe("current-revenue-by-region.csv")
  expect(await readFile(await download.path(), "utf8")).toBe(
    "region,total_revenue\r\neast,99.000000000\r\nwest,30.000000000\r\n",
  )

  await page.goto("http://127.0.0.1:8230/data-products")
  const warehouseCapability = page.getByRole("listitem").filter({hasText: "Managed warehouse"})
  const catalogCapability = page.getByRole("listitem").filter({hasText: "Managed catalog"})
  await expect(warehouseCapability.getByText("Ready", {exact: true})).toBeVisible()
  await expect(catalogCapability.getByText("Ready", {exact: true})).toBeVisible()
  await page.getByRole("link", {name: "Current revenue by region"}).click()

  await expect(page.getByRole("heading", {name: "Current revenue by region"})).toBeVisible()
  await expect(page.getByRole("region", {name: "Product availability"})).toContainText(
    "Ready in managed warehouse",
  )
  await expect(page.getByText("2 columns", {exact: true})).toBeVisible()
  await expect(page.getByText("1 governed source", {exact: true})).toBeVisible()
  await expect(page.locator("details", {hasText: "Technical location"})).not.toHaveAttribute("open")

  await page.goto("http://127.0.0.1:8230/dashboards")
  const dashboardCapability = page.getByRole("listitem").filter({hasText: "Analyst dashboards"})
  await expect(dashboardCapability.getByText("Ready", {exact: true})).toBeVisible()
  await expect(page.getByRole("heading", {name: "Current revenue by region"})).toBeVisible()
  await page.getByRole("link", {name: "Current revenue by region"}).click()

  await expect(page.getByRole("heading", {name: "Current revenue by region"})).toBeVisible()
  await expect(page.getByText(/Revision 1 published by the managed BI provider/)).toBeVisible()
  await expect(page.getByText("Data as of", {exact: true})).toBeVisible()
  await expect(page.getByText("Current", {exact: true})).toBeVisible()
  await expect(page.getByText("Access", {exact: true})).toBeVisible()
  await expect(page.getByText("Workspace role", {exact: true})).toBeVisible()
  await expect(page.getByText(/superset/i)).toHaveCount(0)
})
