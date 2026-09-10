import {useEffect, useState} from "react"

import {ConsoleApiError} from "../../api/client"
import type {
  ConsoleEnvelopeDataProductsView,
  ConsoleEnvelopeDataProductView,
  DataProductView,
} from "../../api/generated"

export interface DataProductsClient {
  getDataProducts(): Promise<ConsoleEnvelopeDataProductsView>
  getDataProduct(dataProductId: string): Promise<ConsoleEnvelopeDataProductView>
}

interface DataProductsPageProps {
  readonly client: DataProductsClient
  readonly dataProductId?: string
}

function dataProductLabel(reference: string): string {
  return reference
    .replace(/^product-/, "")
    .replaceAll(/[-_]+/g, " ")
    .replace(/^./, (first) => first.toUpperCase())
}

export function DataProductsPage({client, dataProductId}: DataProductsPageProps) {
  const [products, setProducts] = useState<readonly DataProductView[] | null>(null)
  const [failure, setFailure] = useState<string | null>(null)

  useEffect(() => {
    let abandoned = false
    const request =
      dataProductId === undefined
        ? client.getDataProducts().then((envelope) => envelope.data.products ?? [])
        : client.getDataProduct(dataProductId).then((envelope) => [envelope.data])
    request
      .then((records) => {
        if (!abandoned) setProducts(records)
      })
      .catch((error: unknown) => {
        if (!abandoned) {
          setFailure(
            error instanceof ConsoleApiError
              ? error.message
              : "The data product listing is unavailable.",
          )
        }
      })
    return () => {
      abandoned = true
    }
  }, [client, dataProductId])

  return (
    <section aria-labelledby="data-products-title" className="summary-page">
      <p className="eyebrow">Governed products</p>
      <h1 id="data-products-title">Data products</h1>
      <p className="summary-page__lead">Products permitted by this workspace&rsquo;s policy.</p>
      {failure === null ? null : <p role="alert">{failure}</p>}
      {failure !== null || products === null ? null : products.length === 0 ? (
        <p className="summary-page__guidance">No data products are currently permitted.</p>
      ) : (
        <ul aria-label="Governed data products" className="capability-ledger">
          {products.map((product) => (
            <li className="capability-ledger__item" key={product.data_product_id}>
              <div>
                <h2>
                  {dataProductId === undefined ? (
                    <a href={`/data-products/${product.data_product_id}`}>
                      {dataProductLabel(product.data_product_id)}
                    </a>
                  ) : (
                    dataProductLabel(product.data_product_id)
                  )}
                </h2>
                <p>Version {product.version}</p>
                <p>Permitted by workspace policy</p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
