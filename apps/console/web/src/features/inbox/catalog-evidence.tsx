import {useEffect, useState} from "react"

import type {CatalogAssetView, ConsoleEnvelopeCatalogAssetView, DataProvenance} from "../../api/generated"

// The browser never builds a provider URL. It may only follow the server-issued, tenant-scoped
// opaque redirect route, and only when the reference is a well-formed public identifier.
const publicIdPattern = /^[a-z][a-z0-9_-]{2,127}$/

export interface CatalogEvidenceClient {
  getCatalogAsset(assetRef: string): Promise<ConsoleEnvelopeCatalogAssetView>
}

interface CatalogEvidenceProps {
  readonly assetRef: string
  readonly client: CatalogEvidenceClient
  readonly dataProvenance: DataProvenance
}

export function CatalogEvidence({assetRef, client, dataProvenance}: CatalogEvidenceProps) {
  const [asset, setAsset] = useState<{
    readonly failed: boolean
    readonly value: CatalogAssetView | null
  }>({failed: false, value: null})

  useEffect(() => {
    let active = true
    void client
      .getCatalogAsset(assetRef)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (
          envelope.meta.data_provenance !== dataProvenance ||
          envelope.data.asset_ref !== assetRef
        ) {
          setAsset({failed: true, value: null})
          return
        }
        setAsset({failed: false, value: envelope.data})
      })
      .catch(() => {
        if (active) {
          setAsset({failed: true, value: null})
        }
      })
    return () => {
      active = false
    }
  }, [assetRef, client, dataProvenance])

  if (asset.failed) {
    return (
      <p className="inbox-unavailable" role="status">
        Catalog record unavailable for <code>{assetRef}</code>. The surrounding review is unchanged.
      </p>
    )
  }
  if (asset.value === null) {
    return (
      <p className="inbox-loading" role="status">
        Loading the catalog record…
      </p>
    )
  }

  const linkRef = asset.value.link_ref
  const classifications = asset.value.classifications ?? []

  return (
    <article className="catalog-evidence">
      <h4>{asset.value.display_name}</h4>
      <p>{asset.value.definition}</p>
      <dl>
        <div>
          <dt>Owner</dt>
          <dd>{asset.value.owner}</dd>
        </div>
        <div>
          <dt>Lineage</dt>
          <dd>{asset.value.lineage_summary}</dd>
        </div>
        <div>
          <dt>Classifications</dt>
          <dd>{classifications.length === 0 ? "None recorded" : classifications.join(", ")}</dd>
        </div>
      </dl>
      {linkRef === null || linkRef === undefined || !publicIdPattern.test(linkRef) ? (
        <p className="inbox-empty">No authorized catalog link was issued.</p>
      ) : (
        <a href={`/api/v1/links/${encodeURIComponent(linkRef)}`} rel="noreferrer">
          Open {asset.value.display_name} in the catalog
        </a>
      )}
    </article>
  )
}
