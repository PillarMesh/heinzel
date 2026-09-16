import {render, screen} from "@testing-library/react"
import {expect, test, vi} from "vitest"

import type {CatalogAssetView, ConsoleEnvelopeCatalogAssetsView} from "../../api/generated"
import {CatalogPage} from "./catalog-page"

const asset: CatalogAssetView = {
  asset_ref: "asset-000000000000000000000001",
  display_name: "Net revenue",
  definition: "Gross revenue less approved refunds.",
  owner: "finance-owner",
  classifications: ["financial"],
  lineage_summary: "1 governed lineage relationship published.",
}

test("lists published catalog assets with navigable details", async () => {
  const envelope = {
    data: {assets: [asset]},
    meta: {data_provenance: "governed_local", correlation_id: "correlation-catalog"},
  } as ConsoleEnvelopeCatalogAssetsView
  const client = {
    getCatalogAssets: vi.fn().mockResolvedValue(envelope),
    getCatalogAsset: vi.fn(),
  }

  render(<CatalogPage client={client} />)

  expect(await screen.findByRole("link", {name: "Net revenue"})).toHaveAttribute(
    "href",
    "/catalog/asset-000000000000000000000001",
  )
  expect(screen.getByText("Gross revenue less approved refunds.")).toBeVisible()
})

test("does not invent an owner label when catalog authority has none", async () => {
  const envelope = {
    data: {assets: [{...asset, owner: null}]},
    meta: {data_provenance: "governed_local", correlation_id: "correlation-catalog"},
  } as ConsoleEnvelopeCatalogAssetsView
  const client = {
    getCatalogAssets: vi.fn().mockResolvedValue(envelope),
    getCatalogAsset: vi.fn(),
  }

  render(<CatalogPage client={client} />)

  expect(await screen.findByRole("link", {name: "Net revenue"})).toBeVisible()
  expect(screen.queryByText(/Owner:/)).not.toBeInTheDocument()
})
