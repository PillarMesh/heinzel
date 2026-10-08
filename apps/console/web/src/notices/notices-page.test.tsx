import {render, screen, within} from "@testing-library/react"
import {expect, test} from "vitest"

import {THIRD_PARTY_COMPONENTS} from "./generated-notices"
import {NoticesPage} from "./notices-page"

test("every component the product ships is named with its version and licence", () => {
  render(<NoticesPage />)

  // The page is the whole list or it is a misleading subset of it, so this asserts the count
  // rather than a sample: a component that stops being rendered stops being attributed.
  const rows = screen.getAllByRole("row").filter((row) => within(row).queryAllByRole("rowheader").length > 0)
  expect(rows).toHaveLength(THIRD_PARTY_COMPONENTS.length)

  const page = screen.getByRole("region", {name: "Third-party notices"})
  const superset = THIRD_PARTY_COMPONENTS.find((c) => c.name === "Apache Superset")
  expect(superset, "the dashboard engine must be attributed even though its chrome is not shown").toBeDefined()
  expect(page).toHaveTextContent("Apache Superset")
  expect(page).toHaveTextContent("Apache-2.0")
})

test("a licence the metadata did not declare is said to be undeclared, not guessed", () => {
  // The one thing on a notices page nobody can check is a licence the page invented.
  render(<NoticesPage />)

  const undeclared = THIRD_PARTY_COMPONENTS.filter((c) => c.license === "unknown")
  const page = screen.getByRole("region", {name: "Third-party notices"})
  for (const component of undeclared) {
    expect(page).toHaveTextContent(component.name)
  }
  expect(screen.queryAllByText("not declared in its metadata")).toHaveLength(undeclared.length)
})

test("the page says which manifests it was generated from", () => {
  render(<NoticesPage />)

  const page = screen.getByRole("region", {name: "Third-party notices"})
  expect(page).toHaveTextContent("uv.lock")
  expect(page).toHaveTextContent("apps/console/package-lock.json")
})
