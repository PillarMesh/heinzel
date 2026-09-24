import {render, screen} from "@testing-library/react"
import {expect, test, vi} from "vitest"

import type {
  ConsoleEnvelopeDataProductsView,
  ConsoleEnvelopeDataProductView,
} from "../../api/generated"
import {DataProductsPage} from "./data-products-page"

test("lists the owning publication identity with navigable details", async () => {
  const envelope = {
    data: {
      products: [
        {
          data_product_id: "product-revenue",
          version: 3,
          publication_status: "published",
          name: "Current revenue by region",
          description: "Approved revenue grouped by region for finance reporting.",
          product_revision: 3,
          generation: 7,
          catalog_revision: 4,
          namespace: "analytics",
          relation_name: "revenue_by_region",
          column_count: 2,
          source_count: 1,
          freshness_observed_at: "2026-09-01T12:00:00Z",
        },
      ],
    },
    meta: {data_provenance: "governed_local", correlation_id: "correlation-products"},
  } as ConsoleEnvelopeDataProductsView
  const client = {
    getDataProducts: vi.fn().mockResolvedValue(envelope),
    getDataProduct: vi.fn(),
  }

  render(<DataProductsPage client={client} />)

  expect(await screen.findByRole("link", {name: "Current revenue by region"})).toHaveAttribute(
    "href",
    "/data-products/product-revenue",
  )
  expect(screen.getByText("Approved revenue grouped by region for finance reporting.")).toBeVisible()
  expect(screen.getByText("Product revision 3, generation 7")).toBeVisible()
  expect(screen.getByText("Ready in managed warehouse")).toBeVisible()
  expect(screen.getByText("2 columns from 1 governed source")).toBeVisible()
  expect(screen.getByText(/Catalog revision 4/)).toBeVisible()
  expect(screen.getByText("Permitted by workspace policy")).toBeVisible()
  expect(screen.queryByText("product-revenue")).not.toBeInTheDocument()
})

test("shows a permitted unpublished product without deriving a name from its ID", async () => {
  const client = {
    getDataProducts: vi.fn().mockResolvedValue({
      data: {
        products: [
          {data_product_id: "product-revenue", version: 3, publication_status: "pending"},
        ],
      },
      meta: {data_provenance: "governed_local", correlation_id: "correlation-products"},
    } as ConsoleEnvelopeDataProductsView),
    getDataProduct: vi.fn(),
  }

  render(<DataProductsPage client={client} />)

  expect(await screen.findByRole("link", {name: "Publication pending"})).toBeVisible()
  expect(
    screen.getByText("The owning catalog has not published this product definition yet."),
  ).toBeVisible()
  expect(screen.queryByText("Revenue")).not.toBeInTheDocument()
  expect(screen.queryByText("product-revenue")).not.toBeInTheDocument()
})

test("presents a published product as a detail view without exposing its internal ID", async () => {
  const client = {
    getDataProducts: vi.fn(),
    getDataProduct: vi.fn().mockResolvedValue({
      data: {
        data_product_id: "product-revenue",
        version: 3,
        publication_status: "published",
        name: "Current revenue by region",
        description: "Approved revenue grouped by region for finance reporting.",
        product_revision: 3,
        generation: 7,
        catalog_revision: 4,
        namespace: "analytics",
        relation_name: "revenue_by_region",
        column_count: 2,
        source_count: 1,
        freshness_observed_at: "2026-09-01T12:00:00Z",
      },
      meta: {data_provenance: "governed_local", correlation_id: "correlation-product"},
    } as ConsoleEnvelopeDataProductView),
  }

  render(<DataProductsPage client={client} dataProductId="product-revenue" />)

  expect(await screen.findByRole("heading", {name: "Current revenue by region"})).toBeVisible()
  expect(screen.getByRole("link", {name: "Back to data products"})).toHaveAttribute(
    "href",
    "/data-products",
  )
  expect(screen.getByRole("region", {name: "Product availability"})).toHaveTextContent(
    "Ready in managed warehouse",
  )
  expect(screen.getByText("2 columns")).toBeVisible()
  expect(screen.getByText("1 governed source")).toBeVisible()
  expect(screen.queryByRole("list", {name: "Governed data products"})).not.toBeInTheDocument()
  expect(screen.queryByText("product-revenue")).not.toBeInTheDocument()
})
