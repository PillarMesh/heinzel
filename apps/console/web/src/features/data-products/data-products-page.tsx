import {formatInstant} from "../../format/instant"
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

function ProductAvailability({product}: {readonly product: DataProductView}) {
  return (
    <section aria-label="Product availability" className="product-detail__availability">
      <h2>Available data</h2>
      <p className="capability-state capability-state--ready">Ready in managed warehouse</p>
      <dl className="product-detail__facts">
        <div>
          <dt>Columns</dt>
          <dd>{product.column_count} columns</dd>
        </div>
        <div>
          <dt>Sources</dt>
          <dd>
            {product.source_count} governed source{product.source_count === 1 ? "" : "s"}
          </dd>
        </div>
        <div>
          <dt>Product version</dt>
          <dd>
            Revision {product.product_revision}, generation {product.generation}
          </dd>
        </div>
        <div>
          <dt>Catalog</dt>
          <dd>Revision {product.catalog_revision}</dd>
        </div>
        <div>
          <dt>Freshness checked</dt>
          <dd>
            <time dateTime={product.freshness_observed_at ?? undefined}>
              {formatInstant(product.freshness_observed_at ?? "")}
            </time>
          </dd>
        </div>
      </dl>
      <details className="product-detail__technical-location">
        <summary>Technical location</summary>
        <code>
          {product.namespace}.{product.relation_name}
        </code>
      </details>
      <p>Permitted by workspace policy</p>
    </section>
  )
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

  if (dataProductId !== undefined) {
    const product = products?.[0]
    return (
      <section aria-labelledby="data-product-title" className="summary-page">
        <a href="/data-products">Back to data products</a>
        <p className="eyebrow">Governed product</p>
        {failure === null ? null : <p role="alert">{failure}</p>}
        {failure !== null || product === undefined ? null : (
          <>
            <h1 id="data-product-title">{product.name ?? "Publication pending"}</h1>
            {product.publication_status === "pending" ? (
              <p className="summary-page__guidance">
                The owning catalog has not published this product definition yet.
              </p>
            ) : (
              <>
                <p className="summary-page__lead">{product.description}</p>
                <ProductAvailability product={product} />
              </>
            )}
          </>
        )}
      </section>
    )
  }

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
                      {product.name ?? "Publication pending"}
                    </a>
                  ) : (
                    (product.name ?? "Publication pending")
                  )}
                </h2>
                {product.publication_status === "pending" ? (
                  <>
                    <p>The owning catalog has not published this product definition yet.</p>
                    <p>Permitted by workspace policy</p>
                  </>
                ) : (
                  <>
                    <p>{product.description}</p>
                    <p>
                      Product revision {product.product_revision}, generation {product.generation}
                    </p>
                    <p>
                      Ready in managed warehouse
                    </p>
                    <p>
                      {product.column_count} columns from {product.source_count} governed source
                      {product.source_count === 1 ? "" : "s"}
                    </p>
                    <p>
                      Catalog revision {product.catalog_revision}. Freshness checked{" "}
                      <time dateTime={product.freshness_observed_at ?? undefined}>
                        {formatInstant(product.freshness_observed_at ?? "")}
                      </time>
                      .
                    </p>
                    <p>Permitted by workspace policy</p>
                  </>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
