import {PageFailure} from "../../components/unavailable"
import {asPageFailure, type PageFailureState} from "../../format/failure"
import {useEffect, useState} from "react"

import type {
  CatalogAssetView,
  ConsoleEnvelopeCatalogAssetsView,
  ConsoleEnvelopeCatalogAssetView,
} from "../../api/generated"

export interface CatalogClient {
  getCatalogAssets(): Promise<ConsoleEnvelopeCatalogAssetsView>
  getCatalogAsset(assetRef: string): Promise<ConsoleEnvelopeCatalogAssetView>
}

interface CatalogPageProps {
  readonly assetRef?: string
  readonly client: CatalogClient
}

export function CatalogPage({assetRef, client}: CatalogPageProps) {
  const [assets, setAssets] = useState<readonly CatalogAssetView[] | null>(null)
  const [failure, setFailure] = useState<PageFailureState | null>(null)

  useEffect(() => {
    let abandoned = false
    const request =
      assetRef === undefined
        ? client.getCatalogAssets().then((envelope) => envelope.data.assets ?? [])
        : client.getCatalogAsset(assetRef).then((envelope) => [envelope.data])
    request
      .then((records) => {
        if (!abandoned) setAssets(records)
      })
      .catch((error: unknown) => {
        if (!abandoned) {
          setFailure(asPageFailure(error, "The catalog listing is unavailable."))
        }
      })
    return () => {
      abandoned = true
    }
  }, [assetRef, client])

  return (
    <section aria-labelledby="catalog-title" className="summary-page">
      <p className="eyebrow">Governed catalog</p>
      <h1 id="catalog-title">Catalog</h1>
      <p className="summary-page__lead">Published meaning, ownership, and lineage.</p>
      <PageFailure expects="Published meaning, ownership and lineage for every governed asset." failure={failure} />
      {failure !== null || assets === null ? null : assets.length === 0 ? (
        <p className="summary-page__guidance">No catalog assets have been published.</p>
      ) : (
        <ul aria-label="Published catalog assets" className="capability-ledger">
          {assets.map((asset) => (
            <li className="capability-ledger__item" key={asset.asset_ref}>
              <div>
                <h2>
                  {assetRef === undefined ? (
                    <a href={`/catalog/${asset.asset_ref}`}>{asset.display_name}</a>
                  ) : (
                    asset.display_name
                  )}
                </h2>
                <p>{asset.definition}</p>
                {asset.owner === null || asset.owner === undefined ? null : (
                  <p>Owner: {asset.owner}</p>
                )}
                <p>{asset.lineage_summary}</p>
                {(asset.classifications ?? []).length === 0 ? null : (
                  <p>Classifications: {(asset.classifications ?? []).join(", ")}</p>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
