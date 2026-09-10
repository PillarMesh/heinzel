import {render, screen} from "@testing-library/react"
import {expect, test, vi} from "vitest"

import type {ConsoleEnvelopeDataProductsView} from "../../api/generated"
import {DataProductsPage} from "./data-products-page"

test("lists governed data product references with navigable details", async () => {
  const envelope = {
    data: {
      products: [
        {data_product_id: "product-revenue", artifact_digest: "a".repeat(64), version: 3},
      ],
    },
    meta: {data_provenance: "governed_local", correlation_id: "correlation-products"},
  } as ConsoleEnvelopeDataProductsView
  const client = {
    getDataProducts: vi.fn().mockResolvedValue(envelope),
    getDataProduct: vi.fn(),
  }

  render(<DataProductsPage client={client} />)

  expect(await screen.findByRole("link", {name: "Revenue"})).toHaveAttribute(
    "href",
    "/data-products/product-revenue",
  )
  expect(screen.getByText("Version 3")).toBeVisible()
  // The listing reads which references policy permits; it verifies nothing, so it must not say so.
  expect(screen.getByText("Permitted by workspace policy")).toBeVisible()
  expect(screen.queryByText(/verified/i)).not.toBeInTheDocument()
  expect(screen.queryByText("a".repeat(64))).not.toBeInTheDocument()
})
